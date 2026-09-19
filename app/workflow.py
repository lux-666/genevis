"""Stage registry order and deterministic planner defaults.

Runtime routing is owned exclusively by the frozen ``workflow_steps`` plan.
The order in this module is for registration, display, and default scope
recommendations; it is not a successor table.
"""

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
    "gapcloser",
    "t2t",
    "base_polish",
]

MANUAL_ONLY_STAGES: frozenset[DomainStage] = frozenset({"base_polish"})

# A successful occurrence of one of these stages owns the canonical assembly
# handoff.  Keep this set aligned with the stages that publish an assembly
# ResourceRef bearing their workflow-step ID.
ASSEMBLY_HANDOFF_PRODUCER_STAGES: frozenset[DomainStage] = frozenset(
    {
        "inventory",
        "assembly",
        "assembly_dedup",
        "assembly_review",
        "hic_scaffolding",
        "gapcloser",
        "t2t",
    }
)

TERMINAL_STAGE: DomainStage = "done"
TERMINAL_FAILURE_STATUSES: frozenset[ExecutionFailureStatus] = frozenset(
    {"failed", "blocked_env", "missing_tool"}
)


def analyze_review_sequence(stages: Sequence[str]) -> dict[int, int]:
    """Bind each explicit Review to the nearest assembly-handoff occurrence.

    ``stages`` excludes the implicit Inventory occurrence.  Returned keys and
    values are frozen workflow-step indexes, where Inventory is index zero.
    Review placement remains literal RunConfig data: this function never
    inserts Review and never rejects a sequence merely because Review is absent.
    """

    latest_producer_index = 0
    review_bindings: dict[int, int] = {}

    for index, stage in enumerate(stages, start=1):
        if stage == "assembly_review":
            review_bindings[index] = latest_producer_index

        if stage in ASSEMBLY_HANDOFF_PRODUCER_STAGES:
            latest_producer_index = index

    return review_bindings


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
    ['read_qc', ..., 'assembly_review', 'hic_scaffolding', 'gapcloser', 't2t']
    """
    if endpoint not in STAGE_ORDER:
        raise ValueError(f"unknown stage endpoint: {endpoint!r}")
    stop = STAGE_ORDER.index(endpoint)
    return [
        stage for stage in STAGE_ORDER[1 : stop + 1] if stage not in MANUAL_ONLY_STAGES
    ]


def recommended_workflow_template(endpoint: str) -> list[DomainStage]:
    """Return the explicit recommended occurrence sequence through *endpoint*.

    This is a planning-only template, deliberately distinct from the unique
    stage registry.  It may repeat a stage, while a validated ``RunConfig``
    continues to preserve the operator-provided literal occurrence list.
    """

    if endpoint == "t2t":
        return [
            *stages_through("hic_scaffolding"),
            "gapcloser",
            "t2t",
            "base_polish",
            "assembly_review",
        ]

    template = stages_through(endpoint)
    if endpoint == "assembly":
        template.extend(("assembly_dedup", "assembly_review"))
    return template


def recommended_workflow_for_manifest(
    asset_types: Collection[str],
) -> list[DomainStage]:
    """Return the canonical recommendation for manifest asset types.

    Manifest-aware planning has one precedence order: raw long reads imply the
    complete-genome recommendation, an existing assembly starts at Review, and
    Hi-C-only input starts at scaffolding.  The returned lists intentionally
    preserve the historical literal occurrence sequences, including the
    manual-only placeholder positions and the finishing Review occurrence.
    """

    available = set(asset_types)
    complete = recommended_workflow_template("t2t")
    if "raw_long_reads" in available:
        return complete
    if "assembly_fasta_registered" in available:
        return complete[complete.index("assembly_review") :]
    if "hic_reads_registered" in available:
        return complete[complete.index("hic_scaffolding") :]
    return []
