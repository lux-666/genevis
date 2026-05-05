"""Routing helpers for the Hi-C scaffolding stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("hic_scaffolding") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving Hi-C inputs."""
    return "fail" if _output(state).get("status") == "failed" else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route to scaffold planning or skip writing."""
    hic_out = _output(state)
    if hic_out.get("status") == "failed":
        return "fail"
    return "scaffold" if hic_out.get("mode") == "scaffold" else "skip"


def route_after_scaffold_plan(state: TopLevelState) -> str:
    """Route after scaffold planning."""
    return "fail" if _output(state).get("status") == "failed" else "block"


def route_after_skip(state: TopLevelState) -> str:
    """Route after skip artifact creation."""
    return "fail" if _output(state).get("status") == "failed" else "commit"
