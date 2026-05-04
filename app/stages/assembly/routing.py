"""Routing helpers for assembly candidate planning."""

from __future__ import annotations

from app.schemas.state import TopLevelState


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving assembly inputs."""
    return "fail" if _output(state).get("status") == "failed" else "build"


def route_after_candidate_set(state: TopLevelState) -> str:
    """Route after building candidate compatibility records."""
    return "fail" if _output(state).get("status") == "failed" else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route after selecting primary/fallback candidates."""
    status = _output(state).get("status")
    if status == "blocked_human":
        return "block"
    if status == "failed":
        return "fail"
    return "plan"


def route_after_plan(state: TopLevelState) -> str:
    """Route after writing planned candidate bundles."""
    return "fail" if _output(state).get("status") == "failed" else "commit"
