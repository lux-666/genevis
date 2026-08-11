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

MANUAL_ONLY_STAGES: frozenset[DomainStage] = frozenset({"gapcloser", "base_polish"})

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
        "t2t",
    }
)

TERMINAL_STAGE: DomainStage = "done"
TERMINAL_FAILURE_STATUSES: frozenset[ExecutionFailureStatus] = frozenset(
    {"failed", "blocked_env", "missing_tool"}
)


def _workflow_step_id(index: int, stage: str) -> str:
    """Return the deterministic ID used by a frozen workflow occurrence."""

    return f"step-{index:03d}-{stage}"


def analyze_review_sequence(stages: Sequence[str]) -> dict[int, int]:
    """Validate mandatory Review boundaries and bind each Review occurrence.

    ``stages`` excludes the implicit Inventory occurrence.  Returned keys and
    values are frozen workflow-step indexes, where Inventory is index zero.
    The function is deliberately independent of RunConfig and checkpoint
    shapes so those boundaries cannot drift.
    """

    latest_producer_index = 0
    pending_review_producer: tuple[int, str] | None = None
    review_bindings: dict[int, int] = {}

    for index, stage in enumerate(stages, start=1):
        # The current manual-only stages are deterministic terminal placeholders,
        # so placing either before a required Review makes that Review unreachable.
        if stage in MANUAL_ONLY_STAGES and pending_review_producer is not None:
            producer_index, producer_stage = pending_review_producer
            raise ValueError(
                "workflow requires assembly_review after "
                f"{_workflow_step_id(producer_index, producer_stage)!r} "
                f"before {stage}"
            )

        if stage == "t2t":
            if pending_review_producer is not None:
                producer_index, producer_stage = pending_review_producer
                raise ValueError(
                    "workflow requires assembly_review after "
                    f"{_workflow_step_id(producer_index, producer_stage)!r} "
                    "before t2t"
                )
            latest_producer_index = index
            pending_review_producer = (index, stage)
            continue

        if stage == "assembly_review":
            review_bindings[index] = latest_producer_index
            if pending_review_producer is not None:
                producer_index, producer_stage = pending_review_producer
                if latest_producer_index != producer_index:
                    raise ValueError(
                        "workflow requires assembly_review bound to "
                        f"{_workflow_step_id(producer_index, producer_stage)!r}"
                    )
                pending_review_producer = None
            latest_producer_index = index
            continue

        if stage == "assembly":
            if pending_review_producer is not None:
                producer_index, producer_stage = pending_review_producer
                raise ValueError(
                    "workflow requires assembly_review after "
                    f"{_workflow_step_id(producer_index, producer_stage)!r} "
                    "before assembly"
                )
            latest_producer_index = index
            pending_review_producer = (index, stage)
            continue

        if stage in ASSEMBLY_HANDOFF_PRODUCER_STAGES:
            latest_producer_index = index
            if (
                pending_review_producer is not None
                and pending_review_producer[1] == "assembly"
            ):
                pending_review_producer = (index, stage)

    if pending_review_producer is not None:
        producer_index, producer_stage = pending_review_producer
        raise ValueError(
            "workflow requires assembly_review after "
            f"{_workflow_step_id(producer_index, producer_stage)!r} before termination"
        )

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
    ['read_qc', ..., 'assembly_review', 'hic_scaffolding', 't2t']
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

    template = stages_through(endpoint)
    if endpoint == "assembly":
        template.append("assembly_dedup")
    if endpoint in {"assembly", "t2t"}:
        template.append("assembly_review")
    return template
