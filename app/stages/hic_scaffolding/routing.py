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
    method = hic_out.get("primary_method") or hic_out.get("selected_method_name")
    if hic_out.get("execution_mode") == "local" and method == "hic_scaffolding.candidate.yahs":
        return "freeze_yahs"
    return "block"


def route_after_yahs_freeze(state: TopLevelState) -> str:
    """Route after freezing a YaHS attempt."""
    status = _output(state).get("status")
    if status in FAIL_STATUSES:
        return "fail"
    if status == "yahs_frozen":
        return "run_yahs"
    return "block"


def route_after_yahs_run(state: TopLevelState) -> str:
    """Route after local YaHS execution."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "collect_yahs"


def route_after_yahs_collect(state: TopLevelState) -> str:
    """Route after collecting YaHS artifacts."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "block"


def route_after_skip(state: TopLevelState) -> str:
    """Route after skip artifact creation."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "commit"
