"""Fail-closed behavior for the parent-registered GapCloser placeholder."""

from __future__ import annotations

from typing import Literal

from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import append_route_trace
from app.stages.gapcloser.state import update_gapcloser_stage_output
from app.workflow import next_stage_name

GAPCLOSER_STAGE: Literal["gapcloser"] = "gapcloser"
GAPCLOSER_NOT_IMPLEMENTED_REASON = (
    "gapcloser is registered in the parent graph but has no implemented method"
)


def stop_unimplemented_gapcloser(state: TopLevelState) -> StateUpdate:
    """Skip legacy unscoped traversal or fail an explicit request."""

    requested = state.get("requested_stages")
    if requested is None or GAPCLOSER_STAGE not in requested:
        reason = "not_implemented" if requested is None else "not_requested"
        return {
            "current_stage": next_stage_name(GAPCLOSER_STAGE, requested),
            "supervision_state": "running_stage",
            "skip_reasons": {
                **(state.get("skip_reasons") or {}),
                GAPCLOSER_STAGE: reason,
            },
        }

    update = update_gapcloser_stage_output(
        state,
        {
            "status": "blocked_not_implemented",
            "reason": GAPCLOSER_NOT_IMPLEMENTED_REASON,
        },
    )
    skip_reasons = dict(state.get("skip_reasons") or {})
    skip_reasons.pop(GAPCLOSER_STAGE, None)
    update.update(
        {
            "current_stage": GAPCLOSER_STAGE,
            "supervision_state": "failed_terminal",
            "last_error": GAPCLOSER_NOT_IMPLEMENTED_REASON,
            "route_trace": append_route_trace(state, "gapcloser_stage"),
            "skip_reasons": skip_reasons,
        }
    )
    return update
