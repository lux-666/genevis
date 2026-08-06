"""Single control-layer transaction for durable review resume."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from langchain_core.runnables import RunnableConfig

from app.control.errors import DurableResumeUnavailableError
from app.control.lifecycle import (
    ResumeValidationError,
    UnsupportedLifecycleOperationError,
    finalize_terminal_state,
    resume_review_state,
    validate_resume_review_state,
)
from app.control.workflow_plan import current_workflow_step_id
from app.review.base import ResumeReviewRequest
from app.schemas.state import validate_top_level_state
from app.storage.artifacts import read_artifact_records
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


@dataclass(frozen=True)
class ResumeTransactionResult:
    """Completed resume state plus durable side-effect references."""

    state: dict[str, Any]
    decision_artifact_path: Path | None


class ResumeTransactionError(RuntimeError):
    """A resume transaction failed at a named lifecycle phase."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase


def execute_resume_transaction(
    *,
    graph: Any,
    config: RunnableConfig,
    session_id: str,
    data_dir: str | Path,
    request: ResumeReviewRequest,
    source: str,
    checkpointer: str,
    blocked_timeout_seconds: float | None = None,
    event_context: Mapping[str, Any] | None = None,
) -> ResumeTransactionResult:
    """Validate, apply, checkpoint, invoke, report, and audit one resume.

    Entry points own only operator I/O and presentation.  All stateful resume
    phases and their ledger events pass through this transaction.
    """

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    decision_path: Path | None = None
    workflow_step_id: str | None = None
    payload = {
        **dict(event_context or {}),
        "source": source,
        "checkpointer": checkpointer,
        "data_dir": str(paths.run_dir),
        "max_blocked_seconds": blocked_timeout_seconds,
        "decision": _decision_event_payload(request),
    }
    if request.rationale_artifact_ref:
        payload["rationale_artifact_ref"] = request.rationale_artifact_ref
    started = False

    def record_started() -> None:
        nonlocal started
        if started:
            return
        ledger.record_event(
            session_id,
            "resume_started",
            payload,
            stage=request.stage if workflow_step_id else None,
            workflow_step_id=workflow_step_id,
            subject_id=request.review_request_id,
        )
        started = True

    try:
        snapshot = graph.get_state(config)
        if (
            snapshot is None
            or not snapshot.values
            or snapshot.values.get("session_id") != session_id
        ):
            raise ResumeTransactionError(
                "snapshot",
                f"no checkpoint found for session_id {session_id!r}",
            )
        state = validate_top_level_state(snapshot.values)
        try:
            workflow_step_id = current_workflow_step_id(
                state, expected_stage=request.stage
            )
        except (TypeError, ValueError) as exc:
            raise ResumeTransactionError(
                "validation",
                f"invalid frozen workflow step for resume: {exc}",
            ) from exc
        decision_path = _decision_artifact_path(paths, request, workflow_step_id)
        payload["decision"] = _decision_event_payload(request, workflow_step_id)
        if decision_path is not None:
            payload["decision_artifact_ref"] = paths.to_ref(decision_path)
        record_started()
        if request.decided_by == "auto_rule" and source != "foreground_countdown":
            raise ResumeTransactionError(
                "validation",
                "auto_rule decisions require source='foreground_countdown'",
            )

        try:
            validate_resume_review_state(state, request)
        except (
            ResumeValidationError,
            DurableResumeUnavailableError,
            UnsupportedLifecycleOperationError,
        ) as exc:
            raise ResumeTransactionError(
                "validation", f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            update = resume_review_state(
                state,
                request,
                blocked_timeout_seconds=blocked_timeout_seconds,
                decision_source=source,
            )
        except (
            ResumeValidationError,
            DurableResumeUnavailableError,
            UnsupportedLifecycleOperationError,
        ) as exc:
            raise ResumeTransactionError(
                "validation", f"{type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:
            raise ResumeTransactionError(
                "decision_apply", f"{type(exc).__name__}: {exc}"
            ) from exc

        graph_update: dict[str, Any] = dict(update)
        try:
            update_result = graph.update_state(config, graph_update)
        except Exception as exc:
            raise ResumeTransactionError(
                "update_state", f"{type(exc).__name__}: {exc}"
            ) from exc
        if update_result is False:
            raise ResumeTransactionError("update_state", "update_state returned False")

        try:
            final = graph.invoke(None, config=config)
        except Exception as exc:
            raise ResumeTransactionError(
                "invoke", f"{type(exc).__name__}: {exc}"
            ) from exc

        try:
            finalize_terminal_state(
                graph,
                config,
                final,
                session_id,
                paths.run_dir,
                checkpointer,
                report_kind="final",
            )
        except Exception as exc:
            raise ResumeTransactionError(
                "terminal_report", f"{type(exc).__name__}: {exc}"
            ) from exc
    except ResumeTransactionError as exc:
        record_started()
        ledger.record_event(
            session_id,
            "resume_failed",
            {**payload, "phase": exc.phase, "error": str(exc)},
            stage=request.stage if workflow_step_id else None,
            workflow_step_id=workflow_step_id,
            subject_id=request.review_request_id,
        )
        raise
    except Exception as exc:
        failure = ResumeTransactionError("snapshot", f"{type(exc).__name__}: {exc}")
        record_started()
        ledger.record_event(
            session_id,
            "resume_failed",
            {**payload, "phase": failure.phase, "error": str(failure)},
            stage=request.stage if workflow_step_id else None,
            workflow_step_id=workflow_step_id,
            subject_id=request.review_request_id,
        )
        raise failure from exc

    ledger.record_event(
        session_id,
        "resume_completed",
        {
            **payload,
            "current_stage": final.get("current_stage"),
            "supervision_state": final.get("supervision_state"),
            "route_trace": final.get("route_trace", []),
        },
        stage=request.stage,
        workflow_step_id=workflow_step_id,
        subject_id=request.review_request_id,
    )
    return ResumeTransactionResult(
        state=final,
        decision_artifact_path=(
            decision_path
            if decision_path is not None and decision_path.is_file()
            else None
        ),
    )


def _decision_artifact_path(
    paths: SessionPaths,
    request: ResumeReviewRequest,
    workflow_step_id: str,
) -> Path | None:
    if not request.review_request_id:
        return None
    request_records = [
        record
        for record in read_artifact_records(paths.run_dir, paths.session_id)
        if record.get("stage") == request.stage
        and record.get("workflow_step_id") == workflow_step_id
        and record.get("review_request_id") == request.review_request_id
        and str(record.get("artifact_type") or "").endswith("review_request")
    ]
    if len(request_records) != 1:
        return None
    return paths.workflow_step_artifact_path(
        workflow_step_id,
        request.stage,
        "reviews",
        request.review_request_id,
        "decision.json",
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
