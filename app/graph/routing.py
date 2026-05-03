"""Top-level route controller for the parent graph."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages import STAGE_ORDER, TERMINAL_STAGE


def route_controller(state: TopLevelState) -> dict:
    """Record that the parent graph evaluated routing."""
    supervision = state.get("supervision_state")
    if supervision in ("failed_terminal", "completed", "blocked_human"):
        return {}
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


def next_stage_name(current_stage: str) -> str:
    """Return the next domain stage, or done after the final stage."""
    if current_stage not in STAGE_ORDER:
        return TERMINAL_STAGE
    index = STAGE_ORDER.index(current_stage)
    if index + 1 >= len(STAGE_ORDER):
        return TERMINAL_STAGE
    return STAGE_ORDER[index + 1]
