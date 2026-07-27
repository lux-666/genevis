"""Routing helpers for the AutoHiC-only Hi-C stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.hic_scaffolding.state import (
    HiCScaffoldingCursor,
    hic_scaffolding_output,
)
from app.workflow import is_terminal_failure

_REQUIREMENT_STATUSES = {
    "blocked_missing_requirements",
    "blocked_env",
    "blocked_param",
    "missing_tool",
}


def _output(state: TopLevelState) -> HiCScaffoldingCursor:
    return hic_scaffolding_output(state)


def route_after_enter(state: TopLevelState) -> str:
    """Resume an approved parameter attempt directly at AutoHiC freeze."""

    status = _output(state).get("status")
    if status in {
        "retry_parameters_after_execution_failure",
        "retry_parameters_after_review_failure",
    }:
        return "recommend"
    if status in {"parameters_approved", "autohic_frozen", "autohic_ran"}:
        return "autohic"
    return "resolve"


def route_after_resolve(state: TopLevelState) -> str:
    """Recommend the enzyme only after required run inputs resolve."""

    status = _output(state).get("status")
    if status in _REQUIREMENT_STATUSES:
        return "requirements"
    if is_terminal_failure(status):
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
    if status == "failed" and _output(state).get("failure_kind") == "execution":
        return "retry_gate"
    if is_terminal_failure(status):
        return "fail"
    return "review"


def route_after_retry_gate(state: TopLevelState) -> str:
    """Continue only when operator policy authorized a fresh recommendation."""

    if _output(state).get("status") == "retry_parameters_after_execution_failure":
        return "recommend"
    return "fail"


def route_after_review(state: TopLevelState) -> str:
    """The prepared AutoHiC result always pauses at the adoption gate."""

    return "block" if _output(state).get("status") == "blocked_human" else "fail"
