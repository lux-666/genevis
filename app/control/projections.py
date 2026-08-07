"""Exception-isolated presentation boundary with no lifecycle authority."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from app.control.workflow_plan import current_workflow_step_id
from app.schemas.state import TopLevelState

PROJECTION_WARNING_MAX_CHARS = 512


@dataclass(frozen=True)
class ProgressProjectionInput:
    """Immutable scalar-only input available to an attached progress renderer."""

    session_id: str
    current_stage: str
    supervision_state: str
    workflow_step_id: str | None
    workflow_step_index: int
    entered_workflow_step_index: int
    total_workflow_steps: int
    completed_workflow_steps: int
    skipped_workflow_steps: int
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


def progress_projection_input(state: TopLevelState) -> ProgressProjectionInput:
    """Derive the only checkpoint information visible to a progress sink."""

    steps = state.get("workflow_steps") or []
    index = state.get("workflow_step_index")
    workflow_step_id: str | None = None
    if isinstance(index, int) and 0 <= index < len(steps):
        try:
            workflow_step_id = current_workflow_step_id(state)
        except (TypeError, ValueError):
            workflow_step_id = None
    interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    skipped_ids = set((state.get("skip_reasons") or {}).keys())
    completed_prefix = index if isinstance(index, int) and index >= 0 else 0
    skipped_count = sum(
        1
        for step in steps[:completed_prefix]
        if isinstance(step, dict) and step.get("step_id") in skipped_ids
    )
    return ProgressProjectionInput(
        session_id=str(state.get("session_id") or ""),
        current_stage=str(state.get("current_stage") or ""),
        supervision_state=str(state.get("supervision_state") or ""),
        workflow_step_id=workflow_step_id,
        workflow_step_index=int(index) if isinstance(index, int) else -1,
        entered_workflow_step_index=(
            int(state.get("entered_workflow_step_index"))
            if isinstance(state.get("entered_workflow_step_index"), int)
            else -1
        ),
        total_workflow_steps=len(steps),
        completed_workflow_steps=max(0, completed_prefix - skipped_count),
        skipped_workflow_steps=skipped_count,
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
    if supervision in {"blocked_human", "failed_terminal"}:
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
