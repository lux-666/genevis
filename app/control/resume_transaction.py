"""Single control-layer transaction for durable review resume."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from langchain_core.runnables import RunnableConfig

from app.control.errors import DurableResumeUnavailableError
from app.control.lifecycle import (
    ResumeValidationError,
    UnsupportedLifecycleOperationError,
    finalize_terminal_state,
    resume_review_state,
    validate_resume_review_state,
)
from app.review.annotation_intake import (
    AnnotationIntakeSelection,
    maybe_collect_annotation_intake,
    persist_annotation_intake_plan,
)
from app.review.base import ResumeReviewRequest
from app.schemas.state import validate_top_level_state
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


@dataclass(frozen=True)
class ResumeTransactionResult:
    """Completed resume state plus durable side-effect references."""

    state: dict[str, Any]
    decision_artifact_path: Path | None
    annotation_intake_artifact_ref: str | None


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
    annotation_intake_selection: AnnotationIntakeSelection | None = None,
    annotation_intake_collector: Callable[..., AnnotationIntakeSelection | None]
    | None = None,
    collect_default_annotation_intake: bool = False,
    event_context: Mapping[str, Any] | None = None,
) -> ResumeTransactionResult:
    """Validate, apply, checkpoint, invoke, report, and audit one resume.

    Entry points own only operator I/O and presentation.  All stateful resume
    phases and their ledger events pass through this transaction.
    """

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    decision_path = _decision_artifact_path(paths, request)
    payload = {
        **dict(event_context or {}),
        "source": source,
        "checkpointer": checkpointer,
        "data_dir": str(paths.run_dir),
        "max_blocked_seconds": blocked_timeout_seconds,
        "decision": _decision_event_payload(request),
    }
    if decision_path is not None:
        payload["decision_artifact_ref"] = paths.to_ref(decision_path)
    if request.rationale_artifact_ref:
        payload["rationale_artifact_ref"] = request.rationale_artifact_ref

    ledger.record_event(session_id, "resume_started", payload)

    try:
        if request.decided_by == "auto_rule" and source != "foreground_countdown":
            raise ResumeTransactionError(
                "validation",
                "auto_rule decisions require source='foreground_countdown'",
            )
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
            validate_resume_review_state(state, request)
        except (
            ResumeValidationError,
            DurableResumeUnavailableError,
            UnsupportedLifecycleOperationError,
        ) as exc:
            raise ResumeTransactionError(
                "validation", f"{type(exc).__name__}: {exc}"
            ) from exc

        selection = annotation_intake_selection
        if annotation_intake_collector is not None:
            if selection is not None:
                raise ResumeTransactionError(
                    "annotation_intake",
                    "resume received both an intake selection and collector",
                )
            try:
                selection = annotation_intake_collector(
                    state=state,
                    request=request,
                )
            except Exception as exc:
                raise ResumeTransactionError(
                    "annotation_intake", f"{type(exc).__name__}: {exc}"
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
        intake = None
        try:
            if selection is not None:
                intake_state = validate_top_level_state({**state, **graph_update})
                intake = persist_annotation_intake_plan(
                    state=intake_state,
                    recommended_plan=selection.recommended_plan,
                    final_plan=selection.final_plan,
                    request=request,
                    source=source,
                )
            elif collect_default_annotation_intake:
                intake = maybe_collect_annotation_intake(
                    state=state,
                    resume_update=graph_update,
                    request=request,
                    source=source,
                    interactive=False,
                )
        except Exception as exc:
            raise ResumeTransactionError(
                "annotation_intake", f"{type(exc).__name__}: {exc}"
            ) from exc
        if intake is not None:
            graph_update.update(intake.update)

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
        ledger.record_event(
            session_id,
            "resume_failed",
            {**payload, "phase": exc.phase, "error": str(exc)},
        )
        raise
    except Exception as exc:
        failure = ResumeTransactionError("snapshot", f"{type(exc).__name__}: {exc}")
        ledger.record_event(
            session_id,
            "resume_failed",
            {**payload, "phase": failure.phase, "error": str(failure)},
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
    )
    return ResumeTransactionResult(
        state=final,
        decision_artifact_path=(
            decision_path
            if decision_path is not None and decision_path.is_file()
            else None
        ),
        annotation_intake_artifact_ref=(
            intake.artifact_ref if intake is not None else None
        ),
    )


def _decision_artifact_path(
    paths: SessionPaths,
    request: ResumeReviewRequest,
) -> Path | None:
    if not request.review_request_id:
        return None
    return paths.artifact_path("reviews", request.review_request_id, "decision.json")


def _decision_event_payload(request: ResumeReviewRequest) -> dict[str, Any]:
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
    return {key: value for key, value in payload.items() if value is not None}
