"""Routing helpers for the function annotation stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("function_annotation") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving protein inputs."""
    return "fail" if _output(state).get("status") == "failed" else "methods"


def route_after_method_plan(state: TopLevelState) -> str:
    """Route after planning function annotation methods."""
    return "fail" if _output(state).get("status") == "failed" else "provenance"


def route_after_provenance_validation(state: TopLevelState) -> str:
    """Route after validating database provenance."""
    return "fail" if _output(state).get("status") == "failed" else "commit"
