"""Routing helpers for the Hi-C scaffolding stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.base import is_fail_status


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("hic_scaffolding") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving Hi-C inputs."""
    return "fail" if is_fail_status(_output(state).get("status")) else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route to scaffold planning or skip writing."""
    hic_out = _output(state)
    if is_fail_status(hic_out.get("status")):
        return "fail"
    return "scaffold" if hic_out.get("mode") == "scaffold" else "skip"


def route_after_scaffold_plan(state: TopLevelState) -> str:
    """Route after scaffold planning."""
    hic_out = _output(state)
    if is_fail_status(hic_out.get("status")):
        return "fail"
    return _next_method(hic_out)


def _next_method(hic_out: dict) -> str:
    methods = hic_out.get("selected_methods") or [hic_out.get("primary_method") or hic_out.get("selected_method_name")]
    completed = set(hic_out.get("completed_method_names") or [])
    method = next((item for item in methods if item and item not in completed), None)
    if hic_out.get("execution_mode") != "local" or not method:
        return "block"
    if method == "hic_scaffolding.candidate.autohic":
        return "autohic"
    return "block"


def route_after_method_subgraph(state: TopLevelState) -> str:
    """Route after a method-specific Hi-C lifecycle subgraph."""
    output = _output(state)
    if is_fail_status(output.get("status")):
        return "fail"
    return _next_method(output)


def route_after_skip(state: TopLevelState) -> str:
    """Route after skip artifact creation."""
    return "fail" if is_fail_status(_output(state).get("status")) else "commit"
