"""Lifecycle helpers for inspecting and resuming human review gates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig

from app.control import errors as control_errors
from app.control.occurrence_settlement import settle_workflow_step_outcome
from app.control.projections import reduce_workflow_progress
from app.control.workflow_plan import (
    current_workflow_step_id,
    fail_workflow_step,
    workflow_steps_from_run_config,
)
from app.review.application import (
    ReviewApplicationError,
    canonicalize_review_intent_request,
)
from app.review.approval import ensure_decision_allowed, load_verified_interrupt_request
from app.review.base import ResumeReviewRequest
from app.review.gates import (
    ReviewGateDeclaration,
    find_review_gate,
    get_review_gate,
    registered_review_stage_label,
)
from app.review.interrupt_context import generic_interrupt_context
from app.review.persistence import ReviewPersistenceError, load_review_request
from app.review.records import counts_by_origin, decision_records
from app.review.status import ReviewStatusProjection, project_review_history
from app.schemas.artifacts import ArtifactRef
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import (
    ActiveInterrupt,
    StateUpdate,
    TopLevelState,
    validate_top_level_state,
)
from app.storage.artifacts import read_artifact_records, resolve_artifact_ref
from app.storage.ledger import (
    Ledger,
    LedgerConflictError,
    project_stage_terminal_events,
)
from app.storage.paths import SessionPaths

ResumeValidationError = control_errors.ResumeValidationError
UnsupportedLifecycleOperationError = control_errors.UnsupportedLifecycleOperationError


class TerminalFinalizationError(RuntimeError):
    """Raised when a terminal checkpoint cannot be proven authoritative."""


@dataclass(frozen=True)
class TerminalFinalizationResult:
    """Proven terminal checkpoint, singleton Ledger row, and soft warning."""

    state: TopLevelState
    terminal_event: dict[str, Any]
    compaction_warning: str | None = None


def reconcile_canonical_terminal_checkpoint(
    checkpoint_state: Mapping[str, Any],
    terminal_event: Mapping[str, Any],
    *,
    session_id: str,
) -> None:
    """Require one canonical terminal to describe the exact terminal checkpoint."""

    if checkpoint_state.get("session_id") != session_id:
        raise TerminalFinalizationError(
            "terminal checkpoint session_id conflicts with the requested session"
        )
    if terminal_event.get("session_id") != session_id:
        raise TerminalFinalizationError(
            "canonical Ledger terminal session_id conflicts with the checkpoint"
        )
    summary = terminal_event.get("summary")
    outcome = summary.get("outcome") if isinstance(summary, Mapping) else None
    outcome_key = outcome if isinstance(outcome, str) else ""
    expected_supervision = {
        "completed": "completed",
        "failed": "failed_terminal",
    }.get(outcome_key)
    if expected_supervision is None:
        raise TerminalFinalizationError(
            f"canonical Ledger terminal has unsupported outcome: {outcome!r}"
        )
    failure_scope = (
        summary.get("failure_scope") if isinstance(summary, Mapping) else None
    )
    if outcome_key == "completed" and failure_scope is not None:
        raise TerminalFinalizationError(
            "completed canonical Ledger terminal must not carry failure_scope"
        )
    if (
        outcome_key == "failed"
        and failure_scope is not None
        and failure_scope
        not in {
            "occurrence",
            "controller_boundary",
        }
    ):
        raise TerminalFinalizationError(
            "canonical Ledger terminal has an invalid failure_scope"
        )
    if checkpoint_state.get("supervision_state") != expected_supervision:
        raise TerminalFinalizationError(
            "canonical Ledger terminal outcome conflicts with the checkpoint"
        )
    if checkpoint_state.get("active_interrupt") is not None:
        raise TerminalFinalizationError(
            "terminal checkpoint still carries an active review interrupt"
        )
    terminal_stage = terminal_event.get("stage")
    if not isinstance(terminal_stage, str) or not terminal_stage:
        raise TerminalFinalizationError("canonical Ledger terminal has no valid stage")
    if checkpoint_state.get("current_stage") != terminal_stage:
        raise TerminalFinalizationError(
            "canonical Ledger terminal stage conflicts with the checkpoint"
        )


def finalize_authoritative_terminal(
    graph: Any,
    config: RunnableConfig,
    state: dict[str, Any],
    session_id: str,
    data_dir: str | Path,
    checkpointer: str,
    *,
    normalize_failure: bool = False,
) -> TerminalFinalizationResult:
    """Prove terminal checkpoint, append one terminal fact, then compact.

    Projection writers are deliberately absent.  An ambiguous failure update
    is always followed by a checkpoint reread; matching persisted values win.
    """

    supervision = state.get("supervision_state")
    if supervision not in {"completed", "failed_terminal"}:
        raise TerminalFinalizationError(
            "authoritative terminal finalization requires a terminal state"
        )
    if normalize_failure and supervision != "failed_terminal":
        raise TerminalFinalizationError(
            "normalize_failure requires supervision_state=failed_terminal"
        )

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    terminal_state = {
        **state,
        "session_id": session_id,
        "data_dir": str(paths.run_dir),
        "active_interrupt": None,
    }

    update_error: Exception | None = None
    if normalize_failure:
        pending_outcome = terminal_state.get("workflow_step_outcome")
        workflow_index = terminal_state.get("workflow_step_index")
        entered_index = terminal_state.get("entered_workflow_step_index")
        between_occurrences = (
            isinstance(pending_outcome, Mapping)
            and pending_outcome.get("outcome") in {"completed", "skipped"}
            and type(workflow_index) is int
            and type(entered_index) is int
            and workflow_index == entered_index + 1
        )
        if between_occurrences:
            occurrence_failure: StateUpdate = {
                "supervision_state": "failed_terminal",
                "last_error": str(
                    terminal_state.get("last_error") or "workflow execution failed"
                ),
            }
        else:
            try:
                occurrence_failure = fail_workflow_step(
                    cast(TopLevelState, terminal_state),
                    str(terminal_state.get("current_stage") or ""),
                    str(
                        terminal_state.get("last_error") or "workflow execution failed"
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise TerminalFinalizationError(
                    "cannot normalize a runtime failure without an active frozen "
                    f"workflow occurrence: {exc}"
                ) from exc
        terminal_state.update(occurrence_failure)
        try:
            graph.update_state(
                config,
                {
                    **occurrence_failure,
                    "active_interrupt": None,
                },
            )
        except Exception as exc:
            update_error = exc

    try:
        snapshot = graph.get_state(config)
        checkpoint_values = getattr(snapshot, "values", None)
        if not isinstance(checkpoint_values, dict):
            raise TerminalFinalizationError(
                "persisted terminal checkpoint has no state values"
            )
        checkpoint_state = validate_top_level_state(checkpoint_values)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if update_error is not None:
            detail = (
                f"update_state raised {type(update_error).__name__}: {update_error}; "
                f"checkpoint reread failed: {detail}"
            )
        raise TerminalFinalizationError(
            f"terminal checkpoint could not be verified: {detail}"
        ) from exc

    try:
        settle_workflow_step_outcome(
            checkpoint_state,
            data_dir=paths.run_dir,
        )
    except (OSError, TypeError, ValueError, LedgerConflictError) as exc:
        raise TerminalFinalizationError(
            f"terminal checkpoint occurrence settlement failed: {exc}"
        ) from exc

    stage_terminals, terminal_integrity = project_stage_terminal_events(
        ledger.read_event_rows(session_id)
    )
    if terminal_integrity:
        raise TerminalFinalizationError(
            "terminal checkpoint has non-authoritative occurrence history"
        )
    steps = list(checkpoint_state.get("workflow_steps") or [])
    failure_scope: Literal["occurrence", "controller_boundary"] | None = None
    if supervision == "completed":
        if checkpoint_state.get("workflow_step_index") != len(steps):
            raise TerminalFinalizationError(
                "completed terminal checkpoint has not reached the end of its plan"
            )
        unsettled = [
            step["step_id"]
            for step in steps
            if stage_terminals.get((step["step_id"], step["stage"]), {}).get(
                "event_type"
            )
            not in {"stage_completed", "stage_skipped"}
        ]
        if unsettled:
            raise TerminalFinalizationError(
                "completed terminal checkpoint has unsettled workflow occurrences: "
                + ", ".join(unsettled)
            )
    else:
        failed = [
            terminal
            for terminal in stage_terminals.values()
            if terminal.get("event_type") == "stage_failed"
        ]
        workflow_step_outcome = checkpoint_state.get("workflow_step_outcome")
        if (
            isinstance(workflow_step_outcome, Mapping)
            and workflow_step_outcome.get("outcome") == "failed"
        ):
            if (
                len(failed) != 1
                or failed[0].get("workflow_step_id")
                != workflow_step_outcome.get("workflow_step_id")
                or failed[0].get("stage") != workflow_step_outcome.get("stage")
            ):
                raise TerminalFinalizationError(
                    "failed terminal checkpoint lacks one matching failed occurrence"
                )
            failed_step_id = str(workflow_step_outcome.get("workflow_step_id") or "")
            failed_index = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step.get("step_id") == failed_step_id
                ),
                None,
            )
            prefix_settled = failed_index is not None and all(
                stage_terminals.get((step["step_id"], step["stage"]), {}).get(
                    "event_type"
                )
                in {"stage_completed", "stage_skipped"}
                for step in steps[:failed_index]
            )
            no_terminal_after_failed = failed_index is not None and all(
                (step["step_id"], step["stage"]) not in stage_terminals
                for step in steps[failed_index + 1 :]
            )
            if not prefix_settled or not no_terminal_after_failed:
                raise TerminalFinalizationError(
                    "failed occurrence is not the terminal edge of a fully "
                    "settled workflow prefix"
                )
            failure_scope = "occurrence"
        else:
            workflow_index = checkpoint_state.get("workflow_step_index")
            entered_index = checkpoint_state.get("entered_workflow_step_index")
            boundary_failure = (
                not failed
                and isinstance(workflow_step_outcome, Mapping)
                and workflow_step_outcome.get("outcome") in {"completed", "skipped"}
                and type(workflow_index) is int
                and type(entered_index) is int
                and workflow_index == entered_index + 1
            )
            prefix_settled = boundary_failure and all(
                stage_terminals.get((step["step_id"], step["stage"]), {}).get(
                    "event_type"
                )
                in {"stage_completed", "stage_skipped"}
                for step in steps[:workflow_index]
            )
            no_terminal_after_frontier = boundary_failure and all(
                (step["step_id"], step["stage"]) not in stage_terminals
                for step in steps[workflow_index:]
            )
            if not prefix_settled or not no_terminal_after_frontier:
                raise TerminalFinalizationError(
                    "failed terminal checkpoint has neither a failed occurrence "
                    "nor a fully settled inter-occurrence frontier"
                )
            failure_scope = "controller_boundary"

    expected = {
        "session_id": session_id,
        "supervision_state": supervision,
        "current_stage": terminal_state.get("current_stage"),
        "active_interrupt": None,
    }
    if supervision == "failed_terminal":
        expected["last_error"] = terminal_state.get("last_error")
    for field, value in expected.items():
        if checkpoint_state.get(field) != value:
            detail = (
                f"terminal checkpoint {field} does not match: "
                f"expected {value!r}, got {checkpoint_state.get(field)!r}"
            )
            if update_error is not None:
                detail += (
                    f" after update_state raised {type(update_error).__name__}: "
                    f"{update_error}"
                )
            raise TerminalFinalizationError(detail)

    outcome: Literal["completed", "failed"] = (
        "completed" if supervision == "completed" else "failed"
    )
    refs = ledger.references_from(
        checkpoint_state,
        role_prefix=f"terminal.{outcome}",
    )
    terminal_read = ledger.read_canonical_terminal(session_id)
    if terminal_read is not None:
        reconcile_canonical_terminal_checkpoint(
            checkpoint_state,
            terminal_read["canonical"],
            session_id=session_id,
        )
    try:
        terminal_event = ledger.record_session_terminal(
            session_id,
            outcome=outcome,
            stage=checkpoint_state.get("current_stage"),
            error=(checkpoint_state.get("last_error") if outcome == "failed" else None),
            refs=refs,
            failure_scope=failure_scope,
        )
    except LedgerConflictError as exc:
        raise TerminalFinalizationError(str(exc)) from exc
    reconcile_canonical_terminal_checkpoint(
        checkpoint_state,
        terminal_event,
        session_id=session_id,
    )

    saver = getattr(graph, "checkpointer", None)
    if checkpointer != "sqlite" or saver is None:
        return TerminalFinalizationResult(
            state=checkpoint_state,
            terminal_event=terminal_event,
        )

    compaction_warning: str | None = None
    try:
        from app.graph.checkpoint_compaction import compact_terminal_checkpoint

        compacted = compact_terminal_checkpoint(saver, config, checkpoint_state)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        ledger.record_event(
            session_id,
            "terminal_compaction_skipped",
            {"error": error},
            idempotency_key="terminal-compaction:skipped",
        )
        compaction_warning = error
        compacted = False

    if compacted:
        ledger.record_event(
            session_id,
            "terminal_compaction_completed",
            {"checkpoint_count": 1},
            idempotency_key="terminal-compaction:completed",
        )
    return TerminalFinalizationResult(
        state=checkpoint_state,
        terminal_event=terminal_event,
        compaction_warning=compaction_warning,
    )


def inspect_state(
    state: TopLevelState,
    *,
    blocked_timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a compact non-mutating summary of an in-memory workflow state.

    Persistence facts (``checkpoint_persistence`` /
    ``durable_resume_available``) are intentionally absent here — they
    are properties of the LangGraph checkpoint backend, not of the
    in-memory state dict. Use :func:`inspect_session` for those.
    """

    snapshot = deepcopy(state)
    active_interrupt = snapshot.get("active_interrupt")
    request_error: str | None = None
    try:
        decision_requirements = _decision_requirements_from_state(
            snapshot,
            active_interrupt,
        )
    except ResumeValidationError as exc:
        decision_requirements = None
        request_error = str(exc)
    workflow_progress = _checkpoint_workflow_progress(snapshot, active_interrupt)
    summary: dict[str, Any] = {
        "session_id": snapshot.get("session_id"),
        # These public current fields are reduced from explicit occurrence
        # facts.  A checkpoint cursor is not evidence that earlier occurrences
        # settled, so it cannot independently establish workflow progress.
        "current_stage": workflow_progress["current_stage"],
        "current_workflow_step_id": workflow_progress["current_workflow_step_id"],
        "supervision_state": snapshot.get("supervision_state"),
        "active_interrupt": active_interrupt,
        "stage_statuses": _stage_statuses(snapshot),
        "latest_artifact_refs": _latest_artifact_refs(snapshot),
        "decision_requirements": decision_requirements,
        "workflow_progress": workflow_progress,
        # Kept only as an explicitly non-authoritative checkpoint observation
        # for callers that need to display the paused graph node.
        "checkpoint_current_stage": snapshot.get("current_stage"),
    }
    if request_error:
        summary["review_request_verification_error"] = request_error
    timeout = _blocked_timeout_status(
        active_interrupt, blocked_timeout_seconds, now=now
    )
    if timeout:
        summary["blocked_timeout"] = timeout
    return summary


def inspect_session(
    session_id: str,
    data_dir: str | Path = "data",
    *,
    blocked_timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect durable ledger/artifact records and checkpoint availability."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    session = ledger.read_session(session_id)
    events = ledger.read_events(session_id)
    artifacts = read_artifact_records(paths.run_dir, session_id)
    review_history = project_review_history(paths.run_dir, session_id)
    review_occurrences = review_history.records()
    terminal_sequence = int(
        (review_history.canonical_terminal or {}).get("sequence") or 0
    )
    authoritative_events = [
        event
        for event in events
        if terminal_sequence == 0
        or int(event.get("sequence") or 0) <= terminal_sequence
    ]
    authoritative_occurrences = [
        occurrence
        for occurrence in review_history.occurrences
        if terminal_sequence == 0
        or int(occurrence.request_sequence or 0) <= terminal_sequence
    ]
    verified_decision_sequences = {
        occurrence.decision_sequence
        for occurrence in authoritative_occurrences
        if occurrence.integrity_error is None
        and occurrence.decision_sequence is not None
    }
    verified_applied_sequences = {
        occurrence.applied_sequence
        for occurrence in authoritative_occurrences
        if occurrence.integrity_error is None
        and occurrence.applied_sequence is not None
    }
    authoritative_review_decisions = [
        event
        for event in authoritative_events
        if event.get("event_type") == "review_decision"
        and int(event.get("sequence") or 0) in verified_decision_sequences
    ]
    trusted_review_events = [
        event
        for event in authoritative_events
        if event.get("event_type") not in {"review_decision", "review_applied"}
        or (
            event.get("event_type") == "review_decision"
            and int(event.get("sequence") or 0) in verified_decision_sequences
        )
        or (
            event.get("event_type") == "review_applied"
            and int(event.get("sequence") or 0) in verified_applied_sequences
        )
    ]
    latest_review_projection = (
        authoritative_occurrences[-1] if authoritative_occurrences else None
    )
    latest_active_review = review_history.latest_unapplied
    latest_review_request = (
        dict(latest_review_projection.request_event)
        if latest_review_projection is not None
        and latest_review_projection.request_event is not None
        else None
    )
    latest_review_decision = _latest_event(
        authoritative_review_decisions,
        "review_decision",
    )
    latest_env_preflight_block = _latest_event(
        authoritative_events, "env_preflight_blocked"
    )
    latest_terminal = review_history.canonical_terminal
    review_decisions = decision_records(authoritative_review_decisions)
    decisions_by_origin = counts_by_origin(review_decisions)
    latest_review_lifecycle_event = _latest_review_lifecycle_event(
        trusted_review_events
    )
    latest_unresolved_review_request = (
        dict(latest_active_review.request_event)
        if latest_active_review is not None
        and latest_active_review.request_event is not None
        else None
    )
    latest_request_ref = (
        latest_unresolved_review_request or latest_review_request or {}
    ).get("review_request_artifact_ref")
    latest_decision_ref = (latest_review_decision or {}).get("artifact_ref")
    latest_request_artifact = _artifact_for_ref(artifacts, latest_request_ref)
    if latest_request_artifact is None and latest_request_ref:
        latest_request_artifact = {"path_ref": latest_request_ref}
    latest_decision_artifact = _artifact_for_ref(artifacts, latest_decision_ref)
    if latest_decision_artifact is None and latest_decision_ref:
        latest_decision_artifact = {"path_ref": latest_decision_ref}
    current_status_path = paths.report_path("current_status.json")
    session_summary_path = paths.report_path("session_summary.json")
    latest_operator_report = (
        str(current_status_path) if current_status_path.exists() else None
    )
    latest_final_report = (
        str(session_summary_path) if session_summary_path.exists() else None
    )

    supervision_state = "unknown"
    if latest_terminal:
        supervision_state = _terminal_supervision_state(latest_terminal.get("outcome"))
    elif latest_active_review is not None:
        supervision_state = "blocked_human"
    elif review_history.integrity_errors:
        supervision_state = "review_integrity_error"
    elif session:
        supervision_state = "running"

    db_path = paths.checkpoint_db_path
    checkpoint_persistence = "sqlite" if db_path.exists() else "memory_only"

    decision_requirements_error: str | None = None
    if latest_unresolved_review_request and (
        latest_active_review is not None and latest_active_review.accepts_new_decision
    ):
        try:
            decision_requirements = _decision_requirements_from_artifact(
                latest_request_artifact,
                paths=paths,
                expected_request_id=str(
                    latest_unresolved_review_request.get("review_request_id")
                    or latest_unresolved_review_request.get("subject_id")
                    or ""
                ),
                expected_request_hash=(
                    _event_ref(
                        latest_unresolved_review_request, "review_request_artifact_ref"
                    )
                    or {}
                ).get("sha256"),
                expected_workflow_step_id=str(
                    latest_unresolved_review_request.get("workflow_step_id") or ""
                ),
            )
        except ResumeValidationError as exc:
            decision_requirements = None
            decision_requirements_error = str(exc)
    else:
        decision_requirements = None
    active_interrupt = _durable_interrupt_projection(
        latest_unresolved_review_request,
        decision_requirements,
        review_status=latest_active_review,
    )
    route_trace = _durable_route_trace(authoritative_events)
    last_error = _latest_error(authoritative_events)
    latest_failed_report = (
        latest_operator_report if supervision_state == "failed_terminal" else None
    )
    latest_blocked_report = (
        latest_operator_report if supervision_state == "blocked_human" else None
    )
    final_assembly: dict[str, Any] | None = None
    final_assembly_integrity_error: str | None = None
    if supervision_state == "completed":
        final_assembly, final_assembly_integrity_error = (
            _latest_verified_assembly_delivery(
                authoritative_events,
                paths=paths,
                session_id=session_id,
            )
        )
    stage_terminals, stage_terminal_integrity = project_stage_terminal_events(
        authoritative_events
    )
    workflow_progress, execution = _inspect_execution_read_model(
        paths,
        stage_terminals=stage_terminals,
        stage_terminal_integrity=stage_terminal_integrity,
        supervision_state=supervision_state,
        failure_scope=(
            latest_terminal.get("failure_scope")
            if isinstance(latest_terminal, Mapping)
            else None
        ),
        latest_unresolved_review_request=latest_unresolved_review_request,
    )
    durable_available = execution.get("durable_progress") == "checkpoint"
    workflow_progress_integrity = workflow_progress.pop(
        "integrity_diagnostics",
        [],
    )
    if workflow_progress["plan_source"] == "frozen_run_config":
        current_workflow_step_id = workflow_progress["current_workflow_step_id"]
        current_stage = workflow_progress["current_stage"]
        if (
            supervision_state == "blocked_human"
            and workflow_progress["blocked_steps"] != 1
        ):
            decision_requirements = None
            active_interrupt = None
            decision_requirements_error = decision_requirements_error or (
                "blocked review request does not match the verified frozen "
                "workflow occurrence"
            )
    else:
        current_workflow_step_id = None
        current_stage = None
        if latest_unresolved_review_request is not None:
            decision_requirements = None
            active_interrupt = None
            decision_requirements_error = decision_requirements_error or (
                "blocked review request cannot be authorized without verified "
                "frozen workflow inputs"
            )

    summary: dict[str, Any] = {
        "inspect_schema_version": 1,
        "session_id": session_id,
        "session": session,
        "current_stage": current_stage,
        "current_workflow_step_id": current_workflow_step_id,
        "supervision_state": supervision_state,
        "active_interrupt": active_interrupt,
        "route_trace": route_trace,
        "last_error": last_error,
        "checkpoint_persistence": checkpoint_persistence,
        "durable_resume_available": durable_available,
        "ledger_event_count": len(events),
        "latest_events": events[-5:],
        "latest_review_request": latest_review_request,
        "latest_review_decision": latest_review_decision,
        "latest_env_preflight_block": latest_env_preflight_block,
        "latest_terminal_event": latest_terminal,
        "review_decisions": review_decisions,
        "decisions_by_origin": decisions_by_origin,
        "latest_review_lifecycle_event": latest_review_lifecycle_event,
        "latest_unresolved_review_request": latest_unresolved_review_request,
        "latest_review_status": (
            latest_active_review.to_record()
            if latest_active_review is not None
            else (
                latest_review_projection.to_record()
                if latest_review_projection is not None
                else None
            )
        ),
        "review_occurrences": review_occurrences,
        "review_status_integrity_errors": list(review_history.integrity_errors),
        "terminal_integrity": review_history.terminal_integrity,
        "stage_terminal_integrity": stage_terminal_integrity,
        "latest_review_request_artifact": latest_request_artifact,
        "latest_review_decision_artifact": latest_decision_artifact,
        "latest_operator_report": latest_operator_report,
        "latest_failed_operator_report": latest_failed_report,
        "latest_blocked_operator_report": latest_blocked_report,
        # Reports are optional, derived views. Their contents are never lifecycle truth.
        "latest_operator_report_payload": None,
        "latest_final_report": latest_final_report,
        "final_assembly": final_assembly,
        "latest_relevant_artifacts": _latest_relevant_artifacts(artifacts),
        "decision_requirements": decision_requirements,
        "artifact_count": len(artifacts),
        "workflow_progress": workflow_progress,
        "workflow_progress_integrity": workflow_progress_integrity,
        "execution": execution,
    }
    if decision_requirements_error:
        summary["review_request_verification_error"] = decision_requirements_error
    if final_assembly_integrity_error:
        summary["final_assembly_integrity_error"] = final_assembly_integrity_error
    if (
        latest_unresolved_review_request
        and latest_active_review is not None
        and latest_active_review.accepts_new_decision
        and blocked_timeout_seconds is not None
    ):
        summary["blocked_timeout"] = _durable_blocked_timeout_status(
            latest_unresolved_review_request,
            blocked_timeout_seconds,
            now=now,
        )
    if not session:
        summary["error"] = "session_not_found"
    summary["next_action"] = build_next_action(summary, data_dir=data_dir)
    return summary


def normalize_resume_review_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    blocked_timeout_seconds: float | None = None,
    decision_source: str = "manual",
    now: datetime | None = None,
) -> ResumeReviewRequest:
    """Validate one proposal and return the canonical request to commit.

    This boundary performs no artifact, Ledger, or checkpoint writes. Automatic
    timeout rules may only normalize the request; durable preparation is owned
    by the review-application coordinator.
    """

    active_interrupt, request_record = _validated_resume_context(state, request)
    active_gate_id = active_interrupt.get("gate_id")
    canonical_request = (
        request.model_copy(update={"gate_id": active_gate_id})
        if request.gate_id is None and isinstance(active_gate_id, str)
        else request
    )
    ensure_decision_allowed(request_record, canonical_request)
    if blocked_timeout_seconds is not None:
        if decision_source != "foreground_countdown":
            raise ResumeValidationError(
                "foreground review decisions require foreground_countdown "
                "decision source"
            )
        if isinstance(blocked_timeout_seconds, bool) or not isinstance(
            blocked_timeout_seconds, (int, float)
        ):
            raise ResumeValidationError(
                "foreground review decisions require a numeric countdown timeout"
            )
        configured_seconds = _review_request_max_blocked_seconds(request_record)
        if blocked_timeout_seconds != configured_seconds:
            raise ResumeValidationError(
                "foreground countdown timeout does not match the verified "
                "review request"
            )
        if canonical_request.decided_by == "human":
            raise ResumeValidationError(
                "foreground review countdown requires an automatic decision origin"
            )
        _validate_foreground_automatic_request(
            active_interrupt,
            canonical_request,
            configured_seconds=configured_seconds,
        )
    elif canonical_request.decided_by == "auto_rule":
        raise ResumeValidationError(
            "auto_rule decisions are only valid in the foreground countdown path"
        )

    final_request = canonical_request
    final_record = request_record
    if blocked_timeout_seconds is not None:
        timeout = _blocked_timeout_status(
            active_interrupt, blocked_timeout_seconds, now=now
        )
        if not timeout or not timeout.get("expired"):
            raise ResumeValidationError("foreground review countdown has not expired")
        if canonical_request.decided_by == "auto_rule":
            try:
                gate = get_review_gate(
                    canonical_request.stage,
                    canonical_request.gate_id,
                )
            except KeyError as exc:
                raise ResumeValidationError(
                    f"Unsupported timeout stage: {canonical_request.stage}"
                ) from exc
            normalizer = gate.load_timeout_request_normalizer()
            if normalizer is None:
                raise ResumeValidationError(
                    f"review gate {canonical_request.stage}/{gate.gate_id} has no "
                    "automatic timeout resolver"
                )
            normalized = normalizer(state, canonical_request, timeout)
            _active, normalized_record = _validated_resume_context(state, normalized)
            ensure_decision_allowed(normalized_record, normalized)
            final_request = normalized
            final_record = normalized_record
    try:
        return canonicalize_review_intent_request(
            state,
            final_request,
            request_record=final_record,
        )
    except ReviewApplicationError as exc:
        raise ResumeValidationError(str(exc)) from exc


def reject_unsupported_lifecycle_operation(operation: str) -> None:
    if operation in {"retry", "rerun", "rebuild", "rebuild-from"}:
        raise UnsupportedLifecycleOperationError(
            f"{operation} is not a resume operation and is not implemented in "
            "the current lifecycle contract."
        )


def _validated_resume_context(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> tuple[ActiveInterrupt, dict[str, Any]]:
    if state.get("session_id") != request.session_id:
        raise ResumeValidationError("resume session_id does not match blocked state")
    if state.get("supervision_state") != "blocked_human":
        raise ResumeValidationError("resume requires supervision_state blocked_human")
    active_interrupt = state.get("active_interrupt")
    if not active_interrupt:
        raise ResumeValidationError("resume requires an active_interrupt")
    if active_interrupt.get("stage") != request.stage:
        raise ResumeValidationError(
            "resume stage does not match active_interrupt.stage"
        )
    _validate_active_interrupt_workflow_step(state, active_interrupt)
    active_gate_id = active_interrupt.get("gate_id")
    if (
        active_gate_id == "parameter_approval"
        and request.gate_id != "parameter_approval"
    ):
        raise ResumeValidationError(
            "parameter approval resume requires explicit gate_id=parameter_approval"
        )
    if request.gate_id and active_gate_id and request.gate_id != active_gate_id:
        raise ResumeValidationError(
            "resume gate_id does not match active_interrupt.gate_id"
        )
    interrupt_request_id = active_interrupt.get("review_request_id")
    interrupt_request_hash = active_interrupt.get("request_hash")
    interrupt_request_ref = active_interrupt.get("request_ref")
    missing = [
        key
        for key, value in (
            ("review_request_id", interrupt_request_id),
            ("request_hash", interrupt_request_hash),
            ("request_ref", interrupt_request_ref),
        )
        if not value
    ]
    if missing:
        raise ResumeValidationError(
            "v2 resume requires compact request identity fields: " + ", ".join(missing)
        )
    if request.review_request_id is None or request.review_request_hash is None:
        raise ResumeValidationError(
            "v2 resume decision requires review_request_id and review_request_hash"
        )
    if request.review_request_id != interrupt_request_id:
        raise ResumeValidationError(
            "resume review_request_id does not match active_interrupt.review_request_id"
        )
    if request.review_request_hash != interrupt_request_hash:
        raise ResumeValidationError(
            "resume review_request_hash does not match active_interrupt.request_hash"
        )
    _request_ref, request_record = load_verified_interrupt_request(
        state,
        active_interrupt,
    )
    return active_interrupt, request_record


def _review_request_max_blocked_seconds(request_record: Mapping[str, Any]) -> int:
    payload = request_record.get("payload")
    value = payload.get("max_blocked_seconds") if isinstance(payload, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < -1:
        raise ResumeValidationError(
            "verified review request max_blocked_seconds must be an integer >= -1"
        )
    return value


def _validate_foreground_automatic_request(
    active_interrupt: Mapping[str, Any],
    request: ResumeReviewRequest,
    *,
    configured_seconds: int,
) -> None:
    request_ref = active_interrupt.get("request_ref")
    try:
        expected_rationale = ArtifactRef.model_validate(request_ref)
    except (TypeError, ValueError):
        expected_rationale = None
    metadata = request.metadata
    elapsed_seconds = metadata.get("elapsed_seconds")
    if (
        metadata.get("source") != "foreground_countdown"
        or metadata.get("configured_seconds") != configured_seconds
        or not isinstance(elapsed_seconds, (int, float))
        or isinstance(elapsed_seconds, bool)
        or elapsed_seconds < configured_seconds
    ):
        raise ResumeValidationError(
            "automatic decision is not bound to the verified foreground countdown"
        )
    if (
        request.decided_by == "auto_rule"
        and request.rationale_artifact_ref != expected_rationale
    ):
        raise ResumeValidationError(
            "auto_rule decision is not bound to the verified review request"
        )


def _stage_statuses(state: Mapping[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for stage, output in (state.get("stage_outputs") or {}).items():
        if isinstance(output, dict):
            statuses[stage] = str(output.get("status", "unknown"))
    return statuses


def _checkpoint_workflow_progress(
    state: Mapping[str, Any],
    active_interrupt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reduce only explicit in-checkpoint occurrence facts.

    A detached checkpoint does not retain the durable prefix of stage terminal
    events, so it cannot prove the cursor's implied completed occurrences.  An
    in-flight ``workflow_step_outcome`` is still an explicit fact and is
    included as such, but the reducer is deliberately marked degraded and
    withholds a current occurrence.  Durable :func:`inspect_session` supplies
    the complete Ledger terminal map and is authoritative.
    """

    outcome = state.get("workflow_step_outcome")
    stage_terminals: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(outcome, Mapping):
        step_id = outcome.get("workflow_step_id")
        stage = outcome.get("stage")
        terminal_outcome = outcome.get("outcome")
        event_type_by_outcome = {
            "completed": "stage_completed",
            "skipped": "stage_skipped",
            "failed": "stage_failed",
        }
        event_type = (
            event_type_by_outcome.get(terminal_outcome)
            if isinstance(terminal_outcome, str)
            else None
        )
        if isinstance(step_id, str) and isinstance(stage, str) and event_type:
            stage_terminals[(step_id, stage)] = {"event_type": event_type}

    checkpoint_only = {
        "kind": "checkpoint_only_stage_terminal_prefix",
        "detail": (
            "checkpoint state does not contain the durable occurrence-terminal "
            "prefix required to determine current workflow progress"
        ),
    }
    try:
        reduction = reduce_workflow_progress(
            state.get("workflow_steps"),
            stage_terminals=stage_terminals,
            active_gate=(
                active_interrupt
                if state.get("supervision_state") == "blocked_human"
                and isinstance(active_interrupt, Mapping)
                else None
            ),
            session_outcome=(
                "completed"
                if state.get("supervision_state") == "completed"
                else "failed"
                if state.get("supervision_state") == "failed_terminal"
                else None
            ),
            terminal_integrity=(checkpoint_only,),
        )
    except (TypeError, ValueError):
        return {
            "plan_source": "unavailable",
            "total_steps": None,
            "completed_steps": None,
            "skipped_steps": None,
            "failed_steps": None,
            "blocked_steps": None,
            "unfinished_steps": None,
            "current_workflow_step_id": None,
            "current_stage": None,
            "projection_authority": "unavailable",
            "integrity_diagnostics": [checkpoint_only],
        }
    return {
        "plan_source": "checkpoint_only",
        **reduction,
    }


def _validate_active_interrupt_workflow_step(
    state: Mapping[str, Any],
    active_interrupt: Mapping[str, Any],
) -> str:
    """Require a blocked request to target exactly the current frozen occurrence."""

    interrupt_step_id = active_interrupt.get("workflow_step_id")
    if not isinstance(interrupt_step_id, str) or not interrupt_step_id:
        raise ResumeValidationError("resume requires active_interrupt.workflow_step_id")
    try:
        current_step_id = current_workflow_step_id(
            cast(TopLevelState, state),
            expected_stage=str(active_interrupt.get("stage") or ""),
        )
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "resume requires a valid frozen workflow step for active_interrupt"
        ) from exc
    if interrupt_step_id != current_step_id:
        raise ResumeValidationError(
            "active_interrupt.workflow_step_id does not match the current frozen "
            "workflow step"
        )
    return current_step_id


def _latest_artifact_refs(state: Mapping[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("asset_registry_ref", "stage_plan_ref"):
        if state.get(key):
            refs[key] = state[key]
    for stage, output in (state.get("stage_outputs") or {}).items():
        if not isinstance(output, dict):
            continue
        stage_refs = {
            key: value
            for key, value in output.items()
            if key.endswith("_ref") or key.endswith("_artifact_ref")
        }
        if stage_refs:
            refs[stage] = stage_refs
    return refs


def _review_request_decision_context(
    record: Mapping[str, Any],
    *,
    request_ref: Any,
    request_hash: Any,
    review_request_artifact_ref: Any,
) -> dict[str, Any]:
    """Project one verified request record into the decision-context contract."""

    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("review request artifact payload is invalid")
    return {
        **payload,
        **dict(record.get("bindings") or {}),
        "stage": record.get("stage"),
        "workflow_step_id": record.get("workflow_step_id"),
        "gate_id": record.get("gate_id"),
        "review_request_id": record.get("review_request_id"),
        "request_ref": request_ref,
        "request_hash": request_hash,
        "review_request_artifact_ref": review_request_artifact_ref,
    }


def _decision_requirements_from_state(
    state: Mapping[str, Any],
    active_interrupt: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not active_interrupt:
        return None
    _validate_active_interrupt_workflow_step(state, active_interrupt)
    _ref, record = load_verified_interrupt_request(state, active_interrupt)
    context = _review_request_decision_context(
        record,
        request_ref=active_interrupt.get("request_ref"),
        request_hash=active_interrupt.get("request_hash"),
        review_request_artifact_ref=(
            (active_interrupt.get("request_ref") or {}).get("path_ref")
            if isinstance(active_interrupt.get("request_ref"), dict)
            else None
        ),
    )
    return _decision_requirements(context)


def _decision_requirements(
    active_interrupt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not active_interrupt:
        return None
    stage = active_interrupt.get("stage")
    gate = find_review_gate(stage, active_interrupt.get("gate_id"))
    decisions_allowed = active_interrupt.get("decisions_allowed")
    if decisions_allowed is None and gate is not None:
        decisions_allowed = list(gate.decisions_allowed)

    builder = gate.load_interrupt_context_builder() if gate is not None else None
    if builder is not None:
        extra = builder(active_interrupt)
    else:
        extra = generic_interrupt_context(active_interrupt)

    return _with_review_requirements(
        active_interrupt,
        {
            "stage": stage,
            "gate_id": active_interrupt.get("gate_id")
            or (gate.gate_id if gate else None),
            "decisions_allowed": decisions_allowed or [],
            **extra,
            "requires_structured_resume": True,
        },
    )


_PARAMETER_APPROVAL_BINDING_FIELDS = (
    "attempt_id",
    "task",
    "previous_attempt_id",
    "recommendation_hash",
)

_PARAMETER_APPROVAL_CONTEXT_FIELDS = (
    "recommendation_ref",
    "parameter_diff",
    "rationale",
    "failure_facts",
    "evidence_refs",
)


def _parameter_approval_resume_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("gate_id") != "parameter_approval":
        return {}
    return {
        key: payload.get(key)
        for key in _PARAMETER_APPROVAL_BINDING_FIELDS
        if key in payload
    }


def _runtime_proposal_requirements(gate: Any) -> dict[str, Any]:
    "Describe what this existing gate proposes without implying a recommender exists."

    proposal = {
        "kind": gate.proposal_kind,
        "label": gate.proposal_label or gate.gate_id.replace("_", " "),
        "parameter_recommendation_status": gate.parameter_recommendation_status,
        "human_confirmation_required": True,
    }
    if gate.related_parameter_gate is not None:
        proposal["related_parameter_gate"] = {
            "stage": gate.related_parameter_gate[0],
            "gate_id": gate.related_parameter_gate[1],
        }
    if gate.parameter_recommendation_notes:
        proposal["parameter_recommendation_notes"] = list(
            gate.parameter_recommendation_notes
        )
    return proposal


def _with_review_requirements(
    active_interrupt: dict[str, Any], requirements: dict[str, Any]
) -> dict[str, Any]:
    enriched = dict(requirements)
    gate = find_review_gate(
        active_interrupt.get("stage"), active_interrupt.get("gate_id")
    )
    if gate is not None:
        enriched["runtime_proposal"] = _runtime_proposal_requirements(gate)
    resume_metadata = _parameter_approval_resume_metadata(active_interrupt)
    if resume_metadata:
        enriched["resume_metadata"] = resume_metadata
        for key in _PARAMETER_APPROVAL_CONTEXT_FIELDS:
            if key in active_interrupt:
                enriched[key] = active_interrupt.get(key)
    for key in (
        "workflow_step_id",
        "auto_decidable",
        "max_blocked_seconds",
        "review_request_artifact_ref",
        "review_request_id",
        "request_ref",
        "request_hash",
    ):
        if key in active_interrupt:
            enriched[key] = active_interrupt.get(key)
    return enriched


def _blocked_timeout_status(
    active_interrupt: Mapping[str, Any] | None,
    blocked_timeout_seconds: float | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not active_interrupt or blocked_timeout_seconds is None:
        return None
    if blocked_timeout_seconds < 0:
        return {
            "configured_seconds": blocked_timeout_seconds,
            "expired": False,
            "reason": "wait_indefinitely",
        }
    blocked_at_raw = active_interrupt.get("blocked_at")
    if not blocked_at_raw:
        return {
            "configured_seconds": blocked_timeout_seconds,
            "expired": False,
            "reason": "blocked_at_missing",
        }
    try:
        blocked_at = datetime.fromisoformat(str(blocked_at_raw))
    except ValueError:
        return {
            "configured_seconds": blocked_timeout_seconds,
            "expired": False,
            "reason": "blocked_at_invalid",
        }
    if blocked_at.tzinfo is None:
        blocked_at = blocked_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed = (current - blocked_at).total_seconds()
    return {
        "configured_seconds": blocked_timeout_seconds,
        "elapsed_seconds": elapsed,
        "expired": elapsed >= blocked_timeout_seconds,
    }


def _durable_blocked_timeout_status(
    review_request_event: dict[str, Any],
    blocked_timeout_seconds: float,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    active_interrupt = {"blocked_at": review_request_event.get("timestamp")}
    timeout = _blocked_timeout_status(
        active_interrupt, blocked_timeout_seconds, now=now
    )
    return timeout or {
        "configured_seconds": blocked_timeout_seconds,
        "expired": False,
        "reason": "timestamp_missing",
    }


def _latest_review_lifecycle_event(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") in {
            "review_requested",
            "review_decision",
            "review_applied",
        }:
            return event
    return None


def _latest_event(
    events: list[dict[str, Any]], event_type: str
) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _latest_artifact(
    artifacts: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        if predicate(artifact):
            return artifact
    return None


def _artifact_for_ref(
    artifacts: list[dict[str, Any]],
    path_ref: Any,
) -> dict[str, Any] | None:
    if not path_ref:
        return None
    target = str(path_ref)
    for artifact in reversed(artifacts):
        candidate = artifact.get("path_ref") or artifact.get("file_path")
        if str(candidate) == target:
            return artifact
    return None


def _event_ref(event: dict[str, Any], role: str) -> dict[str, Any] | None:
    for ref in event.get("refs") or []:
        if isinstance(ref, dict) and ref.get("role") == role:
            return ref
    return None


def _latest_verified_assembly_delivery(
    events: list[dict[str, Any]],
    *,
    paths: SessionPaths,
    session_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Project the latest index-verified delivered assembly for a completed run."""

    for event in reversed(events):
        if event.get("event_type") != "resource_delivered":
            continue
        summary = event.get("summary")
        if (
            not isinstance(summary, Mapping)
            or summary.get("resource_type") != "assembly"
        ):
            continue
        candidate_event_ref = _event_ref(event, "candidate_bundle_ref")
        primary_event_ref = _event_ref(event, "primary_output_ref")
        try:
            candidate_ref = _artifact_ref_from_event_ref(candidate_event_ref)
            primary_ref = _artifact_ref_from_event_ref(primary_event_ref)
            candidate_path = resolve_artifact_ref(
                paths.run_dir, session_id, candidate_ref
            )
            primary_path = resolve_artifact_ref(paths.run_dir, session_id, primary_ref)
        except (OSError, ValueError) as exc:
            return None, f"delivered assembly artifact verification failed: {exc}"
        return (
            {
                "stage": event.get("stage"),
                "workflow_step_id": event.get("workflow_step_id"),
                "delivery_mode": summary.get("delivery_mode"),
                "candidate_bundle_ref": candidate_ref.model_dump(mode="json"),
                "candidate_bundle_path": str(candidate_path),
                "primary_output_ref": primary_ref.model_dump(mode="json"),
                "primary_output_path": str(primary_path),
            },
            None,
        )
    return None, None


def _artifact_ref_from_event_ref(value: object) -> ArtifactRef:
    if not isinstance(value, Mapping):
        raise ValueError("resource delivery is missing an artifact ref")
    return ArtifactRef.model_validate(
        {key: value[key] for key in ArtifactRef.model_fields if key in value}
    )


def _latest_relevant_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relevant_prefixes = (
        "assembly_review.",
        "hic_scaffolding.",
    )
    relevant: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in reversed(artifacts):
        artifact_type = str(artifact.get("artifact_type", ""))
        if not artifact_type.startswith(relevant_prefixes):
            continue
        if artifact_type in seen:
            continue
        relevant.append(artifact)
        seen.add(artifact_type)
        if len(relevant) >= 8:
            break
    return list(reversed(relevant))


def _terminal_supervision_state(outcome: Any) -> str:
    normalized = str(outcome or "unknown")
    if normalized in {"completed", "succeeded", "success"}:
        return "completed"
    if normalized in {"failed", "failed_terminal", "error"}:
        return "failed_terminal"
    return normalized


def _inspect_execution_read_model(
    paths: SessionPaths,
    *,
    stage_terminals: Mapping[tuple[str, str], Mapping[str, Any]],
    stage_terminal_integrity: list[dict[str, Any]],
    supervision_state: str,
    failure_scope: object,
    latest_unresolved_review_request: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project verified-plan progress plus per-session execution evidence.

    The frozen RunConfig is the sole denominator authority.  If it cannot be
    verified, inspection reports all plan-derived values as unavailable rather
    than reconstructing a plan from Ledger events or the stage registry.
    """

    from app.storage.frozen_inputs import (
        frozen_execution_lock_held,
        frozen_execution_progress,
        validate_frozen_inputs,
    )

    try:
        lock_held: bool | None = frozen_execution_lock_held(paths)
    except OSError:
        lock_held = None

    unavailable_progress = {
        "plan_source": "unavailable",
        "total_steps": None,
        "completed_steps": None,
        "skipped_steps": None,
        "failed_steps": None,
        "blocked_steps": None,
        "unfinished_steps": None,
        "current_workflow_step_id": None,
        "current_stage": None,
    }
    try:
        bundle = validate_frozen_inputs(paths)
        steps = workflow_steps_from_run_config(bundle.run_config)
    except (OSError, ValueError):
        return unavailable_progress, {
            "frozen_inputs_status": "invalid_or_missing",
            "durable_progress": "unavailable",
            "execution_lock_held": lock_held,
        }

    try:
        durable_progress: str = frozen_execution_progress(paths)
    except (OSError, ValueError):
        durable_progress = "unavailable"

    active_gate = (
        latest_unresolved_review_request
        if supervision_state == "blocked_human"
        else None
    )
    reduction = reduce_workflow_progress(
        steps,
        stage_terminals=stage_terminals,
        active_gate=active_gate,
        session_outcome=supervision_state,
        failure_scope=(failure_scope if isinstance(failure_scope, str) else None),
        terminal_integrity=stage_terminal_integrity,
    )
    return (
        {
            "plan_source": "frozen_run_config",
            **reduction,
        },
        {
            "frozen_inputs_status": "verified",
            "durable_progress": durable_progress,
            "execution_lock_held": lock_held,
        },
    )


def _durable_route_trace(events: list[dict[str, Any]]) -> list[str]:
    """Project durable route entries as the same step-qualified trace as state."""

    trace: list[str] = []
    for event in events:
        if event.get("event_type") != "stage_entered" or not event.get("stage"):
            continue
        step_id = event.get("workflow_step_id")
        node = event.get("route_node") or event.get("stage")
        if isinstance(step_id, str) and step_id:
            trace.append(f"{step_id}:{node}")
        else:
            trace.append(str(node))
    return trace[-ROUTE_TRACE_LIMIT:]


def _latest_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("error"):
            return str(event["error"])
        if event.get("detail") and event.get("event_type") == "env_preflight_blocked":
            return str(event["detail"])
    return None


def _durable_interrupt_projection(
    request: dict[str, Any] | None,
    requirements: dict[str, Any] | None,
    *,
    review_status: ReviewStatusProjection | None = None,
) -> dict[str, Any] | None:
    if request is None:
        return None
    projected = {
        "stage": request.get("stage"),
        "workflow_step_id": request.get("workflow_step_id"),
        "review_request_id": request.get("review_request_id")
        or request.get("subject_id"),
        "reason": request.get("reason"),
        "review_request_artifact_ref": request.get("review_request_artifact_ref"),
    }
    if requirements:
        projected["gate_id"] = requirements.get("gate_id")
        projected["decisions_allowed"] = list(
            requirements.get("decisions_allowed") or []
        )
    if review_status is not None:
        projected["review_status"] = review_status.status
        if review_status.integrity_error is not None:
            projected["integrity_error"] = review_status.integrity_error
    return {key: value for key, value in projected.items() if value is not None}


def build_next_action(
    summary: dict[str, Any], *, data_dir: str | Path = "data"
) -> dict[str, Any]:
    if summary.get("error"):
        return {
            "kind": "inspect_unavailable",
            "summary": (
                f"No durable ledger session found for {summary.get('session_id')!r}."
            ),
            "required_fields": [],
            "relevant_refs": [],
        }

    review_status = summary.get("latest_review_status")
    if (
        summary.get("supervision_state") == "blocked_human"
        and isinstance(review_status, Mapping)
        and review_status.get("integrity_error")
    ):
        return {
            "kind": "review_integrity_error",
            "summary": str(review_status["integrity_error"]),
            "stage": review_status.get("stage"),
            "review_status": review_status.get("status"),
            "required_fields": [],
            "relevant_refs": _artifact_refs(
                review_status.get("intent_ref"),
                review_status.get("application_ref"),
                summary.get("latest_review_request_artifact"),
            ),
        }
    if (
        summary.get("supervision_state") == "blocked_human"
        and isinstance(review_status, Mapping)
        and review_status.get("requires_recovery") is True
    ):
        abs_data_dir = str(Path(data_dir).expanduser().resolve(strict=False))
        session_id = summary.get("session_id")
        return {
            "kind": "review_recovery_required",
            "summary": (
                "A review decision is already committed; recover that exact "
                "intent instead of submitting a different decision."
            ),
            "stage": review_status.get("stage"),
            "review_status": review_status.get("status"),
            "required_fields": [],
            "command": (
                "gene-agent resume "
                f"--session-id {session_id} --data-dir {abs_data_dir} "
                "--checkpointer sqlite"
            ),
            "relevant_refs": _artifact_refs(
                review_status.get("intent_ref"),
                review_status.get("application_ref"),
            ),
        }

    requirements = summary.get("decision_requirements")
    if requirements:
        return _review_gate_next_action(summary, data_dir=data_dir)

    if summary.get("supervision_state") == "completed":
        refs = _artifact_refs(
            summary.get("latest_final_report"),
        )
        return {
            "kind": "completed",
            "summary": ("Workflow completed; inspect the listed final report."),
            "required_fields": [],
            "relevant_refs": refs,
        }

    env_block = summary.get("latest_env_preflight_block")
    if env_block:
        return _env_block_next_action(env_block)

    if summary.get("supervision_state") == "failed_terminal":
        return _failed_terminal_next_action(summary)

    return {
        "kind": "unknown",
        "summary": (
            "No active operator action was inferred from durable ledger and artifact "
            "records."
        ),
        "required_fields": [],
        "relevant_refs": _artifact_refs(*summary.get("latest_relevant_artifacts", [])),
    }


def build_result_next_action(result: dict[str, Any]) -> dict[str, Any]:
    supervision = result.get("supervision_state")
    active_interrupt = result.get("active_interrupt")
    if supervision == "blocked_human" and active_interrupt:
        try:
            requirements = _decision_requirements_from_state(result, active_interrupt)
        except ResumeValidationError as exc:
            return {
                "kind": "review_request_invalid",
                "summary": str(exc),
                "stage": active_interrupt.get("stage"),
                "required_fields": [
                    "review_request_id",
                    "review_request_hash",
                    "request_ref",
                ],
                "relevant_refs": _artifact_refs(active_interrupt.get("request_ref")),
            }
        checkpoint = result.get("checkpoint_persistence")
        summary = {
            "session_id": result.get("session_id"),
            "decision_requirements": requirements,
            "durable_resume_available": checkpoint != "memory",
        }
        return _review_gate_next_action(
            summary, data_dir=result.get("data_dir", "data")
        )
    if supervision == "failed_terminal":
        missing_input = _result_missing_input(result)
        if missing_input:
            return _missing_input_next_action(missing_input)
        env_block = _result_env_block(result)
        if env_block:
            return _env_block_next_action(env_block)
        return _failed_terminal_next_action(
            {
                "supervision_state": supervision,
                "latest_operator_report_payload": {
                    "current_stage": result.get("current_stage"),
                    "last_error": result.get("last_error"),
                    "relevant_refs": _result_relevant_refs(result),
                },
            }
        )
    return {
        "kind": "unknown",
        "summary": "No active operator action was inferred from the workflow result.",
        "required_fields": [],
        "relevant_refs": [],
    }


def build_resume_decision_template(summary: dict[str, Any]) -> dict[str, Any]:
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    gate_id = requirements.get("gate_id")
    if not isinstance(stage, str):
        raise ResumeValidationError(
            "no active registered review decision requirements; registered stages: "
            f"{registered_review_stage_label()}"
        )
    try:
        gate = get_review_gate(stage, gate_id)
    except KeyError as exc:
        raise ResumeValidationError(
            "no active registered review decision requirements; registered stages: "
            f"{registered_review_stage_label()}"
        ) from exc

    decisions_allowed = list(requirements.get("decisions_allowed") or [])
    decision = _preferred_template_decision(gate, decisions_allowed)
    recommended_route = requirements.get("recommended_route")
    recommended_route_decision = (
        f"select_{recommended_route}" if isinstance(recommended_route, str) else None
    )
    if (
        gate.normalize_timeout_request
        and recommended_route_decision in decisions_allowed
    ):
        decision = str(recommended_route_decision)
    template: dict[str, Any] = {
        "session_id": summary.get("session_id"),
        "stage": stage,
        "gate_id": gate.gate_id,
        "review_request_id": requirements.get("review_request_id"),
        "review_request_hash": requirements.get("request_hash"),
        "decision": decision,
        "notes": ["replace this note with the operator rationale"],
        "metadata": dict(requirements.get("resume_metadata") or {}),
        "decided_by": "human",
        "rationale_artifact_ref": None,
    }
    required_fields = set(gate.required_resume_fields_for(decision))
    if requirements.get("candidate_ids"):
        candidate_ids = list(requirements.get("candidate_ids") or [])
        if "candidate_ids" in required_fields:
            template["candidate_ids"] = candidate_ids
        else:
            selected_candidate = candidate_ids[0] if candidate_ids else "<candidate_id>"
            template["candidate_id"] = selected_candidate
            template["rejected_candidates"] = []
    if requirements.get("candidate_methods"):
        candidate_methods = list(requirements.get("candidate_methods") or [])
        template["method_name"] = (
            candidate_methods[0] if candidate_methods else "<method_name>"
        )
    recommended_selection = requirements.get("recommended_assembly_selection")
    if gate.normalize_timeout_request and isinstance(recommended_selection, dict):
        recommended_method = recommended_selection.get("primary_method")
        if recommended_method:
            template["method_name"] = str(recommended_method)
    return template


def review_max_blocked_seconds(summary: dict[str, Any]) -> int:
    """Return the verified per-request foreground review wait."""

    requirements = summary.get("decision_requirements") or {}
    value = requirements.get("max_blocked_seconds")
    if not isinstance(value, int) or isinstance(value, bool) or value < -1:
        raise ResumeValidationError(
            "verified review request max_blocked_seconds must be an integer >= -1"
        )
    return value


def foreground_automatic_decision_label(
    summary: dict[str, Any],
) -> str | None:
    """Describe the gate's existing automatic resolver, if one is eligible."""

    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    gate_id = requirements.get("gate_id")
    if not isinstance(stage, str):
        return None
    gate = find_review_gate(stage, gate_id if isinstance(gate_id, str) else None)
    if gate is None:
        return None
    if gate.normalize_timeout_request:
        return str(build_resume_decision_template(summary).get("decision") or "rule")
    if gate.prompt_bundle and requirements.get("auto_decidable") is True:
        return "LLM review recommendation"
    return None


def build_foreground_auto_resume_request(
    summary: dict[str, Any],
    *,
    configured_seconds: int,
    elapsed_seconds: float,
    data_dir: str | Path | None = None,
) -> ResumeReviewRequest:
    """Run the gate's existing automatic resolver after countdown expiry."""

    if configured_seconds < 0:
        raise ResumeValidationError(
            "an indefinite review wait cannot build an automatic decision"
        )
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    gate_id = requirements.get("gate_id")
    if not isinstance(stage, str):
        raise ResumeValidationError("foreground review gate is not registered")
    try:
        gate = get_review_gate(
            stage,
            gate_id if isinstance(gate_id, str) else None,
        )
    except KeyError as exc:
        raise ResumeValidationError("foreground review gate is not registered") from exc

    countdown_metadata = {
        "source": "foreground_countdown",
        "configured_seconds": configured_seconds,
        "elapsed_seconds": elapsed_seconds,
    }
    if gate.prompt_bundle and requirements.get("auto_decidable") is True:
        if data_dir is None:
            raise ResumeValidationError(
                "LLM review recommendation requires the frozen session data directory"
            )
        review_request_id = requirements.get("review_request_id")
        request_ref = requirements.get("request_ref")
        request_hash = requirements.get("request_hash")
        if (
            not isinstance(review_request_id, str)
            or not review_request_id
            or not isinstance(request_ref, Mapping)
            or not isinstance(request_hash, str)
            or not request_hash
        ):
            raise ResumeValidationError(
                "LLM review recommendation requires a verified immutable request"
            )
        from app.review.recommender import (
            RecommendationError,
            recommend_review_decision,
        )

        try:
            recommendation = recommend_review_decision(
                session_id=str(summary.get("session_id") or ""),
                stage=stage,
                review_request_id=review_request_id,
                request_ref=request_ref,
                request_hash=request_hash,
                data_dir=data_dir,
            )
        except (RecommendationError, ValueError) as exc:
            raise ResumeValidationError(
                f"automatic review recommendation failed: {exc}"
            ) from exc
        return recommendation.model_copy(
            update={
                "notes": [
                    *recommendation.notes,
                    "foreground review countdown expired",
                ],
                "metadata": {
                    **recommendation.metadata,
                    **countdown_metadata,
                },
            }
        )

    if not gate.normalize_timeout_request:
        raise ResumeValidationError(
            f"review gate {stage}/{gate.gate_id} has no eligible automatic resolver"
        )

    template = build_resume_decision_template(summary)
    template_decision = str(template.get("decision") or "")
    for field_name in gate.required_resume_fields_for(template_decision):
        field_value = template.get(field_name)
        if not field_value or (
            isinstance(field_value, str)
            and field_value.startswith("<")
            and field_value.endswith(">")
        ):
            raise ResumeValidationError(
                "foreground automatic resolver request is missing required field "
                f"{field_name!r}"
            )
    rationale_ref = requirements.get("request_ref")
    try:
        rationale_artifact_ref = ArtifactRef.model_validate(rationale_ref)
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "foreground automatic resolver requires the verified review request "
            "artifact"
        ) from exc
    payload = {
        **template,
        "notes": ["accepted by the foreground review countdown"],
        "metadata": {
            **dict(template.get("metadata") or {}),
            **countdown_metadata,
        },
        "decided_by": "auto_rule",
        "rationale_artifact_ref": rationale_artifact_ref.model_dump(mode="json"),
    }
    try:
        return ResumeReviewRequest.model_validate(payload)
    except Exception as exc:
        raise ResumeValidationError(
            f"foreground automatic resolver request failed validation: {exc}"
        ) from exc


def _preferred_template_decision(
    gate: ReviewGateDeclaration,
    decisions_allowed: list[str],
) -> str:
    if gate.template_preferred_decision in decisions_allowed:
        return str(gate.template_preferred_decision)
    return (
        decisions_allowed[0]
        if decisions_allowed
        else (gate.template_preferred_decision or gate.decisions_allowed[0])
    )


def _review_gate_next_action(
    summary: dict[str, Any], *, data_dir: str | Path
) -> dict[str, Any]:
    session_id = summary.get("session_id")
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage", "review")
    abs_data_dir = str(Path(data_dir).expanduser().resolve(strict=False))
    template_hint = str(
        Path(abs_data_dir) / "decisions" / f"{session_id}_{stage}_decision.json"
    )
    command = (
        "gene-agent inspect "
        f"--session-id {session_id} --data-dir {abs_data_dir} "
        f"--write-decision-template {template_hint}"
    )
    resume_command = (
        "gene-agent resume "
        f"--session-id {session_id} --data-dir {abs_data_dir} "
        f"--decision-file {template_hint} --checkpointer sqlite"
    )
    if not summary.get("durable_resume_available"):
        resume_summary = (
            "Durable resume is unavailable until the session has a sqlite "
            "checkpoint database."
        )
    else:
        resume_summary = (
            f"Human review is required for {stage}; write and edit a decision file, "
            "then resume."
        )
    fields = [
        "session_id",
        "stage",
        "review_request_id",
        "review_request_hash",
        "decision",
        "decided_by",
    ]
    gate = find_review_gate(stage, requirements.get("gate_id"))
    if gate is not None:
        decisions_allowed = list(requirements.get("decisions_allowed") or [])
        preferred = _preferred_template_decision(gate, decisions_allowed)
        fields.extend(
            field
            for field in gate.required_resume_fields_for(preferred)
            if field not in fields
        )
    return {
        "kind": "review_decision_required",
        "summary": resume_summary,
        "stage": stage,
        "decisions_allowed": list(requirements.get("decisions_allowed") or []),
        "candidate_ids": list(requirements.get("candidate_ids") or []),
        "candidate_methods": list(requirements.get("candidate_methods") or []),
        "required_fields": fields,
        "decision_template_path_hint": template_hint,
        "command": command,
        "resume_command": resume_command,
        "relevant_refs": _artifact_refs(
            summary.get("latest_review_request_artifact"),
            requirements.get("request_ref"),
            requirements.get("report_refs"),
            _candidate_comparison_relevant_refs(
                requirements.get("candidate_comparison_context")
            ),
        ),
    }


def _env_block_next_action(event: dict[str, Any]) -> dict[str, Any]:
    status = event.get("status")
    kind = "missing_tool" if status == "missing_tool" else "blocked_env"
    missing_tools = list(event.get("missing_tools") or [])
    env_target = event.get("env_target")
    detail = event.get("detail") or "environment preflight failed"
    if kind == "missing_tool":
        summary = (
            f"Install or expose required tool(s) in {env_target}: "
            f"{', '.join(missing_tools)}."
        )
    else:
        summary = f"Fix the configured execution environment {env_target}: {detail}."
    return {
        "kind": kind,
        "summary": summary,
        "stage": event.get("stage"),
        "method_name": event.get("method_name"),
        "env_target": env_target,
        "missing_tools": missing_tools,
        "detail": detail,
        "required_fields": [],
        "relevant_refs": [],
    }


def _result_stage_output(result: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    current_stage = result.get("current_stage")
    stage_outputs = result.get("stage_outputs") or {}
    if not isinstance(current_stage, str) or not isinstance(stage_outputs, dict):
        return None
    output = stage_outputs.get(current_stage)
    if not isinstance(output, dict):
        return None
    return current_stage, output


def _result_missing_input(result: dict[str, Any]) -> dict[str, Any] | None:
    stage_output = _result_stage_output(result)
    if not stage_output:
        return None
    current_stage, output = stage_output
    if output.get("status") != "blocked_input":
        return None
    return {
        "stage": current_stage,
        "blocked_input_ref": output.get("blocked_input_ref"),
        "detail": output.get("reason") or result.get("last_error"),
    }


def _result_env_block(result: dict[str, Any]) -> dict[str, Any] | None:
    stage_output = _result_stage_output(result)
    if not stage_output:
        return None
    current_stage, output = stage_output
    if output.get("status") not in {"missing_tool", "blocked_env"}:
        return None
    return {
        "stage": current_stage,
        "method_name": output.get("method_name"),
        "status": output.get("status"),
        "env_target": output.get("env_target"),
        "missing_tools": output.get("missing_tools") or [],
        "detail": output.get("reason"),
    }


def _missing_input_next_action(block: dict[str, Any]) -> dict[str, Any]:
    blocked_ref = block.get("blocked_input_ref")
    detail = block.get("detail") or "required input is missing"
    if blocked_ref:
        summary = (
            "Provide or correct the missing input path, then rerun with the original "
            f"manifest: {blocked_ref}."
        )
    else:
        summary = (
            "Provide or correct the missing input referenced by the manifest: "
            f"{detail}."
        )
    return {
        "kind": "missing_input",
        "summary": summary,
        "stage": block.get("stage"),
        "blocked_input_ref": blocked_ref,
        "detail": detail,
        "required_fields": ["manifest asset path"],
        "relevant_refs": [str(blocked_ref)] if blocked_ref else [],
    }


def _result_relevant_refs(result: dict[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("asset_registry_ref", "stage_plan_ref"):
        if result.get(key):
            refs[key] = result[key]
    current_stage = result.get("current_stage")
    stage_outputs = result.get("stage_outputs") or {}
    if isinstance(current_stage, str) and isinstance(stage_outputs, dict):
        output = stage_outputs.get(current_stage)
        if isinstance(output, dict):
            refs[current_stage] = {
                key: value
                for key, value in output.items()
                if key.endswith("_ref") or key.endswith("_artifact_ref")
            }
    return refs


def _report_missing_input(payload: dict[str, Any]) -> dict[str, Any] | None:
    refs = payload.get("relevant_refs")
    if not isinstance(refs, dict):
        return None
    stage = payload.get("current_stage")
    stage_refs = refs.get(stage) if stage else None
    if not isinstance(stage_refs, dict):
        return None
    blocked_ref = stage_refs.get("blocked_input_ref")
    if not blocked_ref:
        return None
    return {
        "stage": stage,
        "blocked_input_ref": blocked_ref,
        "detail": payload.get("last_error"),
    }


def _failed_terminal_next_action(summary: dict[str, Any]) -> dict[str, Any]:
    payload = (
        summary.get("latest_operator_report_payload")
        or summary.get("latest_terminal_event")
        or {}
    )
    last_error = str(
        payload.get("last_error") or summary.get("last_error") or "unknown failure"
    )
    stage = (
        payload.get("current_stage")
        or payload.get("stage")
        or summary.get("current_stage")
    )
    missing_input = _report_missing_input(payload)
    if missing_input:
        return _missing_input_next_action(missing_input)
    return {
        "kind": "failed_terminal",
        "summary": last_error,
        "stage": stage,
        "last_error": last_error,
        "required_fields": [],
        "relevant_refs": _artifact_refs(
            payload.get("relevant_refs"),
            summary.get("latest_relevant_artifacts"),
        ),
    }


def _candidate_comparison_relevant_refs(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return {
        key: context[key] for key in ("candidate_comparison_ref",) if context.get(key)
    }


def _artifact_refs(*values: Any) -> list[str]:
    refs: list[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, list):
            refs.extend(_artifact_refs(*value))
        elif isinstance(value, dict):
            path_ref = value.get("path_ref") or value.get("file_path")
            if path_ref:
                refs.append(str(path_ref))
            elif value.get("artifact"):
                refs.extend(_artifact_refs(value.get("artifact")))
            elif value.get("relevant_refs"):
                refs.extend(_artifact_refs(value.get("relevant_refs")))
            else:
                refs.extend(_artifact_refs(*value.values()))
        else:
            refs.append(str(value))
    return list(dict.fromkeys(refs))


def _decision_requirements_from_artifact(
    artifact: dict[str, Any] | None,
    *,
    paths: SessionPaths,
    expected_request_id: str,
    expected_request_hash: str | None = None,
    expected_workflow_step_id: str | None = None,
) -> dict[str, Any] | None:
    """Load requirements only after validating the indexed immutable ref."""
    if not artifact:
        raise ResumeValidationError("review request artifact is missing from the index")
    path_ref = artifact.get("path_ref")
    sha256 = artifact.get("sha256")
    size_bytes = artifact.get("size_bytes")
    artifact_id = artifact.get("artifact_id")
    artifact_type = artifact.get("artifact_type")
    try:
        ref = ArtifactRef.model_validate(
            {
                "schema_version": 2,
                "artifact_id": artifact_id,
                "path_ref": path_ref,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "artifact_type": artifact_type,
            }
        )
    except Exception as exc:
        raise ResumeValidationError(
            "review request artifact index ref is invalid"
        ) from exc
    if expected_request_hash and ref.sha256 != expected_request_hash:
        raise ResumeValidationError(
            "ledger review request hash does not match the artifact index"
        )
    try:
        record = load_review_request(
            paths.run_dir,
            ref,
            session_id=paths.session_id,
        )
    except ReviewPersistenceError as exc:
        raise ResumeValidationError(
            f"review request verification failed: {exc}"
        ) from exc
    if record.get("review_request_id") != expected_request_id:
        raise ResumeValidationError(
            "ledger review_request_id does not match the request artifact"
        )
    if (
        expected_workflow_step_id
        and record.get("workflow_step_id") != expected_workflow_step_id
    ):
        raise ResumeValidationError(
            "ledger workflow_step_id does not match the request artifact"
        )
    context = _review_request_decision_context(
        record,
        request_ref=ref.model_dump(mode="json"),
        request_hash=ref.sha256,
        review_request_artifact_ref=ref.path_ref,
    )
    return _decision_requirements(context)
