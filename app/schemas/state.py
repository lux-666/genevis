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

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
StageCursor: TypeAlias = dict[str, JsonValue]
StageOutputs: TypeAlias = dict[DomainStage, StageCursor]
ResourceRefs: TypeAlias = dict[str, dict[str, JsonValue]]

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
    if value.get("stage") not in DOMAIN_STAGES:
        raise ValueError("active_interrupt.stage must be a known domain stage")
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
    asset_registry_ref: NotRequired[dict[str, Any] | None]
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
    stage_outputs: Required[Annotated[StageOutputs, replace_stage_outputs]]

    # Central resource reference table for cross-stage resource handoff.
    # Maps logical resource names to their current locations and metadata.
    resource_refs: NotRequired[Annotated[ResourceRefs, replace_resource_refs]]

    # Stage skip reasons keyed by stage name.
    skip_reasons: Required[dict[DomainStage, str]]


class StateUpdate(TypedDict, total=False):
    """Partial top-level state delta returned by graph nodes."""

    session_id: str
    thread_id: str
    data_dir: str
    control_state: ControlState
    supervision_state: SupervisionState
    current_stage: DomainStage
    requested_stages: list[DomainStage] | None
    input_manifest_ref: str
    asset_registry_ref: dict[str, Any] | None
    stage_plan_ref: str | None
    active_interrupt: ActiveInterrupt | None
    shared_parameters: SharedParameters | dict[str, Any] | None
    last_error: str | None
    review_notes: list[str]
    route_trace: list[str]
    stage_outputs: StageOutputs
    resource_refs: ResourceRefs
    skip_reasons: dict[DomainStage, str]


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
        stage_outputs=validate_stage_outputs({}),
        resource_refs=validate_resource_refs({}),
        skip_reasons={},
    )
