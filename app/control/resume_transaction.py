"""Recover-first coordinator for durable review application and resume."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from langchain_core.runnables import RunnableConfig

from app.control.projections import (
    ProjectionBoundary,
    ProjectionWarning,
    project_workflow_state,
)
from app.control.resume import normalize_resume_review_request
from app.control.terminal import (
    TerminalFinalizationError,
    finalize_authoritative_terminal,
    reconcile_canonical_terminal_checkpoint,
)
from app.review.application import (
    PersistedReviewIntent,
    ReviewApplicationIdentity,
    ReviewIntentConflictError,
    VerifiedReviewApplication,
    load_verified_review_application,
    load_verified_review_intent,
    persist_review_application,
    persist_review_intent,
    portable_resume_review_request,
    review_identity_from_blocked_state,
)
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate
from app.review.ledger import record_review_applied, record_review_decision
from app.review.status import (
    ReviewLifecycleStatus,
    ReviewStatusProjection,
    project_review_status,
)
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import TopLevelState, validate_top_level_state
from app.storage.artifacts import resolve_artifact_ref
from app.storage.frozen_inputs import FrozenExecutionLease
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


@dataclass(frozen=True)
class ResumeTransactionResult:
    """Durable resume result and non-authoritative operational diagnostics."""

    state: TopLevelState
    decision_artifact_path: Path | None
    intent_artifact_path: Path | None = None
    application_artifact_path: Path | None = None
    review_status: ReviewLifecycleStatus | None = None
    projection_warnings: tuple[ProjectionWarning, ...] = ()
    operational_warnings: tuple[str, ...] = ()
    compaction_warning: str | None = None


class ResumeTransactionError(RuntimeError):
    """A review transaction failed at one named recoverable phase."""

    def __init__(
        self,
        phase: str,
        message: str,
        *,
        review_status: ReviewLifecycleStatus | None = None,
        intent_ref: ArtifactRef | None = None,
        application_ref: ArtifactRef | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.review_status = review_status
        self.intent_ref = intent_ref
        self.application_ref = application_ref


@dataclass(frozen=True)
class _TerminalPreflight:
    state: TopLevelState
    compaction_warning: str | None = None
    operational_warnings: tuple[str, ...] = ()


def execute_resume_transaction(
    *,
    graph: Any,
    config: RunnableConfig,
    session_id: str,
    data_dir: str | Path,
    request: ResumeReviewRequest | None,
    source: str,
    checkpointer: str,
    lease: FrozenExecutionLease,
    blocked_timeout_seconds: float | None = None,
    event_context: Mapping[str, Any] | None = None,
    projection_boundary: ProjectionBoundary | None = None,
    on_review_applied: Callable[[ResumeReviewRequest, str], None] | None = None,
) -> ResumeTransactionResult:
    """Recover or apply one exact review decision while holding the session lease."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    try:
        lease.require_active(paths)
    except ValueError as exc:
        raise ResumeTransactionError("lease", str(exc)) from exc

    ledger = Ledger(paths.run_dir)
    boundary = projection_boundary or ProjectionBoundary()
    state = _read_checkpoint(graph, config, session_id)
    terminal = _terminal_preflight(
        graph=graph,
        config=config,
        state=state,
        session_id=session_id,
        paths=paths,
        checkpointer=checkpointer,
        ledger=ledger,
    )
    if terminal is not None:
        project_workflow_state(
            boundary,
            terminal.state,
            data_dir=paths.run_dir,
            session_id=session_id,
        )
        return _transaction_result(
            paths,
            terminal.state,
            boundary=boundary,
            compaction_warning=terminal.compaction_warning,
            operational_warnings=terminal.operational_warnings,
        )

    identity = _identity_from_checkpoint(paths, state)
    status_projection = _healthy_status(state, identity)
    status = status_projection.status
    intent_ref = status_projection.intent_ref
    application_ref = status_projection.application_ref
    workflow_step_id = identity.workflow_step_id
    payload: dict[str, Any] = {
        **dict(event_context or {}),
        "source": source,
        "checkpointer": checkpointer,
        "data_dir": str(paths.run_dir),
        "max_blocked_seconds": blocked_timeout_seconds,
        "review_status": status,
        "review_request_id": identity.review_request_id,
        "request_hash": identity.request_hash,
    }
    if request is not None:
        payload["decision"] = _decision_event_payload(request, workflow_step_id)
        if request.rationale_artifact_ref:
            payload["rationale_artifact_ref"] = (
                request.rationale_artifact_ref.model_dump(mode="json")
            )

    started = False

    def record_started() -> None:
        nonlocal started
        if started:
            return
        ledger.record_event(
            session_id,
            "resume_started",
            payload,
            stage=identity.stage,
            workflow_step_id=workflow_step_id,
            subject_id=identity.review_request_id,
        )
        started = True

    phase = "validation"
    compaction_warning: str | None = None
    operational_warnings: list[str] = []
    try:
        record_started()
        if status == "awaiting_decision":
            if request is None:
                raise ResumeTransactionError(
                    "validation",
                    "review occurrence is awaiting a decision",
                    review_status=status,
                )
            phase = "validation"
            canonical_request = normalize_resume_review_request(
                state,
                request,
                blocked_timeout_seconds=blocked_timeout_seconds,
                decision_source=source,
            )
            payload["decision"] = _decision_event_payload(
                canonical_request,
                workflow_step_id,
            )
            phase = "intent_persist"
            persisted_intent = persist_review_intent(
                state,
                canonical_request,
                source=source,
                blocked_timeout_seconds=blocked_timeout_seconds,
            )
            intent_ref = persisted_intent.ref
            status_projection = _healthy_status(state, identity)
            status = status_projection.status
            if status != "decision_committed":
                raise ResumeTransactionError(
                    "intent_persist",
                    "persisted review intent did not project as decision_committed",
                    review_status=status,
                    intent_ref=intent_ref,
                )
        else:
            if intent_ref is None:
                raise ResumeTransactionError(
                    "validation",
                    "committed review state has no verified intent ref",
                    review_status=status,
                )
            intent_model = load_verified_review_intent(
                paths.run_dir,
                intent_ref,
                expected_identity=identity,
            )
            persisted_intent = PersistedReviewIntent(
                intent=intent_model,
                ref=intent_ref,
            )
            if request is not None:
                _require_matching_committed_request(
                    paths,
                    identity,
                    request,
                    intent_model.request,
                )

        gate = get_review_gate(identity.stage, identity.gate_id)
        if status == "decision_committed":
            phase = "application_prepare"
            prepared = gate.load_prepare_application()(state, persisted_intent.intent)
            phase = "application_persist"
            application_ref = persist_review_application(
                state,
                intent=persisted_intent,
                prepared=prepared,
            )
            status_projection = _healthy_status(state, identity)
            status = status_projection.status
            if status != "application_prepared":
                raise ResumeTransactionError(
                    "application_persist",
                    "persisted review application did not project as prepared",
                    review_status=status,
                    intent_ref=intent_ref,
                    application_ref=application_ref,
                )

        if application_ref is None:
            raise ResumeTransactionError(
                "application_persist",
                "review application ref is unavailable",
                review_status=status,
                intent_ref=intent_ref,
            )
        verified_application = load_verified_review_application(
            paths.run_dir,
            application_ref,
            expected_identity=identity,
        )

        phase = "application_persist"
        record_review_decision(
            ledger,
            session_id,
            application_ref=verified_application.ref,
            expected_identity=identity,
        )

        phase = "checkpoint_apply"
        checkpoint_state = _read_checkpoint(graph, config, session_id)
        if not _checkpoint_has_application(
            checkpoint_state,
            verified_application.ref,
        ):
            _require_same_blocked_occurrence(checkpoint_state, identity)
            update = dict(
                gate.load_apply_application()(checkpoint_state, verified_application)
            )
            update["review_application_ref"] = verified_application.ref.model_dump(
                mode="json"
            )
            update_error: Exception | None = None
            update_result: Any = None
            try:
                # A blocked run last wrote through route_controller. Letting
                # LangGraph infer that node would dispatch the advanced cursor
                # directly into an unseeded stage. Apply as the gate's owning
                # stage so its unconditional edge re-enters route_controller.
                update_result = graph.update_state(
                    config, update, as_node=f"{identity.stage}_stage"
                )
            except Exception as exc:
                update_error = exc

            phase = "checkpoint_verify"
            checkpoint_state = _read_checkpoint(graph, config, session_id)
            if not _checkpoint_has_application(
                checkpoint_state,
                verified_application.ref,
            ):
                detail = "checkpoint does not carry the prepared application ref"
                if update_error is not None:
                    detail = (
                        f"update_state raised {type(update_error).__name__}: "
                        f"{update_error}; {detail}"
                    )
                elif update_result is False:
                    detail = f"update_state returned False; {detail}"
                raise ResumeTransactionError(
                    "checkpoint_apply",
                    detail,
                    review_status="application_prepared",
                    intent_ref=intent_ref,
                    application_ref=application_ref,
                )

        phase = "checkpoint_verify"
        record_review_applied(
            ledger,
            session_id,
            application_ref=verified_application.ref,
            expected_identity=identity,
            checkpoint_state=checkpoint_state,
        )
        status_projection = _healthy_status(checkpoint_state, identity)
        status = status_projection.status
        if status != "applied":
            raise ResumeTransactionError(
                "checkpoint_verify",
                "checkpoint proof did not project as review applied",
                review_status=status,
                intent_ref=intent_ref,
                application_ref=application_ref,
            )

        # Publish the verified transition before downstream work can block.
        if on_review_applied is not None:
            boundary.run(
                "review_applied",
                on_review_applied,
                persisted_intent.intent.request,
                checkpoint_state["supervision_state"],
                disable_after_failure=True,
            )
        if checkpoint_state["supervision_state"] != "completed":
            project_workflow_state(
                boundary,
                checkpoint_state,
                data_dir=paths.run_dir,
                session_id=session_id,
            )

        phase = "invoke"
        try:
            invoked = graph.invoke(None, config=config)
        except Exception as invoke_error:
            latest = _read_checkpoint(graph, config, session_id)
            recovered_terminal = _terminal_preflight(
                graph=graph,
                config=config,
                state=latest,
                session_id=session_id,
                paths=paths,
                checkpointer=checkpointer,
                ledger=ledger,
            )
            if recovered_terminal is not None:
                final_state = recovered_terminal.state
                compaction_warning = recovered_terminal.compaction_warning
                operational_warnings.extend(recovered_terminal.operational_warnings)
                operational_warnings.append(
                    _bounded_error(
                        "graph.invoke raised after a terminal checkpoint: "
                        f"{type(invoke_error).__name__}: {invoke_error}"
                    )
                )
            else:
                phase = "terminal_finalize"
                failure_state: TopLevelState = {
                    **latest,
                    "supervision_state": "failed_terminal",
                    "last_error": (f"{type(invoke_error).__name__}: {invoke_error}"),
                    "active_interrupt": None,
                }
                finalized = finalize_authoritative_terminal(
                    graph,
                    config,
                    dict(failure_state),
                    session_id,
                    paths.run_dir,
                    checkpointer,
                    normalize_failure=True,
                )
                final_state = finalized.state
                compaction_warning = finalized.compaction_warning
                project_workflow_state(
                    boundary,
                    final_state,
                    data_dir=paths.run_dir,
                    session_id=session_id,
                )
                raise ResumeTransactionError(
                    "invoke",
                    f"{type(invoke_error).__name__}: {invoke_error}",
                    review_status="applied",
                    intent_ref=intent_ref,
                    application_ref=application_ref,
                ) from invoke_error
        else:
            final_state = validate_top_level_state(invoked)
            if final_state.get("supervision_state") in {
                "completed",
                "failed_terminal",
            }:
                phase = "terminal_finalize"
                finalized = finalize_authoritative_terminal(
                    graph,
                    config,
                    dict(final_state),
                    session_id,
                    paths.run_dir,
                    checkpointer,
                )
                final_state = finalized.state
                compaction_warning = finalized.compaction_warning

        project_workflow_state(
            boundary,
            final_state,
            data_dir=paths.run_dir,
            session_id=session_id,
        )
    except ResumeTransactionError as exc:
        _record_resume_failed(
            ledger,
            session_id,
            payload,
            identity,
            exc,
            fallback_status=status,
            intent_ref=intent_ref,
            application_ref=application_ref,
        )
        raise
    except Exception as exc:
        failure = ResumeTransactionError(
            phase,
            f"{type(exc).__name__}: {exc}",
            review_status=status,
            intent_ref=intent_ref,
            application_ref=application_ref,
        )
        _record_resume_failed(
            ledger,
            session_id,
            payload,
            identity,
            failure,
            fallback_status=status,
            intent_ref=intent_ref,
            application_ref=application_ref,
        )
        raise failure from exc

    ledger.record_event(
        session_id,
        "resume_completed",
        {
            **payload,
            "review_status": "applied",
            "current_stage": final_state.get("current_stage"),
            "supervision_state": final_state.get("supervision_state"),
            "route_trace": final_state.get("route_trace", []),
        },
        stage=identity.stage,
        workflow_step_id=workflow_step_id,
        subject_id=identity.review_request_id,
    )
    return _transaction_result(
        paths,
        final_state,
        application=verified_application,
        review_status="applied",
        boundary=boundary,
        compaction_warning=compaction_warning,
        operational_warnings=tuple(operational_warnings),
    )


def _read_checkpoint(
    graph: Any,
    config: RunnableConfig,
    session_id: str,
) -> TopLevelState:
    try:
        snapshot = graph.get_state(config)
        values = getattr(snapshot, "values", None)
        if not isinstance(values, dict) or not values:
            raise ValueError(f"no checkpoint found for session_id {session_id!r}")
        if values.get("session_id") != session_id:
            raise ValueError(
                "checkpoint session_id does not match the requested session"
            )
        return validate_top_level_state(values)
    except ResumeTransactionError:
        raise
    except Exception as exc:
        raise ResumeTransactionError(
            "snapshot",
            f"checkpoint could not be read: {type(exc).__name__}: {exc}",
        ) from exc


def _terminal_preflight(
    *,
    graph: Any,
    config: RunnableConfig,
    state: TopLevelState,
    session_id: str,
    paths: SessionPaths,
    checkpointer: str,
    ledger: Ledger,
) -> _TerminalPreflight | None:
    terminal_read = ledger.read_canonical_terminal(session_id)
    supervision = state.get("supervision_state")
    checkpoint_terminal = supervision in {"completed", "failed_terminal"}
    if terminal_read is not None:
        canonical = terminal_read["canonical"]
        try:
            reconcile_canonical_terminal_checkpoint(
                state,
                canonical,
                session_id=session_id,
            )
        except TerminalFinalizationError as exc:
            raise ResumeTransactionError("terminal_preflight", str(exc)) from exc
        integrity = terminal_read.get("terminal_integrity")
        if integrity is not None:
            return _TerminalPreflight(
                state=state,
                operational_warnings=(
                    _bounded_error(
                        "terminal ledger integrity diagnostic: "
                        f"{integrity.get('kind')} "
                        f"({integrity.get('terminal_count')} rows)"
                    ),
                ),
            )
        try:
            finalized = finalize_authoritative_terminal(
                graph,
                config,
                dict(state),
                session_id,
                paths.run_dir,
                checkpointer,
            )
        except TerminalFinalizationError as exc:
            raise ResumeTransactionError(
                "terminal_preflight",
                str(exc),
            ) from exc
        return _TerminalPreflight(
            state=finalized.state,
            compaction_warning=finalized.compaction_warning,
        )
    if not checkpoint_terminal:
        return None
    try:
        finalized = finalize_authoritative_terminal(
            graph,
            config,
            dict(state),
            session_id,
            paths.run_dir,
            checkpointer,
        )
    except TerminalFinalizationError as exc:
        raise ResumeTransactionError("terminal_preflight", str(exc)) from exc
    return _TerminalPreflight(
        state=finalized.state,
        compaction_warning=finalized.compaction_warning,
    )


def _identity_from_checkpoint(
    paths: SessionPaths,
    state: TopLevelState,
) -> ReviewApplicationIdentity:
    if state.get("supervision_state") == "blocked_human":
        return review_identity_from_blocked_state(state)
    raw_ref = state.get("review_application_ref")
    if raw_ref is None:
        raise ResumeTransactionError(
            "validation",
            "checkpoint is neither an active review gate nor an applied "
            "review recovery",
        )
    verified = load_verified_review_application(paths.run_dir, raw_ref)
    application = verified.application
    return ReviewApplicationIdentity(
        session_id=application.session_id,
        workflow_step_id=application.workflow_step_id,
        stage=application.stage,
        gate_id=application.gate_id,
        review_request_id=application.review_request_id,
        request_hash=application.request_hash,
    )


def _healthy_status(
    state: TopLevelState,
    identity: ReviewApplicationIdentity,
) -> ReviewStatusProjection:
    try:
        return project_review_status(state, identity=identity).require_healthy()
    except Exception as exc:
        raise ResumeTransactionError(
            "validation",
            f"review status integrity failure: {type(exc).__name__}: {exc}",
        ) from exc


def _require_matching_committed_request(
    paths: SessionPaths,
    identity: ReviewApplicationIdentity,
    proposed: ResumeReviewRequest,
    committed: ResumeReviewRequest,
) -> None:
    expected = {
        "session_id": identity.session_id,
        "stage": identity.stage,
        "review_request_id": identity.review_request_id,
        "review_request_hash": identity.request_hash,
    }
    for field, value in expected.items():
        if getattr(proposed, field) != value:
            raise ReviewIntentConflictError(
                f"submitted decision {field} conflicts with committed intent"
            )
    if proposed.gate_id not in {None, identity.gate_id}:
        raise ReviewIntentConflictError(
            "submitted decision gate_id conflicts with committed intent"
        )
    canonical = proposed.model_copy(update={"gate_id": identity.gate_id})
    portable = portable_resume_review_request(paths.run_dir, canonical)
    if portable != committed:
        raise ReviewIntentConflictError(
            "submitted decision conflicts with immutable intent.json"
        )


def _require_same_blocked_occurrence(
    state: TopLevelState,
    identity: ReviewApplicationIdentity,
) -> None:
    try:
        current = review_identity_from_blocked_state(state)
    except Exception as exc:
        raise ResumeTransactionError(
            "checkpoint_verify",
            f"prepared application no longer targets an active blocked gate: {exc}",
            review_status="application_prepared",
        ) from exc
    if current != identity:
        raise ResumeTransactionError(
            "checkpoint_verify",
            "prepared application targets a stale review occurrence",
            review_status="application_prepared",
        )


def _checkpoint_has_application(
    state: TopLevelState,
    expected: ArtifactRef,
) -> bool:
    raw_ref = state.get("review_application_ref")
    if raw_ref is None:
        return False
    try:
        return ArtifactRef.model_validate(raw_ref) == expected
    except Exception as exc:
        raise ResumeTransactionError(
            "checkpoint_verify",
            f"checkpoint review_application_ref is invalid: {exc}",
            review_status="application_prepared",
            application_ref=expected,
        ) from exc


def _record_resume_failed(
    ledger: Ledger,
    session_id: str,
    payload: Mapping[str, Any],
    identity: ReviewApplicationIdentity,
    error: ResumeTransactionError,
    *,
    fallback_status: ReviewLifecycleStatus,
    intent_ref: ArtifactRef | None,
    application_ref: ArtifactRef | None,
) -> None:
    refs = [
        ref
        for ref in (
            error.intent_ref or intent_ref,
            error.application_ref or application_ref,
        )
        if ref is not None
    ]
    ledger.record_event(
        session_id,
        "resume_failed",
        {
            **dict(payload),
            "phase": error.phase,
            "error": str(error),
            "review_status": error.review_status or fallback_status,
        },
        stage=identity.stage,
        workflow_step_id=identity.workflow_step_id,
        subject_id=identity.review_request_id,
        refs=refs,
    )


def _transaction_result(
    paths: SessionPaths,
    state: TopLevelState,
    *,
    application: VerifiedReviewApplication | None = None,
    review_status: ReviewLifecycleStatus | None = None,
    boundary: ProjectionBoundary,
    compaction_warning: str | None = None,
    operational_warnings: tuple[str, ...] = (),
) -> ResumeTransactionResult:
    if application is None and state.get("review_application_ref") is not None:
        try:
            checkpoint_application_ref = state.get("review_application_ref")
            assert checkpoint_application_ref is not None
            application = load_verified_review_application(
                paths.run_dir,
                checkpoint_application_ref,
            )
        except Exception:
            application = None
    decision_path: Path | None = None
    intent_path: Path | None = None
    application_path: Path | None = None
    if application is not None:
        application_path = resolve_artifact_ref(
            paths.run_dir,
            paths.session_id,
            application.ref,
        )
        decision_path = resolve_artifact_ref(
            paths.run_dir,
            paths.session_id,
            application.application.decision_ref,
        )
        intent_path = resolve_artifact_ref(
            paths.run_dir,
            paths.session_id,
            application.application.intent_ref,
        )
    return ResumeTransactionResult(
        state=state,
        decision_artifact_path=decision_path,
        intent_artifact_path=intent_path,
        application_artifact_path=application_path,
        review_status=review_status,
        projection_warnings=boundary.warnings,
        operational_warnings=operational_warnings,
        compaction_warning=compaction_warning,
    )


def _decision_event_payload(
    request: ResumeReviewRequest,
    workflow_step_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stage": request.stage,
        "gate_id": request.gate_id,
        "decision": request.decision,
        "candidate_id": request.candidate_id,
        "method_name": request.method_name,
        "decided_by": request.decided_by,
        "review_request_id": request.review_request_id,
    }
    if request.candidate_ids:
        payload["candidate_ids"] = list(request.candidate_ids)
    if workflow_step_id:
        payload["workflow_step_id"] = workflow_step_id
    return {key: value for key, value in payload.items() if value is not None}


def _bounded_error(message: str) -> str:
    return " ".join(message.split())[:1024]
