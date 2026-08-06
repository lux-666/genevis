"""Small LangGraph state schema.

Canonical definition path: ``app/schemas/state.py``.

Large files, command logs, generated scripts, and metric tables belong in
artifacts or ledger records. The checkpointed state stores control fields and
references only.
"""

import json
import math
import warnings
from collections.abc import Mapping
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    Required,
    TypeAlias,
    TypedDict,
    TypeVar,
    cast,
    get_args,
)

from app.schemas.limits import (
    ACTIVE_INTERRUPT_MAX_BYTES,
    LAST_ERROR_MAX_CHARS,
    RESOURCE_REFS_MAX_BYTES,
    REVIEW_NOTE_MAX_CHARS,
    REVIEW_NOTES_MAX_ITEMS,
    ROUTE_TRACE_LIMIT,
    STAGE_CURSOR_MAX_BYTES,
    STAGE_OUTPUTS_MAX_BYTES,
    TOP_LEVEL_STATE_HARD_MAX_BYTES,
    TOP_LEVEL_STATE_TARGET_BYTES,
)
from app.schemas.resource_ref import ResourceRef
from app.schemas.shared_parameters import SharedParameters

DomainStage = Literal[
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

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
StageCursor: TypeAlias = dict[str, JsonValue]
StageOutputs: TypeAlias = dict[DomainStage, StageCursor]
ResourceRefs: TypeAlias = dict[str, dict[str, JsonValue]]

MAX_WORKFLOW_STEPS = 64

DOMAIN_STAGES: frozenset[str] = frozenset(get_args(DomainStage))

_FROZEN_ATTEMPT_BODY_KEYS = frozenset(
    {
        "attempt_id",
        "session_id",
        "stage",
        "method_name",
        "script_path",
        "working_dir",
        "expected_outputs",
    }
)

_ARTIFACT_IDENTITY_KEYS = frozenset({"artifact_id", "path_ref", "sha256"})
_ARTIFACT_BODY_KEYS = frozenset({"body", "bytes", "content", "data", "payload", "text"})

_StageKey = TypeVar("_StageKey", bound=str)


class CheckpointBudgetWarning(UserWarning):
    """The whole state exceeded its architecture target but not its hard limit."""


def validate_stage_outputs(value: Mapping[_StageKey, object] | None) -> StageOutputs:
    """Validate the bounded JSON cursor contract stored in checkpoints."""

    if value is None:
        return {}
    unknown_stages = set(value).difference(DOMAIN_STAGES)
    if unknown_stages:
        names = ", ".join(sorted(unknown_stages))
        raise ValueError(f"stage_outputs contains unknown stages: {names}")

    outputs: dict[str, StageCursor] = {}
    for stage, cursor in value.items():
        if not isinstance(cursor, dict):
            raise TypeError(f"stage_outputs.{stage} must be a JSON object")
        _validate_json_value(cursor, path=f"stage_outputs.{stage}")
        cursor_size = _canonical_json_size(cursor)
        if cursor_size > STAGE_CURSOR_MAX_BYTES:
            raise ValueError(
                f"stage_outputs.{stage} exceeds {STAGE_CURSOR_MAX_BYTES} bytes"
            )
        outputs[stage] = cast(StageCursor, cursor)

    total_size = _canonical_json_size(outputs)
    if total_size > STAGE_OUTPUTS_MAX_BYTES:
        raise ValueError(f"stage_outputs exceeds {STAGE_OUTPUTS_MAX_BYTES} bytes")
    return cast(StageOutputs, outputs)


def replace_stage_outputs(
    _current: StageOutputs | None,
    update: Mapping[str, object] | None,
) -> StageOutputs:
    """LangGraph reducer that validates every replacement before checkpointing."""

    return validate_stage_outputs(update)


def validate_workflow_steps(value: object) -> list["WorkflowStep"]:
    """Validate the immutable occurrence list used by runtime routing."""

    # app.workflow imports DomainStage from this module, so defer this import
    # until validation rather than creating an import-time schema cycle.
    from app.workflow import ASSEMBLY_HANDOFF_PRODUCER_STAGES, analyze_review_sequence

    if not isinstance(value, list) or not value:
        raise TypeError("workflow_steps must be a non-empty list")
    if len(value) > MAX_WORKFLOW_STEPS:
        raise ValueError(f"workflow_steps exceeds {MAX_WORKFLOW_STEPS} items")

    steps: list[WorkflowStep] = []
    for index, raw_step in enumerate(value):
        if not isinstance(raw_step, Mapping):
            raise TypeError(f"workflow_steps[{index}] must be a JSON object")
        unknown = set(raw_step).difference(WorkflowStep.__annotations__)
        missing = set(WorkflowStep.__required_keys__).difference(raw_step)
        if unknown:
            raise ValueError(
                f"workflow_steps[{index}] contains unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        if missing:
            raise ValueError(
                f"workflow_steps[{index}] is missing required fields: "
                + ", ".join(sorted(missing))
            )
        stage = raw_step.get("stage")
        if stage not in DOMAIN_STAGES or stage == "done":
            raise ValueError(f"workflow_steps[{index}].stage is not executable")
        allow_local = raw_step.get("allow_local_execution")
        if type(allow_local) is not bool:
            raise TypeError(
                f"workflow_steps[{index}].allow_local_execution must be a bool"
            )
        expected_id = f"step-{index:03d}-{stage}"
        if raw_step.get("step_id") != expected_id:
            raise ValueError(f"workflow_steps[{index}].step_id must be {expected_id!r}")
        has_review_target = "review_target" in raw_step
        if stage != "assembly_review" and has_review_target:
            raise ValueError(
                f"workflow_steps[{index}].review_target is allowed only for "
                "assembly_review"
            )

        step: WorkflowStep = {
            "step_id": expected_id,
            "stage": cast(DomainStage, stage),
            "allow_local_execution": allow_local,
        }
        if stage == "assembly_review":
            if not has_review_target:
                raise ValueError(
                    f"workflow_steps[{index}].review_target is required for "
                    "assembly_review"
                )
            raw_target = raw_step.get("review_target")
            if not isinstance(raw_target, Mapping):
                raise TypeError(
                    f"workflow_steps[{index}].review_target must be a JSON object"
                )
            target_unknown = set(raw_target).difference(ReviewTarget.__annotations__)
            target_missing = set(ReviewTarget.__required_keys__).difference(raw_target)
            if target_unknown:
                raise ValueError(
                    f"workflow_steps[{index}].review_target contains unsupported "
                    "fields: " + ", ".join(sorted(target_unknown))
                )
            if target_missing:
                raise ValueError(
                    f"workflow_steps[{index}].review_target is missing required "
                    "fields: " + ", ".join(sorted(target_missing))
                )
            if raw_target.get("resource_type") != "assembly":
                raise ValueError(
                    f"workflow_steps[{index}].review_target.resource_type must be "
                    "'assembly'"
                )
            producer_step_id = raw_target.get("producer_step_id")
            if not isinstance(producer_step_id, str):
                raise TypeError(
                    f"workflow_steps[{index}].review_target.producer_step_id "
                    "must be a string"
                )
            step["review_target"] = {
                "resource_type": "assembly",
                "producer_step_id": producer_step_id,
            }
        steps.append(step)

    if steps[0]["stage"] != "inventory":
        raise ValueError("workflow_steps must begin with inventory")
    if any(step["stage"] == "inventory" for step in steps[1:]):
        raise ValueError("inventory may appear only as the first workflow step")

    review_bindings = analyze_review_sequence([step["stage"] for step in steps[1:]])
    for review_index, producer_index in review_bindings.items():
        review_step = steps[review_index]
        target = review_step["review_target"]
        producer_step_id = target["producer_step_id"]
        matching_indexes = [
            index
            for index, step in enumerate(steps[:review_index])
            if step["step_id"] == producer_step_id
        ]
        if not matching_indexes:
            raise ValueError(
                f"workflow_steps[{review_index}].review_target.producer_step_id "
                "must reference an earlier workflow step"
            )
        referenced_step = steps[matching_indexes[0]]
        if referenced_step["stage"] not in ASSEMBLY_HANDOFF_PRODUCER_STAGES:
            raise ValueError(
                f"workflow_steps[{review_index}].review_target.producer_step_id "
                "must reference an assembly handoff producer"
            )
        expected_producer_step_id = steps[producer_index]["step_id"]
        if producer_step_id != expected_producer_step_id:
            raise ValueError(
                f"workflow_steps[{review_index}].review_target.producer_step_id "
                f"must be {expected_producer_step_id!r}"
            )
    return steps


def freeze_workflow_steps(
    current: list["WorkflowStep"] | None,
    update: object,
) -> list["WorkflowStep"]:
    """Accept the plan once and reject runtime append, reorder, or replacement."""

    validated = validate_workflow_steps(update)
    # LangGraph initializes list-valued reducer channels with ``[]`` before it
    # applies the invocation input.  A valid workflow plan is never empty, so
    # that reducer identity is the only additional first-write state we accept.
    if current not in (None, []) and current != validated:
        raise ValueError("workflow_steps are immutable after graph initialization")
    return validated


def freeze_stage_seeds(
    current: StageOutputs | None,
    update: Mapping[str, object] | None,
) -> StageOutputs:
    """Accept frozen RunConfig cursor seeds once and reject later mutation."""

    validated = validate_stage_outputs(update)
    # Dict-valued reducer channels likewise start at ``{}``.  Runtime code has
    # no writer for this channel after initialization; this branch permits the
    # graph's reducer identity to receive the frozen seed projection.
    if current not in (None, {}) and current != validated:
        raise ValueError("stage_seeds are immutable after graph initialization")
    return validated


def validate_resource_refs(value: Mapping[str, object] | None) -> ResourceRefs:
    """Validate the bounded, JSON-only cross-stage resource table."""

    if value is None:
        return {}
    refs: dict[str, dict[str, JsonValue]] = {}
    for resource_type, raw_ref in value.items():
        if not isinstance(resource_type, str) or not resource_type.strip():
            raise TypeError("resource_refs keys must be non-empty strings")
        if not isinstance(raw_ref, Mapping):
            raise TypeError(f"resource_refs.{resource_type} must be a JSON object")
        ref = ResourceRef.from_dict(raw_ref)
        if ref.resource_type != resource_type:
            raise ValueError(
                f"resource_refs.{resource_type}.resource_type must match its table key"
            )
        serialized = ref.to_dict()
        _validate_json_value(serialized, path=f"resource_refs.{resource_type}")
        refs[resource_type] = cast(dict[str, JsonValue], serialized)

    total_size = _canonical_json_size(refs)
    if total_size > RESOURCE_REFS_MAX_BYTES:
        raise ValueError(f"resource_refs exceeds {RESOURCE_REFS_MAX_BYTES} bytes")
    return refs


def replace_resource_refs(
    _current: ResourceRefs | None,
    update: Mapping[str, object] | None,
) -> ResourceRefs:
    """Replace the resource table after validating its checkpoint contract."""

    return validate_resource_refs(update)


def _validate_json_value(value: object, *, path: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if (
            _ARTIFACT_IDENTITY_KEYS <= value.keys()
            and _ARTIFACT_BODY_KEYS & value.keys()
        ):
            raise ValueError(
                f"{path} embeds an artifact body; store only its ArtifactRef"
            )
        if _FROZEN_ATTEMPT_BODY_KEYS <= value.keys():
            raise ValueError(
                f"{path} embeds a FrozenAttemptSpec; store its artifact ref instead"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string JSON key")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} contains non-JSON value {type(value).__name__}")


def _canonical_json_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def validate_top_level_state(value: Mapping[str, object]) -> "TopLevelState":
    """Validate the complete JSON-only state at the checkpoint boundary."""

    if not isinstance(value, dict):
        raise TypeError("TopLevelState must be a JSON object")
    declared = set(TopLevelState.__annotations__)
    unknown = set(value).difference(declared)
    if unknown:
        raise ValueError(
            "TopLevelState contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    missing = set(TopLevelState.__required_keys__).difference(value)
    if missing:
        raise ValueError(
            "TopLevelState is missing required fields: " + ", ".join(sorted(missing))
        )

    for name, field_value in value.items():
        validate_state_field(name, field_value)

    _validate_workflow_cursor(value)

    serialized_bytes = _canonical_json_size(value)
    if serialized_bytes > TOP_LEVEL_STATE_HARD_MAX_BYTES:
        raise ValueError(
            "TopLevelState exceeds hard limit "
            f"{TOP_LEVEL_STATE_HARD_MAX_BYTES} bytes: {serialized_bytes}"
        )
    if serialized_bytes > TOP_LEVEL_STATE_TARGET_BYTES:
        warnings.warn(
            "TopLevelState exceeds target "
            f"{TOP_LEVEL_STATE_TARGET_BYTES} bytes: {serialized_bytes}",
            CheckpointBudgetWarning,
            stacklevel=2,
        )
    return cast(TopLevelState, dict(value))


def validate_state_field(name: str, value: object) -> None:
    """Validate one pending state-channel write before a saver persists it."""

    if name not in TopLevelState.__annotations__:
        raise ValueError(f"unknown TopLevelState field: {name}")
    _validate_json_value(value, path=name)
    if name == "stage_outputs":
        validate_stage_outputs(cast(Mapping[str, object] | None, value))
    elif name == "stage_seeds":
        validate_stage_outputs(cast(Mapping[str, object] | None, value))
    elif name == "workflow_steps":
        validate_workflow_steps(value)
    elif name in {"workflow_step_index", "entered_workflow_step_index"}:
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
    elif name == "stage_plan_ref":
        if not isinstance(value, str) or not value.strip():
            raise TypeError("stage_plan_ref must be a non-empty string")
    elif name == "resource_refs":
        validate_resource_refs(cast(Mapping[str, object] | None, value))
    elif name == "active_interrupt":
        _validate_active_interrupt(value)
    elif name == "last_error":
        _validate_last_error(value)
    elif name == "review_notes":
        _validate_review_notes(value)
    elif name == "route_trace":
        _validate_route_trace(value)
    if _canonical_json_size(value) > TOP_LEVEL_STATE_HARD_MAX_BYTES:
        raise ValueError(
            f"{name} exceeds whole-state hard limit "
            f"{TOP_LEVEL_STATE_HARD_MAX_BYTES} bytes"
        )


def _validate_workflow_cursor(value: Mapping[str, object]) -> None:
    """Validate the compact plan cursor against its immutable projection."""

    steps = validate_workflow_steps(value.get("workflow_steps"))
    index = value.get("workflow_step_index")
    entered = value.get("entered_workflow_step_index")
    current_stage = value.get("current_stage")
    if type(index) is not int or not 0 <= index <= len(steps):
        raise ValueError("workflow_step_index is outside workflow_steps")
    if type(entered) is not int or not -1 <= entered < len(steps):
        raise ValueError("entered_workflow_step_index is outside workflow_steps")
    if entered > index or index - entered > 1:
        raise ValueError(
            "entered_workflow_step_index must identify the active or just-completed "
            "workflow step"
        )
    expected_stage: object = "done" if index == len(steps) else steps[index]["stage"]
    if current_stage != expected_stage:
        raise ValueError(
            "current_stage does not match workflow_step_index: "
            f"expected {expected_stage!r}, got {current_stage!r}"
        )
    if index == 0 and entered not in {-1, 0}:
        raise ValueError("the initial workflow cursor has an invalid entered index")


def _validate_active_interrupt(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise TypeError("active_interrupt must be a JSON object or null")
    unknown = set(value).difference(ActiveInterrupt.__annotations__)
    if unknown:
        raise ValueError(
            "active_interrupt contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    missing = set(ActiveInterrupt.__required_keys__).difference(value)
    if missing:
        raise ValueError(
            "active_interrupt is missing required fields: " + ", ".join(sorted(missing))
        )
    if value.get("stage") not in DOMAIN_STAGES:
        raise ValueError("active_interrupt.stage must be a known domain stage")
    workflow_step_id = value.get("workflow_step_id")
    if not isinstance(workflow_step_id, str) or not workflow_step_id.strip():
        raise ValueError("active_interrupt.workflow_step_id must be a non-empty string")
    request_ref = value.get("request_ref")
    if request_ref is not None:
        from app.schemas.artifacts import ArtifactRef

        try:
            parsed_ref = ArtifactRef.model_validate(request_ref)
        except Exception as exc:
            raise ValueError(
                "active_interrupt.request_ref must be a valid ArtifactRef"
            ) from exc
        request_hash = value.get("request_hash")
        if request_hash is not None and request_hash != parsed_ref.sha256:
            raise ValueError(
                "active_interrupt.request_hash must match request_ref.sha256"
            )
    serialized_bytes = _canonical_json_size(value)
    if serialized_bytes > ACTIVE_INTERRUPT_MAX_BYTES:
        raise ValueError(
            "active_interrupt exceeds hard limit "
            f"{ACTIVE_INTERRUPT_MAX_BYTES} bytes: {serialized_bytes}"
        )


def _validate_last_error(last_error: object) -> None:
    if last_error is not None and not isinstance(last_error, str):
        raise TypeError("last_error must be a string or null")
    if isinstance(last_error, str) and len(last_error) > LAST_ERROR_MAX_CHARS:
        raise ValueError(f"last_error exceeds {LAST_ERROR_MAX_CHARS} characters")


def _validate_review_notes(notes: object) -> None:
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise TypeError("review_notes must be a list of strings")
    if len(notes) > REVIEW_NOTES_MAX_ITEMS:
        raise ValueError(f"review_notes exceeds {REVIEW_NOTES_MAX_ITEMS} items")
    for index, note in enumerate(notes):
        if len(note) > REVIEW_NOTE_MAX_CHARS:
            raise ValueError(
                f"review_notes[{index}] exceeds {REVIEW_NOTE_MAX_CHARS} characters; "
                "publish long notes as artifacts"
            )


def _validate_route_trace(route_trace: object) -> None:
    if not isinstance(route_trace, list) or not all(
        isinstance(item, str) for item in route_trace
    ):
        raise TypeError("route_trace must be a list of strings")
    if len(route_trace) > ROUTE_TRACE_LIMIT:
        raise ValueError(f"route_trace exceeds {ROUTE_TRACE_LIMIT} entries")


class ActiveInterrupt(TypedDict, total=False):
    """Bounded checkpoint pointer to an immutable human-review request."""

    stage: Required[DomainStage]
    workflow_step_id: Required[str]
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


class ReviewTarget(TypedDict):
    """Exact assembly handoff occurrence consumed by one Review step."""

    resource_type: Literal["assembly"]
    producer_step_id: str


class WorkflowStep(TypedDict):
    """One immutable occurrence in the frozen linear workflow plan."""

    step_id: str
    stage: DomainStage
    allow_local_execution: bool
    review_target: NotRequired[ReviewTarget]


class TopLevelState(TypedDict, total=False):
    # Stable run identity and storage root.
    session_id: Required[str]
    thread_id: NotRequired[str]
    data_dir: Required[str]

    # Operator-level lifecycle for the whole run.
    control_state: Required[ControlState]

    # Current supervision status used by routing, review gates, and entrypoints.
    supervision_state: Required[SupervisionState]

    # Workflow position.  The frozen RunConfig is authoritative; these fields
    # are its immutable, checkpoint-sized routing projection.
    current_stage: Required[DomainStage]
    workflow_steps: Required[Annotated[list[WorkflowStep], freeze_workflow_steps]]
    workflow_step_index: Required[int]
    entered_workflow_step_index: Required[int]

    # Durable references to external contracts and compact artifact indexes.
    input_manifest_ref: Required[str]
    asset_registry_ref: NotRequired[dict[str, Any] | None]
    stage_plan_ref: Required[str]

    # Immutable per-stage configuration seeds projected from the frozen
    # RunConfig.  A repeated occurrence starts from this seed rather than a
    # prior occurrence's completed cursor.
    stage_seeds: Required[Annotated[StageOutputs, freeze_stage_seeds]]

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
    stage_outputs: Required[Annotated[StageOutputs, replace_stage_outputs]]

    # Central resource reference table for cross-stage resource handoff.
    # Maps logical resource names to their current locations and metadata.
    resource_refs: NotRequired[Annotated[ResourceRefs, replace_resource_refs]]

    # Stage skip reasons keyed by workflow step ID.
    skip_reasons: Required[dict[str, str]]


class StateUpdate(TypedDict, total=False):
    """Partial top-level state delta returned by graph nodes."""

    session_id: str
    thread_id: str
    data_dir: str
    control_state: ControlState
    supervision_state: SupervisionState
    current_stage: DomainStage
    workflow_steps: list[WorkflowStep]
    workflow_step_index: int
    entered_workflow_step_index: int
    input_manifest_ref: str
    asset_registry_ref: dict[str, Any] | None
    stage_plan_ref: str
    stage_seeds: StageOutputs
    active_interrupt: ActiveInterrupt | None
    shared_parameters: SharedParameters | dict[str, Any] | None
    last_error: str | None
    review_notes: list[str]
    route_trace: list[str]
    stage_outputs: StageOutputs
    resource_refs: ResourceRefs
    skip_reasons: dict[str, str]


def initial_state(
    *,
    session_id: str,
    input_manifest_ref: str,
    data_dir: str = "data",
    workflow_steps: list[WorkflowStep] | None = None,
    stage_plan_ref: str = "control/run_config.json",
    stage_seeds: StageOutputs | None = None,
) -> TopLevelState:
    """Build the minimal parent graph state."""
    steps = validate_workflow_steps(
        workflow_steps
        or [
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            }
        ]
    )
    return TopLevelState(
        session_id=session_id,
        thread_id=session_id,
        data_dir=data_dir,
        control_state="running",
        supervision_state="idle",
        current_stage=steps[0]["stage"],
        workflow_steps=steps,
        workflow_step_index=0,
        entered_workflow_step_index=-1,
        input_manifest_ref=input_manifest_ref,
        asset_registry_ref=None,
        stage_plan_ref=stage_plan_ref,
        stage_seeds=validate_stage_outputs(stage_seeds),
        active_interrupt=None,
        shared_parameters=None,
        last_error=None,
        review_notes=[],
        route_trace=[],
        stage_outputs=validate_stage_outputs({}),
        resource_refs=validate_resource_refs({}),
        skip_reasons={},
    )
