"""Routing helpers for assembly deduplication."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.base import is_fail_status


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly_dedup") or {}


def route_after_enter(state: TopLevelState) -> str:
    return "fail" if is_fail_status(_output(state).get("status")) else "plan"


def route_after_plan(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if is_fail_status(status):
        return "fail"
    if status == "ready_to_run":
        return "run"
    return "commit"


def route_after_run(state: TopLevelState) -> str:
    return "fail" if is_fail_status(_output(state).get("status")) else "commit"
