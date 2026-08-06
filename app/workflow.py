"""Stage registry order and deterministic planner defaults.

Runtime routing is owned exclusively by the frozen ``workflow_steps`` plan.
The order in this module is for registration, display, and default scope
recommendations; it is not a successor table.
"""

from __future__ import annotations

from collections.abc import Collection

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
    "gapcloser",
    "t2t",
    "base_polish",
]

MANUAL_ONLY_STAGES: frozenset[DomainStage] = frozenset(
    {"gapcloser", "base_polish"}
)

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
    """Return the default recommended prefix through *endpoint* (inclusive).

    TUI intake and planner recommendations share this default instead of
    maintaining independent literal arrays.  A frozen run may use another
    order or repeat stages; runtime never consults this helper for successors.
    Parent-registered placeholders remain manual-only and are not inserted by
    the default prefix recommendation.

    >>> stages_through("assembly_review")
    ['read_qc', 'genome_survey', 'assembly', 'assembly_dedup', 'assembly_review']
    >>> stages_through("t2t")
    ['read_qc', ..., 'assembly_review', 'hic_scaffolding', 't2t']
    """
    if endpoint not in STAGE_ORDER:
        raise ValueError(f"unknown stage endpoint: {endpoint!r}")
    stop = STAGE_ORDER.index(endpoint)
    return [
        stage
        for stage in STAGE_ORDER[1 : stop + 1]
        if stage not in MANUAL_ONLY_STAGES
    ]
