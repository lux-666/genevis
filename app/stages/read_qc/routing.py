"""Routing helpers for the read QC stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


def _status(state: TopLevelState) -> str | None:
    return ((state.get("stage_outputs") or {}).get("read_qc") or {}).get("status")


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving raw long reads."""
    return "fail" if _status(state) == "failed" else "plan"


def route_after_plan(state: TopLevelState) -> str:
    """Route after planning SeqKit stats."""
    read_qc_out = ((state.get("stage_outputs") or {}).get("read_qc") or {})
    if read_qc_out.get("status") == "failed":
        return "fail"
    if read_qc_out.get("skip_reason") == "local_execution_not_enabled":
        return "commit"
    return "freeze"


def route_after_run(state: TopLevelState) -> str:
    """Route after local execution."""
    return "fail" if _status(state) == "failed" else "collect"


def route_after_collect(state: TopLevelState) -> str:
    """Route after artifact collection."""
    return "fail" if _status(state) == "failed" else "commit"
