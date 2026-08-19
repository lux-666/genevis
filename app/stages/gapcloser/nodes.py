"""Fail-closed behavior for the parent-registered GapCloser placeholder."""

from __future__ import annotations

from typing import Literal

from app.control.workflow_plan import fail_workflow_step
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import append_route_trace
from app.stages.gapcloser.state import update_gapcloser_stage_output

GAPCLOSER_STAGE: Literal["gapcloser"] = "gapcloser"
GAPCLOSER_NOT_IMPLEMENTED_REASON = (
    "gapcloser is registered in the parent graph but has no implemented method"
)


def stop_unimplemented_gapcloser(state: TopLevelState) -> StateUpdate:
    """Fail closed when the frozen plan reaches GapCloser."""

    update = update_gapcloser_stage_output(
        state,
        {
            "status": "blocked_not_implemented",
            "reason": GAPCLOSER_NOT_IMPLEMENTED_REASON,
        },
    )
    update.update(
        {
            **fail_workflow_step(
                state,
                GAPCLOSER_STAGE,
                GAPCLOSER_NOT_IMPLEMENTED_REASON,
            ),
            "route_trace": append_route_trace(state, "gapcloser_stage"),
        }
    )
    return update
