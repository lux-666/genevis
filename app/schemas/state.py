"""Small LangGraph state schema.

Canonical definition path: ``app/schemas/state.py``.

Large files, command logs, generated scripts, and metric tables belong in
artifacts or ledger records. The checkpointed state stores control fields and
references only.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

DomainStage = Literal[
    "inventory",
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review",
    "hic_scaffolding",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation",
    "done",
]


class TopLevelState(TypedDict, total=False):

    # Stable run identity and storage root.
    session_id: str
    thread_id: str
    data_dir: str

    # Operator-level lifecycle for the whole run.
    control_state: Literal["running", "paused_manual", "stop_requested", "closed"]

    # Current supervision status used by routing, review gates, and entrypoints.
    supervision_state: Literal[
        "idle",
        "running_stage",
        "reviewing",
        "blocked_human",
        "completed",
        "failed_terminal",
    ]

    # Workflow position and requested stage subset.
    current_stage: DomainStage
    requested_stages: list[DomainStage] | None

    # Durable references to external contracts and compact artifact indexes.
    input_manifest_ref: str
    asset_registry_ref: str | None
    stage_plan_ref: str | None

    # Human-review or pause metadata. Keep payloads short and ref-heavy.
    active_interrupt: dict[str, Any] | None

    # Last terminal or blocking error, if any.
    last_error: str | None

    # Small operator-facing notes accumulated during review or resume flows.
    review_notes: list[str]

    # Ordered node/stage trace for debugging high-level routing.
    route_trace: list[str]

    # Per-stage compact outputs. Large outputs must be represented by refs.
    stage_outputs: dict[str, dict[str, Any]]

    # Stage skip reasons keyed by stage name.
    skip_reasons: dict[str, str]


def initial_state(
    *,
    session_id: str,
    input_manifest_ref: str,
    data_dir: str = "data",
    requested_stages: list[DomainStage] | None = None,
) -> TopLevelState:
    """Build the minimal parent graph state."""
    return TopLevelState(
        session_id=session_id,
        thread_id=session_id,
        data_dir=data_dir,
        control_state="running",
        supervision_state="idle",
        current_stage="inventory",
        requested_stages=requested_stages,
        input_manifest_ref=input_manifest_ref,
        asset_registry_ref=None,
        stage_plan_ref=None,
        active_interrupt=None,
        last_error=None,
        review_notes=[],
        route_trace=[],
        stage_outputs={},
        skip_reasons={},
    )
