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
    """Route after validating database provenance — dispatch to diamond execution or crash-recovery."""
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "provenance_validated":
        return "freeze_diamond"
    if status == "diamond_frozen":
        return "run_diamond"
    if status == "diamond_ran":
        return "collect_diamond"
    if status == "diamond_completed":
        return "commit"
    return "commit"


def route_after_diamond_freeze(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "diamond_completed":
        return "commit"
    return "run_diamond"


def route_after_diamond_run(state: TopLevelState) -> str:
    return "fail" if _output(state).get("status") == "failed" else "collect_diamond"


def route_after_diamond_collect(state: TopLevelState) -> str:
    return "fail" if _output(state).get("status") == "failed" else "commit"
