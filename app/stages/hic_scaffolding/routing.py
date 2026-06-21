"""Routing helpers for the Hi-C scaffolding stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


FAIL_STATUSES = {"failed", "blocked_env", "missing_tool"}


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("hic_scaffolding") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving Hi-C inputs."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route to scaffold planning or skip writing."""
    hic_out = _output(state)
    if hic_out.get("status") in FAIL_STATUSES:
        return "fail"
    return "scaffold" if hic_out.get("mode") == "scaffold" else "skip"


def route_after_scaffold_plan(state: TopLevelState) -> str:
    """Route after scaffold planning."""
    hic_out = _output(state)
    if hic_out.get("status") in FAIL_STATUSES:
        return "fail"
    return _next_method(hic_out)


def _next_method(hic_out: dict) -> str:
    methods = hic_out.get("selected_methods") or [hic_out.get("primary_method") or hic_out.get("selected_method_name")]
    completed = set(hic_out.get("completed_method_names") or [])
    method = next((item for item in methods if item and item not in completed), None)
    if hic_out.get("execution_mode") != "local" or not method:
        return "block"
    if method == "hic_scaffolding.candidate.juicer_3ddna_juicebox":
        return "juicer_3ddna"
    if method == "hic_scaffolding.candidate.yahs":
        return "yahs"
    return "block"


def route_after_method_subgraph(state: TopLevelState) -> str:
    """Route after a method-specific Hi-C lifecycle subgraph."""
    output = _output(state)
    if output.get("status") in FAIL_STATUSES:
        return "fail"
    return _next_method(output)


def route_after_skip(state: TopLevelState) -> str:
    """Route after skip artifact creation."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "commit"
