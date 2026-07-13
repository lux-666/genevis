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
    """Compact, cross-stage contract for a pending human review.

    Stage-specific interrupt payloads may carry additional data in their
    originating stage output.  Fields read by shared lifecycle and reporting
    code are declared here so those consumers do not receive an unstructured
    ``dict[str, Any]`` from :class:`TopLevelState`.
    """

    stage: Required[DomainStage]
    gate_id: NotRequired[str | None]
    reason: NotRequired[str]
    blocked_at: NotRequired[str]
    decisions_allowed: NotRequired[list[str]]
    messages: NotRequired[list[str]]
    notes: NotRequired[list[str]]
    auto_decidable: NotRequired[bool]
    pending_human_confirmation: NotRequired[bool]
    review_request_artifact_ref: NotRequired[str]
    review_policy: NotRequired[dict[str, Any]]
    review_policy_outcome: NotRequired[dict[str, Any]]
    candidates: NotRequired[list[dict[str, Any]]]
    method_candidates: NotRequired[list[dict[str, Any]]]
    signals: NotRequired[list[dict[str, Any]]]
    report_refs: NotRequired[dict[str, str]]
    candidate_comparison_context: NotRequired[dict[str, Any]]
    recommended_assembly_selection: NotRequired[dict[str, Any]]
    recommended_route: NotRequired[str]
    recommendation_reasoning: NotRequired[dict[str, Any] | list[Any] | str]
    attempt_id: NotRequired[int]
    previous_attempt_id: NotRequired[int | None]
    task: NotRequired[str]
    proposal_semantic_hash: NotRequired[str]
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
