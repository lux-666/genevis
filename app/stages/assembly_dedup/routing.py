"""Routing helpers for assembly deduplication."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.assembly_dedup.state import (
    AssemblyDedupCursor,
    assembly_dedup_output,
)
from app.workflow import is_terminal_failure


def _output(state: TopLevelState) -> AssemblyDedupCursor:
    return assembly_dedup_output(state)


def route_after_enter(state: TopLevelState) -> str:
    return "fail" if is_terminal_failure(_output(state).get("status")) else "plan"


def route_after_plan(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if is_terminal_failure(status):
        return "fail"
    if status == "ready_to_run":
        return "purge_dups"
    if status in {"evaluate_dedup", "dedup_quast_frozen", "dedup_quast_ran"}:
        return "evaluate"
    if status == "dedup_advice_ready":
        return "recommend"
    if status == "blocked_human":
        return "block"
    if (
        status in {"dedup_frozen", "dedup_ran"}
        and _output(state).get("recommendation_mode") == "advisor"
    ):
        return "purge_dups"
    return "commit"


def route_after_purge_dups(state: TopLevelState) -> str:
    if is_terminal_failure(_output(state).get("status")):
        return "fail"
    return (
        "assess" if _output(state).get("recommendation_mode") == "advisor" else "commit"
    )
