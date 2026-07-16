"""Routing helpers for the AutoHiC-only Hi-C stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.base import is_fail_status


_REQUIREMENT_STATUSES = {
    "blocked_missing_requirements",
    "blocked_env",
    "blocked_param",
    "missing_tool",
}


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("hic_scaffolding") or {}


def route_after_enter(state: TopLevelState) -> str:
    """Resume an approved parameter attempt directly at AutoHiC freeze."""

    return "autohic" if _output(state).get("status") == "parameters_approved" else "resolve"


def route_after_resolve(state: TopLevelState) -> str:
    """Recommend the enzyme only after required run inputs resolve."""

    status = _output(state).get("status")
    if status in _REQUIREMENT_STATUSES:
        return "requirements"
    if is_fail_status(status):
        return "fail"
    return "recommend"


def route_after_recommendation(state: TopLevelState) -> str:
    """Every valid LLM recommendation pauses for human approval."""

    status = _output(state).get("status")
    return "block" if status == "blocked_human" else "fail"


def route_after_autohic(state: TopLevelState) -> str:
    """Keep missing runtime requirements separate from execution failure."""

    status = _output(state).get("status")
    if status in _REQUIREMENT_STATUSES:
        return "requirements"
    if is_fail_status(status):
        return "fail"
    return "review"


def route_after_review(state: TopLevelState) -> str:
    """The prepared AutoHiC result always pauses at the adoption gate."""

    return "block" if _output(state).get("status") == "blocked_human" else "fail"
