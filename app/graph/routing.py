"""Top-level route controller for the parent graph."""

from __future__ import annotations

from app.control.workflow_plan import current_workflow_step, seed_current_workflow_step
from app.schemas.state import StateUpdate, TopLevelState


def route_controller(state: TopLevelState) -> StateUpdate:
    """Seed and dispatch exactly the active frozen workflow occurrence."""
    supervision = state.get("supervision_state")
    if supervision in ("failed_terminal", "completed", "blocked_human"):
        return {}
    step = current_workflow_step(state)
    if step is None:
        return {"current_stage": "done", "supervision_state": "completed"}
    return {"supervision_state": "idle", **seed_current_workflow_step(state)}


def route_from_current_stage(state: TopLevelState) -> str:
    """Return the graph node name for the current stage."""
    supervision = state.get("supervision_state")
    if supervision in ("failed_terminal", "blocked_human"):
        return "end"
    step = current_workflow_step(state)
    if step is None:
        return "end"
    return f"{step['stage']}_stage"
