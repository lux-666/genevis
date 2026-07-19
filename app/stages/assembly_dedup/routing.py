"""Routing helpers for assembly deduplication."""

from __future__ import annotations

from app.workflow import is_terminal_failure
from app.schemas.state import TopLevelState


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly_dedup") or {}


def route_after_enter(state: TopLevelState) -> str:
    return "fail" if is_terminal_failure(_output(state).get("status")) else "plan"


def route_after_plan(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if is_terminal_failure(status):
        return "fail"
    if status == "ready_to_run":
        return "run"
    return "commit"


def route_after_run(state: TopLevelState) -> str:
    return "fail" if is_terminal_failure(_output(state).get("status")) else "commit"
