"""Routing helpers for assembly candidate planning."""

from __future__ import annotations

from app.schemas.state import TopLevelState


FAIL_STATUSES = {"failed", "blocked_env", "missing_tool"}


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving assembly inputs."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "build"


def route_after_candidate_set(state: TopLevelState) -> str:
    """Route after building candidate compatibility records."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route after selecting primary/fallback candidates."""
    status = _output(state).get("status")
    if status == "blocked_human":
        return "block"
    if status in FAIL_STATUSES:
        return "fail"
    return "plan"


def route_after_plan(state: TopLevelState) -> str:
    """Route after writing planned candidate bundles."""
    status = _output(state).get("status")
    if status in FAIL_STATUSES:
        return "fail"
    if status == "flye_execution_planned":
        return "freeze_flye"
    return "commit"


def route_after_flye_freeze(state: TopLevelState) -> str:
    """Route after freezing a local Flye attempt; short-circuit on preflight block."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "run_flye"


def route_after_flye_run(state: TopLevelState) -> str:
    """Route after running local Flye."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "collect_flye"


def route_after_flye_collect(state: TopLevelState) -> str:
    """Route after collecting Flye artifacts."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "commit"
