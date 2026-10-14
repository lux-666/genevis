"""Exception-isolated presentation boundary with no lifecycle authority."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.schemas.state import TopLevelState, WorkflowStep, validate_workflow_steps

PROJECTION_WARNING_MAX_CHARS = 512


@dataclass(frozen=True)
class ProgressProjectionInput:
    """Immutable scalar-only input available to an attached progress renderer."""

    session_id: str
    current_stage: str | None
    supervision_state: str
    workflow_step_id: str | None
    workflow_step_index: int
    entered_workflow_step_index: int
    total_workflow_steps: int
    completed_workflow_steps: int
    skipped_workflow_steps: int
    failed_workflow_steps: int
    blocked_workflow_steps: int
    projection_authority: str
    review_request_id: str | None
    gate_id: str | None
    route_trace_count: int
    review_note_count: int


@dataclass(frozen=True)
class ProjectionWarning:
    """Bounded operational diagnostic that cannot change workflow outcome."""

    name: str
    error_type: str
    message: str


@dataclass(frozen=True)
class ProjectionResult:
    """Result of one isolated presentation callback."""

    completed: bool
    skipped: bool = False
    warning: ProjectionWarning | None = None


class ProjectionBoundary:
    """Run projections against copies and convert ``Exception`` into warnings."""

    def __init__(self) -> None:
        self._disabled: set[str] = set()
        self._warnings: list[ProjectionWarning] = []

    @property
    def warnings(self) -> tuple[ProjectionWarning, ...]:
        return tuple(self._warnings)

    def run(
        self,
        name: str,
        callback: Callable[..., Any],
        *args: Any,
        disable_after_failure: bool = False,
        **kwargs: Any,
    ) -> ProjectionResult:
        """Invoke one callback without allowing ordinary failures to escape."""

        if name in self._disabled:
            return ProjectionResult(completed=False, skipped=True)
        try:
            copied_args = deepcopy(args)
            copied_kwargs = deepcopy(kwargs)
            callback(*copied_args, **copied_kwargs)
        except Exception as exc:
            warning = ProjectionWarning(
                name=_bounded(name, 128),
                error_type=_bounded(type(exc).__name__, 128),
                message=_bounded(str(exc), PROJECTION_WARNING_MAX_CHARS),
            )
            self._warnings.append(warning)
            if disable_after_failure:
                self._disabled.add(name)
            return ProjectionResult(completed=False, warning=warning)
        return ProjectionResult(completed=True)


def reduce_workflow_progress(
    workflow_steps: object,
    *,
    stage_terminals: Mapping[tuple[str, str], Mapping[str, Any]],
    active_gate: Mapping[str, Any] | None = None,
    session_outcome: str | None = None,
    failure_scope: str | None = None,
    terminal_integrity: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Reduce one verified plan and canonical terminal/gate facts.

    This is the sole workflow progress algorithm for attached rendering and
    detached inspection. Cursor position, route trace, and Ledger tail order
    are deliberately absent from its inputs.
    """

    steps = validate_workflow_steps(workflow_steps)
    terminal_by_step: dict[str, str] = {}
    diagnostics = [dict(item) for item in terminal_integrity]
    for step in steps:
        terminal = stage_terminals.get((step["step_id"], step["stage"]))
        if terminal is None:
            continue
        event_type = terminal.get("event_type")
        if event_type not in {"stage_completed", "stage_skipped", "stage_failed"}:
            continue
        terminal_by_step[step["step_id"]] = str(event_type)

    completed = sum(value == "stage_completed" for value in terminal_by_step.values())
    skipped = sum(value == "stage_skipped" for value in terminal_by_step.values())
    failed = sum(value == "stage_failed" for value in terminal_by_step.values())
    first_unsettled_index = next(
        (
            index
            for index, step in enumerate(steps)
            if step["step_id"] not in terminal_by_step
        ),
        None,
    )
    if first_unsettled_index is not None:
        later_terminals = [
            step["step_id"]
            for step in steps[first_unsettled_index + 1 :]
            if step["step_id"] in terminal_by_step
        ]
        if later_terminals:
            diagnostics.append(
                {
                    "kind": "non_prefix_stage_terminal",
                    "first_unsettled_step_id": steps[first_unsettled_index]["step_id"],
                    "later_terminal_step_ids": later_terminals,
                }
            )

    session_terminal = session_outcome in {
        "completed",
        "failed",
        "failed_terminal",
    }
    if session_outcome == "completed" and first_unsettled_index is not None:
        diagnostics.append(
            {
                "kind": "completed_session_has_unsettled_occurrence",
                "workflow_step_id": steps[first_unsettled_index]["step_id"],
            }
        )
    if session_outcome in {"failed", "failed_terminal"}:
        if failure_scope == "occurrence":
            if failed == 0:
                diagnostics.append({"kind": "failed_session_has_no_failed_occurrence"})
        elif failure_scope == "controller_boundary":
            settled_prefix_length = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step["step_id"] not in terminal_by_step
                ),
                len(steps),
            )
            if failed:
                diagnostics.append(
                    {"kind": "controller_boundary_failure_has_failed_occurrence"}
                )
            elif settled_prefix_length == 0:
                diagnostics.append(
                    {"kind": "controller_boundary_failure_has_no_settled_prefix"}
                )
        elif failure_scope is None:
            diagnostics.append({"kind": "failed_session_missing_failure_scope"})
        else:
            diagnostics.append({"kind": "failed_session_has_invalid_failure_scope"})
    elif failure_scope is not None:
        diagnostics.append({"kind": "completed_session_has_failure_scope"})

    current_step: WorkflowStep | None = None
    if not diagnostics and not session_terminal and failed == 0:
        if first_unsettled_index is not None:
            current_step = steps[first_unsettled_index]

    blocked = 0
    if active_gate is not None:
        gate_step_id = active_gate.get("workflow_step_id")
        gate_stage = active_gate.get("stage")
        if (
            current_step is not None
            and gate_step_id == current_step["step_id"]
            and gate_stage == current_step["stage"]
        ):
            blocked = 1
        else:
            diagnostics.append(
                {
                    "kind": "active_gate_does_not_match_current_occurrence",
                    "workflow_step_id": gate_step_id,
                    "stage": gate_stage,
                }
            )
            current_step = None

    return {
        "total_steps": len(steps),
        "completed_steps": completed,
        "skipped_steps": skipped,
        "failed_steps": failed,
        "blocked_steps": blocked,
        "unfinished_steps": len(steps) - completed - skipped,
        "current_workflow_step_id": (
            current_step["step_id"] if current_step is not None else None
        ),
        "current_stage": current_step["stage"] if current_step is not None else None,
        "projection_authority": (
            "degraded_stage_terminal_projection" if diagnostics else "authoritative"
        ),
        "integrity_diagnostics": diagnostics,
    }


def progress_projection_input(
    state: TopLevelState,
    *,
    stage_terminals: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    terminal_integrity: Iterable[Mapping[str, Any]] = (),
) -> ProgressProjectionInput:
    """Build attached progress from the shared occurrence reducer."""

    supervision = str(state.get("supervision_state") or "")
    failure_scope = _attached_failure_scope(
        state,
        stage_terminals=stage_terminals or {},
    )
    reduction = reduce_workflow_progress(
        state.get("workflow_steps"),
        stage_terminals=stage_terminals or {},
        active_gate=(
            state.get("active_interrupt") if supervision == "blocked_human" else None
        ),
        session_outcome=(
            "completed"
            if supervision == "completed"
            else "failed"
            if supervision == "failed_terminal"
            else None
        ),
        failure_scope=failure_scope,
        terminal_integrity=terminal_integrity,
    )
    interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    return ProgressProjectionInput(
        session_id=str(state.get("session_id") or ""),
        current_stage=(
            str(reduction["current_stage"])
            if reduction["current_stage"] is not None
            else None
        ),
        supervision_state=supervision,
        workflow_step_id=reduction["current_workflow_step_id"],
        workflow_step_index=(
            next(
                (
                    index
                    for index, step in enumerate(state.get("workflow_steps") or [])
                    if step.get("step_id") == reduction["current_workflow_step_id"]
                ),
                len(state.get("workflow_steps") or []),
            )
        ),
        entered_workflow_step_index=(
            int(state.get("entered_workflow_step_index"))
            if isinstance(state.get("entered_workflow_step_index"), int)
            else -1
        ),
        total_workflow_steps=int(reduction["total_steps"]),
        completed_workflow_steps=int(reduction["completed_steps"]),
        skipped_workflow_steps=int(reduction["skipped_steps"]),
        failed_workflow_steps=int(reduction["failed_steps"]),
        blocked_workflow_steps=int(reduction["blocked_steps"]),
        projection_authority=str(reduction["projection_authority"]),
        review_request_id=(
            str(interrupt.get("review_request_id"))
            if interrupt.get("review_request_id") is not None
            else None
        ),
        gate_id=(
            str(interrupt.get("gate_id"))
            if interrupt.get("gate_id") is not None
            else None
        ),
        route_trace_count=len(state.get("route_trace") or []),
        review_note_count=len(state.get("review_notes") or []),
    )


def _attached_failure_scope(
    state: TopLevelState,
    *,
    stage_terminals: Mapping[tuple[str, str], Mapping[str, Any]],
) -> str | None:
    """Project occurrence scope from the same handoff and terminal proof.

    Attached rendering runs before the run-level terminal singleton is written,
    so it cannot read ``failure_scope`` from that future Ledger row. A failed
    occurrence is nevertheless already proven by its explicit handoff and
    matching durable ``stage_failed`` fact. Controller-boundary scope remains
    the authoritative finalizer's responsibility and is consumed by detached
    inspection from ``session_terminal``.
    """

    if state.get("supervision_state") != "failed_terminal":
        return None
    outcome = state.get("workflow_step_outcome")
    if not isinstance(outcome, Mapping) or outcome.get("outcome") != "failed":
        return None
    workflow_step_id = outcome.get("workflow_step_id")
    stage = outcome.get("stage")
    if not isinstance(workflow_step_id, str) or not isinstance(stage, str):
        return None
    terminal = stage_terminals.get((workflow_step_id, stage))
    if terminal is not None and terminal.get("event_type") == "stage_failed":
        return "occurrence"
    return None


def project_workflow_state(
    boundary: ProjectionBoundary,
    state: TopLevelState,
    *,
    data_dir: str | Path,
    session_id: str,
) -> ProjectionResult | None:
    """Write the report appropriate to a returned state through the boundary."""

    from app.reports.review_report import (
        write_final_review_report,
        write_operator_report,
    )

    supervision = state.get("supervision_state")
    if supervision == "completed":
        return boundary.run(
            "final_report",
            write_final_review_report,
            data_dir,
            session_id,
        )
    if supervision in {"running_stage", "blocked_human", "failed_terminal"}:
        return boundary.run(
            "operator_report",
            write_operator_report,
            data_dir,
            session_id,
            state,
        )
    return None


def _bounded(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)] + "…"
