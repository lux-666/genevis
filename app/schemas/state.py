"""Small LangGraph state schema.

Canonical definition path: ``app/schemas/state.py``.

Large files, command logs, generated scripts, and metric tables belong in
artifacts or ledger records. The checkpointed state stores control fields and
references only.
"""

from typing import Any, Literal, NotRequired, Required, TypeAlias, TypedDict

from app.schemas.shared_parameters import SharedParameters

DomainStage = Literal[
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
    "done",
]


ControlState = Literal["running", "paused_manual", "stop_requested", "closed"]
SupervisionState = Literal[
    "idle",
    "running_stage",
    "reviewing",
    "blocked_human",
    "completed",
    "failed_terminal",
]

StageOutput: TypeAlias = dict[str, Any]
StageOutputs: TypeAlias = dict[DomainStage, StageOutput]


class ActiveInterrupt(TypedDict, total=False):
    """Bounded checkpoint pointer to an immutable human-review request."""

    stage: Required[DomainStage]
    gate_id: NotRequired[str | None]
    reason: NotRequired[str]
    blocked_at: NotRequired[str]
    review_request_id: NotRequired[str]
    request_ref: NotRequired[dict[str, Any]]
    request_hash: NotRequired[str]
    attempt_id: NotRequired[int]
    previous_attempt_id: NotRequired[int | None]
    task: NotRequired[str]
    recommendation_hash: NotRequired[str]


class TopLevelState(TypedDict, total=False):

    # Stable run identity and storage root.
    session_id: Required[str]
    thread_id: NotRequired[str]
    data_dir: Required[str]

    # Operator-level lifecycle for the whole run.
    control_state: Required[ControlState]

    # Current supervision status used by routing, review gates, and entrypoints.
    supervision_state: Required[SupervisionState]

    # Workflow position and requested stage subset.
    current_stage: Required[DomainStage]
    requested_stages: NotRequired[list[DomainStage] | None]

    # Durable references to external contracts and compact artifact indexes.
    input_manifest_ref: Required[str]
    asset_registry_ref: NotRequired[str | None]
    stage_plan_ref: NotRequired[str | None]

    # Human-review or pause metadata. Keep payloads short and ref-heavy.
    active_interrupt: NotRequired[ActiveInterrupt | None]

    # Cross-method request parameters collected once by planning.
    shared_parameters: NotRequired[SharedParameters | dict[str, Any] | None]

    # Last terminal or blocking error, if any.
    last_error: NotRequired[str | None]

    # Small operator-facing notes accumulated during review or resume flows.
    review_notes: Required[list[str]]

    # Ordered node/stage trace for debugging high-level routing.
    route_trace: Required[list[str]]

    # Per-stage compact outputs. Large outputs must be represented by refs.
    stage_outputs: Required[StageOutputs]

    # Stage skip reasons keyed by stage name.
    skip_reasons: Required[dict[DomainStage, str]]


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
        shared_parameters=None,
        last_error=None,
        review_notes=[],
        route_trace=[],
        stage_outputs={},
        skip_reasons={},
    )
