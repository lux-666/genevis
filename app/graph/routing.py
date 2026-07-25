"""Top-level route controller for the parent graph."""

from __future__ import annotations

from app.schemas.state import StateUpdate, TopLevelState
from app.workflow import STAGE_ORDER, TERMINAL_STAGE, next_requested_stage


def route_controller(state: TopLevelState) -> StateUpdate:
    """Record that the parent graph evaluated routing.

    Also skips stages not in requested_stages by advancing current_stage.
    """
    supervision = state.get("supervision_state")
    if supervision in ("failed_terminal", "completed", "blocked_human"):
        return {}
    current = state.get("current_stage", "inventory")
    requested = state.get("requested_stages")
    if (
        requested
        and current != "inventory"
        and current != TERMINAL_STAGE
        and current not in requested
    ):
        next_req = next_requested_stage(current, requested)
        return {"supervision_state": "idle", "current_stage": next_req}
    return {"supervision_state": "idle"}


def route_from_current_stage(state: TopLevelState) -> str:
    """Return the graph node name for the current stage."""
    supervision = state.get("supervision_state")
    if supervision in ("failed_terminal", "blocked_human"):
        return "end"
    current_stage = state.get("current_stage", "inventory")
    if current_stage == TERMINAL_STAGE:
        return "end"
    if current_stage not in STAGE_ORDER:
        return "end"
    return f"{current_stage}_stage"
