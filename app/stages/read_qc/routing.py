"""Routing helpers for the read QC stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


FAIL_STATUSES = {"failed", "blocked_input", "blocked_env", "missing_tool"}


def _status(state: TopLevelState) -> str | None:
    return ((state.get("stage_outputs") or {}).get("read_qc") or {}).get("status")


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving raw long reads."""
    return "fail" if _status(state) in FAIL_STATUSES else "plan"


def route_after_plan(state: TopLevelState) -> str:
    """Route after planning SeqKit stats."""
    read_qc_out = ((state.get("stage_outputs") or {}).get("read_qc") or {})
    if read_qc_out.get("status") in FAIL_STATUSES:
        return "fail"
    if read_qc_out.get("skip_reason") == "local_execution_not_enabled":
        return "filter"
    return "freeze"


def route_after_freeze(state: TopLevelState) -> str:
    """Route after freezing the SeqKit attempt; short-circuit on preflight block."""
    return "fail" if _status(state) in FAIL_STATUSES else "run"


def route_after_run(state: TopLevelState) -> str:
    """Route after local execution."""
    return "fail" if _status(state) in FAIL_STATUSES else "collect"


def route_after_collect(state: TopLevelState) -> str:
    """Route after artifact collection."""
    return "fail" if _status(state) in FAIL_STATUSES else "filter"


def route_after_length_filter(state: TopLevelState) -> str:
    """Route after optional length filtering."""
    return "fail" if _status(state) in FAIL_STATUSES else "fofn"


def route_after_make_fofn(state: TopLevelState) -> str:
    """Route after FOFN creation."""
    return "fail" if _status(state) in FAIL_STATUSES else "commit"
