"""Small LangGraph state schema.

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
    """Checkpointed state for the parent graph."""

    session_id: str
    thread_id: str
    data_dir: str
    control_state: Literal["running", "paused_manual", "stop_requested", "closed"]
    supervision_state: Literal[
        "idle",
        "running_stage",
        "reviewing",
        "blocked_human",
        "completed",
        "failed_terminal",
    ]
    current_stage: DomainStage
    requested_stages: list[DomainStage] | None
    input_manifest_ref: str
    asset_registry_ref: str | None
    stage_plan_ref: str | None
    active_interrupt: dict[str, Any] | None
    last_error: str | None
    review_notes: list[str]
    route_trace: list[str]
    stage_outputs: dict[str, dict[str, Any]]
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
