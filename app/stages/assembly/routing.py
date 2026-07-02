"""Routing helpers for assembly candidate planning."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.assembly.constants import EXTRA_FAIL_STATUSES
from app.stages.base import is_fail_status


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving assembly inputs."""
    return "fail" if is_fail_status(_output(state).get("status"), EXTRA_FAIL_STATUSES) else "infer_profile"


def route_after_infer_profile(state: TopLevelState) -> str:
    """Route after writing the input profile."""
    status = _output(state).get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "select_route"


def route_after_select_route(state: TopLevelState) -> str:
    """Route after confirming or blocking on route selection."""
    status = _output(state).get("status")
    if status == "blocked_human":
        return "block"
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "build"


def route_after_candidate_set(state: TopLevelState) -> str:
    """Route after building candidate compatibility records."""
    return "fail" if is_fail_status(_output(state).get("status"), EXTRA_FAIL_STATUSES) else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route after selecting primary/fallback candidates."""
    status = _output(state).get("status")
    if status == "blocked_human":
        return "block"
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "plan"


def route_after_plan(state: TopLevelState) -> str:
    """Route after writing planned candidate bundles."""
    status = _output(state).get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    if status == "flye_execution_planned":
        return "flye"
    if status == "nextdenovo_execution_planned":
        return "nextdenovo"
    if status == "hifiasm_execution_planned":
        return "hifiasm"
    if status == "hifiasm_ont_execution_planned":
        return "hifiasm_ont"
    return "commit"


def route_after_method_subgraph(state: TopLevelState) -> str:
    """Route after a method-specific assembly subgraph returns."""
    status = _output(state).get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "commit"
