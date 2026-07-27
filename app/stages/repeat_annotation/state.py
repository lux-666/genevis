"""Typed checkpoint boundary for the repeat-annotation stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias, cast

from pydantic import ConfigDict, TypeAdapter, with_config
from pydantic import JsonValue as PydanticJsonValue
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.execution import MethodExecutionAttemptStatus
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)

JsonObject: TypeAlias = dict[str, PydanticJsonValue]
RepeatStrategy: TypeAlias = Literal["provided", "de_novo"]
RepeatConfidence: TypeAlias = Literal["high", "low"]


@with_config(ConfigDict(extra="forbid"))
class MethodExecutionAttemptJson(TypedDict):
    """Compact recovery cursor owned by the shared execution lifecycle."""

    schema_version: Literal[2]
    attempt_id: str
    method_id: str
    status: MethodExecutionAttemptStatus
    frozen_attempt_ref: ArtifactRefJson
    execution_ref: ArtifactRefJson | None


@with_config(ConfigDict(extra="forbid"))
class RepeatAnnotationStageCursor(TypedDict, total=False):
    """All compact JSON values owned by the repeat-annotation stage."""

    # Run configuration and shared execution-boundary fields.
    execution_mode: str
    primary_method: str
    selected_methods: list[str]
    env: dict[str, str]
    review_policy: JsonObject
    status: str
    reason: str
    failure_kind: str
    method_name: str
    env_target: str
    missing_tools: list[str]
    execution_attempts: list[MethodExecutionAttemptJson]

    # Input resolution and strategy selection.
    input_assembly_ref: str
    provided_repeat_library_refs: list[str]
    provided_repeat_library_paths: list[str]
    confidence: RepeatConfidence
    low_confidence_reasons: list[str]
    strategy: RepeatStrategy
    repeat_library_source: RepeatStrategy

    # Library and masking plans.
    repeat_library_ref: str
    library_candidate_methods: list[str]
    helper_methods: list[str]
    method_selection_required: bool
    masking_method: str
    candidate_methods: list[str]
    expected_outputs: dict[str, str]
    repeat_annotation_gff3_ref: str
    softmasked_genome_ref: str
    hardmasked_genome_ref: str | None
    plan_artifact_ref: str
    artifact_refs: list[str]
    notes: list[str]
    execution_enabled: bool
    repeatmasker_attempt_id: str
    repeat_percentage: float | None

    # Review and committed handoff fields.
    review_request_artifact_ref: JsonObject
    review_policy_outcome: JsonObject
    auto_decidable: bool
    pending_human_confirmation: bool
    decision: str
    mask_review_decision_ref: JsonObject
    decided_by: str
    rationale_artifact_ref: str | None
    repeat_annotation: str
    softmasked_genome: str
    bundle_artifact_ref: str


REPEAT_ANNOTATION_STAGE_CURSOR_ADAPTER = TypeAdapter(RepeatAnnotationStageCursor)


def validated_repeat_annotation_payload(
    payload: Mapping[str, object],
) -> RepeatAnnotationStageCursor:
    """Validate and narrow a partial cursor received from a shared service."""

    return REPEAT_ANNOTATION_STAGE_CURSOR_ADAPTER.validate_python(payload)


def repeat_annotation_output(
    state: TopLevelState,
) -> RepeatAnnotationStageCursor:
    """Validate and narrow the stage cursor at its checkpoint read boundary."""

    return validated_stage_output(
        state,
        "repeat_annotation",
        REPEAT_ANNOTATION_STAGE_CURSOR_ADAPTER,
    )


def update_repeat_annotation_output(
    state: TopLevelState,
    payload: RepeatAnnotationStageCursor,
) -> StateUpdate:
    """Merge, validate, and JSON-serialize a repeat-annotation cursor update."""

    return update_validated_stage_output(
        state,
        "repeat_annotation",
        cast(dict[str, object], payload),
        REPEAT_ANNOTATION_STAGE_CURSOR_ADAPTER,
    )
