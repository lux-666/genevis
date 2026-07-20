"""Routing helpers for the read QC stage."""

from __future__ import annotations

from app.workflow import is_terminal_failure
from app.schemas.state import TopLevelState


EXTRA_FAIL_STATUSES = frozenset({"blocked_input"})


def _status(state: TopLevelState) -> str | None:
    return ((state.get("stage_outputs") or {}).get("read_qc") or {}).get("status")


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving raw long reads."""
    return (
        "fail"
        if is_terminal_failure(_status(state), extra_statuses=EXTRA_FAIL_STATUSES)
        else "plan"
    )


def route_after_plan(state: TopLevelState) -> str:
    """Route after planning SeqKit stats."""
    read_qc_out = ((state.get("stage_outputs") or {}).get("read_qc") or {})
    if is_terminal_failure(
        read_qc_out.get("status"), extra_statuses=EXTRA_FAIL_STATUSES
    ):
        return "fail"
    if read_qc_out.get("skip_reason") == "local_execution_not_enabled":
        return "filter"
    return "stats"


def route_after_stats(state: TopLevelState) -> str:
    """Route after the SeqKit stats method subgraph returns."""
    return (
        "fail"
        if is_terminal_failure(_status(state), extra_statuses=EXTRA_FAIL_STATUSES)
        else "filter"
    )


def route_after_length_filter(state: TopLevelState) -> str:
    """Route after optional length filtering."""
    return (
        "fail"
        if is_terminal_failure(_status(state), extra_statuses=EXTRA_FAIL_STATUSES)
        else "commit"
    )
