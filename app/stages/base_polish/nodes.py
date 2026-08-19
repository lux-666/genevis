"""Fail-closed behavior for the parent-registered Base Polish placeholder."""

from __future__ import annotations

from typing import Literal

from app.control.workflow_plan import fail_workflow_step
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import append_route_trace
from app.stages.base_polish.state import update_base_polish_stage_output

BASE_POLISH_STAGE: Literal["base_polish"] = "base_polish"
BASE_POLISH_NOT_IMPLEMENTED_REASON = (
    "base_polish is registered in the parent graph but has no implemented method"
)


def stop_unimplemented_base_polish(state: TopLevelState) -> StateUpdate:
    """Fail closed when the frozen plan reaches Base Polish."""

    update = update_base_polish_stage_output(
        state,
        {
            "status": "blocked_not_implemented",
            "reason": BASE_POLISH_NOT_IMPLEMENTED_REASON,
        },
    )
    update.update(
        {
            **fail_workflow_step(
                state,
                BASE_POLISH_STAGE,
                BASE_POLISH_NOT_IMPLEMENTED_REASON,
            ),
            "route_trace": append_route_trace(state, "base_polish_stage"),
        }
    )
    return update
