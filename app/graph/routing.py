"""Top-level route controller for the parent graph."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages import STAGE_ORDER, TERMINAL_STAGE


def route_controller(state: TopLevelState) -> dict:
    """Record that the parent graph evaluated routing.

    Also skips stages not in requested_stages by advancing current_stage.
    """
    supervision = state.get("supervision_state")
    if supervision in ("failed_terminal", "completed", "blocked_human"):
        return {}
    current = state.get("current_stage", "inventory")
    requested = state.get("requested_stages")
    if requested and current != "inventory" and current != TERMINAL_STAGE and current not in requested:
        next_req = _next_in_requested(current, requested)
        return {"supervision_state": "idle", "current_stage": next_req}
    return {"supervision_state": "idle"}


def _next_in_requested(current_stage: str, requested: list[str]) -> str:
    """Find the next stage in STAGE_ORDER that is in requested, or done."""
    if current_stage not in STAGE_ORDER:
        return TERMINAL_STAGE
    index = STAGE_ORDER.index(current_stage)
    for i in range(index + 1, len(STAGE_ORDER)):
        if STAGE_ORDER[i] in requested:
            return STAGE_ORDER[i]
    return TERMINAL_STAGE


def skipped_stages_until_next(
    current_stage: str,
    requested_stages: list[str] | None,
) -> list[str]:
    """Return unrequested stages bypassed by an immediate resume handoff."""
    if not requested_stages or current_stage not in STAGE_ORDER:
        return []
    next_stage = _next_in_requested(current_stage, requested_stages)
    start = STAGE_ORDER.index(current_stage) + 1
    stop = STAGE_ORDER.index(next_stage) if next_stage in STAGE_ORDER else len(STAGE_ORDER)
    return [
        stage
        for stage in STAGE_ORDER[start:stop]
        if stage not in requested_stages
    ]


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


def next_stage_name(
    current_stage: str,
    requested_stages: list[str] | None = None,
) -> str:
    """Return the next applicable domain stage, or ``done``.

    Stage subgraphs normally hand control back through ``route_controller``,
    which skips unrequested stages.  Resume updates can commit a gate outside
    that node transition, so callers at a resume boundary may supply the saved
    requested-stage set and get the same routing decision immediately.
    """
    if requested_stages:
        return _next_in_requested(current_stage, requested_stages)
    if current_stage not in STAGE_ORDER:
        return TERMINAL_STAGE
    index = STAGE_ORDER.index(current_stage)
    if index + 1 >= len(STAGE_ORDER):
        return TERMINAL_STAGE
    return STAGE_ORDER[index + 1]
