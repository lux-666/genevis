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
    return "commit"


def route_after_purge_dups(state: TopLevelState) -> str:
    return "fail" if is_terminal_failure(_output(state).get("status")) else "commit"
