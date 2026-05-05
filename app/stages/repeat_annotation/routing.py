"""Routing helpers for the repeat annotation stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("repeat_annotation") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving repeat annotation inputs."""
    return "fail" if _output(state).get("status") == "failed" else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route after selecting repeat annotation strategy."""
    return "fail" if _output(state).get("status") == "failed" else "library"


def route_after_library_plan(state: TopLevelState) -> str:
    """Route after planning repeat library handling."""
    return "fail" if _output(state).get("status") == "failed" else "masking"


def route_after_masking_plan(state: TopLevelState) -> str:
    """Route after planning repeat masking."""
    return "fail" if _output(state).get("status") == "failed" else "commit"
