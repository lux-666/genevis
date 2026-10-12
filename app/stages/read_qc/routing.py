"""Routing helpers for the read QC stage."""

from __future__ import annotations

from typing import Literal

from app.schemas.state import TopLevelState
from app.stages.read_qc.state import read_read_qc_stage_output
from app.workflow import is_terminal_failure

EXTRA_FAIL_STATUSES = frozenset({"blocked_input"})


def _status(state: TopLevelState) -> str | None:
    return read_read_qc_stage_output(state).get("status")


def route_after_resolve(state: TopLevelState) -> Literal["fail", "plan"]:
    """Route after resolving raw long reads."""
    return (
        "fail"
        if is_terminal_failure(_status(state), extra_statuses=EXTRA_FAIL_STATUSES)
        else "plan"
    )


def route_after_plan(state: TopLevelState) -> Literal["fail", "filter", "stats"]:
    """Route after planning SeqKit stats."""
    read_qc_out = read_read_qc_stage_output(state)
    if is_terminal_failure(
        read_qc_out.get("status"), extra_statuses=EXTRA_FAIL_STATUSES
    ):
        return "fail"
    stats = (read_qc_out.get("substeps") or {}).get("long_read_stats") or {}
    if stats.get("status") == "skipped":
        return "filter"
    return "stats"


def route_after_stats(state: TopLevelState) -> Literal["fail", "filter"]:
    """Route after the SeqKit stats method subgraph returns."""
    return (
        "fail"
        if is_terminal_failure(_status(state), extra_statuses=EXTRA_FAIL_STATUSES)
        else "filter"
    )


def route_after_length_filter(
    state: TopLevelState,
) -> Literal["fail", "short"]:
    """Route after optional length filtering."""
    return (
        "fail"
        if is_terminal_failure(_status(state), extra_statuses=EXTRA_FAIL_STATUSES)
        else "short"
    )


def route_after_fastp(state: TopLevelState) -> Literal["fail", "continue"]:
    """Never commit or continue after a paired-read method failure."""

    return (
        "fail"
        if is_terminal_failure(_status(state), extra_statuses=EXTRA_FAIL_STATUSES)
        else "continue"
    )
