"""Small LangGraph state schema.

Canonical definition path: ``app/schemas/state.py``.

Large files, command logs, generated scripts, and metric tables belong in
artifacts or ledger records. The checkpointed state stores control fields and
references only.
"""

import json
import math
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

from app.schemas.limits import STAGE_CURSOR_MAX_BYTES, STAGE_OUTPUTS_MAX_BYTES
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

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
StageCursor: TypeAlias = dict[str, JsonValue]
StageOutputs: TypeAlias = dict[DomainStage, StageCursor]

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

_StageKey = TypeVar("_StageKey", bound=str)


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
    stage_outputs: Required[Annotated[StageOutputs, replace_stage_outputs]]

    # Central resource reference table for cross-stage resource handoff.
    # Maps logical resource names to their current locations and metadata.
    resource_refs: NotRequired[dict[str, dict[str, Any]]]

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
        stage_outputs=validate_stage_outputs({}),
        resource_refs={},
        skip_reasons={},
    )
