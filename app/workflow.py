"""Canonical stage ordering and stage-to-stage navigation."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from app.schemas.execution import ExecutionFailureStatus
from app.schemas.state import DomainStage

STAGE_ORDER: list[DomainStage] = [
    "inventory",
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_dedup",
    "assembly_review",
    "hic_scaffolding",
    "t2t",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation",
]

TERMINAL_STAGE: DomainStage = "done"
TERMINAL_FAILURE_STATUSES: frozenset[ExecutionFailureStatus] = frozenset(
    {"failed", "blocked_env", "missing_tool"}
)


def is_terminal_failure(
    status: object,
    *,
    extra_statuses: Collection[str] = (),
) -> bool:
    """Return whether a stage status must route to its failure boundary."""

    return status in TERMINAL_FAILURE_STATUSES or status in extra_statuses


def stages_through(endpoint: str) -> list[DomainStage]:
    """Return the non-inventory prefix of STAGE_ORDER through *endpoint* (inclusive).

    This is the single canonical source for route expansion: both the TUI
    intake and the planner recommendation derive their stage lists from here
    rather than maintaining independent literal arrays.

    >>> stages_through("assembly_review")
    ['read_qc', 'genome_survey', 'assembly', 'assembly_dedup', 'assembly_review']
    >>> stages_through("t2t")
    ['read_qc', ..., 'assembly_review', 'hic_scaffolding', 't2t']
    """
    if endpoint not in STAGE_ORDER:
        raise ValueError(f"unknown stage endpoint: {endpoint!r}")
    stop = STAGE_ORDER.index(endpoint)
    return [s for s in STAGE_ORDER[1 : stop + 1]]  # skip "inventory"


def next_requested_stage(
    current_stage: str,
    requested: Sequence[str],
) -> DomainStage:
    """Find the next requested stage in workflow order, or the terminal stage."""
    if current_stage not in STAGE_ORDER:
        return TERMINAL_STAGE
    index = STAGE_ORDER.index(current_stage)
    for stage in STAGE_ORDER[index + 1 :]:
        if stage in requested:
            return stage
    return TERMINAL_STAGE


def skipped_stages_until_next(
    current_stage: str,
    requested_stages: Sequence[str] | None,
) -> list[str]:
    """Return unrequested stages bypassed by an immediate resume handoff."""
    if not requested_stages or current_stage not in STAGE_ORDER:
        return []
    next_stage = next_requested_stage(current_stage, requested_stages)
    start = STAGE_ORDER.index(current_stage) + 1
    stop = (
        STAGE_ORDER.index(next_stage) if next_stage in STAGE_ORDER else len(STAGE_ORDER)
    )
    return [stage for stage in STAGE_ORDER[start:stop] if stage not in requested_stages]


def unrequested_stages_from_current(
    current_stage: str,
    requested_stages: Sequence[str] | None,
) -> list[str]:
    """Return the current unrequested stage and any following skipped stages."""

    if not requested_stages or current_stage not in STAGE_ORDER:
        return []
    next_stage = next_requested_stage(current_stage, requested_stages)
    start = STAGE_ORDER.index(current_stage)
    stop = (
        STAGE_ORDER.index(next_stage) if next_stage in STAGE_ORDER else len(STAGE_ORDER)
    )
    return [stage for stage in STAGE_ORDER[start:stop] if stage not in requested_stages]


def next_stage_name(
    current_stage: str,
    requested_stages: Sequence[str] | None = None,
) -> DomainStage:
    """Return the next applicable domain stage, or the terminal stage."""
    if requested_stages:
        return next_requested_stage(current_stage, requested_stages)
    if current_stage not in STAGE_ORDER:
        return TERMINAL_STAGE
    index = STAGE_ORDER.index(current_stage)
    if index + 1 >= len(STAGE_ORDER):
        return TERMINAL_STAGE
    return STAGE_ORDER[index + 1]
