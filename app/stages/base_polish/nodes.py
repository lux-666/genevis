"""Fail-closed behavior for the parent-registered Base Polish placeholder."""

from __future__ import annotations

from typing import Literal

from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import append_route_trace
from app.stages.base_polish.state import update_base_polish_stage_output
from app.workflow import next_stage_name

BASE_POLISH_STAGE: Literal["base_polish"] = "base_polish"
BASE_POLISH_NOT_IMPLEMENTED_REASON = (
    "base_polish is registered in the parent graph but has no implemented method"
)


def stop_unimplemented_base_polish(state: TopLevelState) -> StateUpdate:
    """Skip legacy unscoped traversal or fail an explicit request."""

    requested = state.get("requested_stages")
    if requested is None or BASE_POLISH_STAGE not in requested:
        reason = "not_implemented" if requested is None else "not_requested"
        return {
            "current_stage": next_stage_name(BASE_POLISH_STAGE, requested),
            "supervision_state": "running_stage",
            "skip_reasons": {
                **(state.get("skip_reasons") or {}),
                BASE_POLISH_STAGE: reason,
            },
        }

    update = update_base_polish_stage_output(
        state,
        {
            "status": "blocked_not_implemented",
            "reason": BASE_POLISH_NOT_IMPLEMENTED_REASON,
        },
    )
    skip_reasons = dict(state.get("skip_reasons") or {})
    skip_reasons.pop(BASE_POLISH_STAGE, None)
    update.update(
        {
            "current_stage": BASE_POLISH_STAGE,
            "supervision_state": "failed_terminal",
            "last_error": BASE_POLISH_NOT_IMPLEMENTED_REASON,
            "route_trace": append_route_trace(state, "base_polish_stage"),
            "skip_reasons": skip_reasons,
        }
    )
    return update
