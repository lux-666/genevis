"""Typed checkpoint boundary for the read-QC stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import (
    JsonValue,
    StateUpdate,
    TopLevelState,
    validate_stage_outputs,
)
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)


@with_config(ConfigDict(extra="forbid"))
class ExecutionAttemptJson(TypedDict, total=False):
    schema_version: Literal[2]
    attempt_id: str
    method_id: str
    status: Literal["frozen", "executed", "execution_failed"]
    frozen_attempt_ref: ArtifactRefJson
    execution_ref: ArtifactRefJson | None


@with_config(ConfigDict(extra="forbid"))
class SeqkitSummaryJson(TypedDict, total=False):
    files: int
    num_seqs: int | None
    sum_len: int | None
    total_bases: int | None
    min_len: int | None
    avg_len: float | None
    max_len: int | None
    n50: int | None
    q20_percent: float | None
    q30_percent: float | None
    avg_qual: float | None


@with_config(ConfigDict(extra="forbid"))
class LengthThresholdsJson(TypedDict, total=False):
    min_len: int


@with_config(ConfigDict(extra="forbid"))
class ReadQCSubstepJson(TypedDict, total=False):
    name: str
    status: str
    method_name: str
    skip_reason: str
    artifact_ref: str
    artifact_refs: list[str]
    summary_ref: str
    post_filter_stats_ref: str
    post_filter_summary_ref: str
    thresholds: LengthThresholdsJson


@with_config(ConfigDict(extra="forbid"))
class LengthFilterRequestJson(TypedDict, total=False):
    method_name: str
    method: str
    approved: bool
    human_approved: bool
    thresholds: LengthThresholdsJson


@with_config(ConfigDict(extra="forbid"))
class ReadQCStageCursor(TypedDict, total=False):
    """All JSON-only fields owned by ``stage_outputs.read_qc``."""

    execution_mode: Literal["local"]
    env: dict[str, str]
    status: str
    reason: str
    method_name: str
    package_name: str
    skip_reason: str
    input_asset_ref: str | None
    input_asset_refs: list[str]
    input_artifact_refs: list[ArtifactRefJson]
    input_member_platforms: list[str]
    input_member_formats: list[str]
    raw_long_reads_path: str
    raw_long_reads_paths: list[str]
    platform: str | None
    format: str | None
    input_source: str | None
    blocked_input_ref: str
    blocked_input_refs: list[str]
    env_target: str
    missing_tools: list[str]
    execution_attempts: list[ExecutionAttemptJson]
    stats_output_path: str
    artifact_refs: list[str]
    stats_artifact_ref: str
    summary_artifact_ref: str
    raw_stats_artifact_ref: str
    raw_summary_artifact_ref: str
    post_filter_stats_artifact_ref: str
    post_filter_summary_artifact_ref: str
    summary: SeqkitSummaryJson
    raw_summary: SeqkitSummaryJson
    post_filter_summary: SeqkitSummaryJson
    final_read_set: Literal["raw", "filtered", "post_filter"]
    length_filter_request: LengthFilterRequestJson
    length_filter_method: str
    length_filter_decision: str
    filtered_long_reads_ref: str
    filtered_long_reads_refs: list[str]
    filtered_long_reads_artifact_refs: list[ArtifactRefJson]
    read_set_manifest_ref: ArtifactRefJson
    substeps: dict[str, ReadQCSubstepJson]


READ_QC_STAGE_CURSOR_ADAPTER = TypeAdapter(ReadQCStageCursor)


def read_read_qc_stage_output(state: TopLevelState) -> ReadQCStageCursor:
    """Validate and narrow the read-QC cursor at its read boundary."""

    return validated_stage_output(
        state,
        "read_qc",
        READ_QC_STAGE_CURSOR_ADAPTER,
    )


def update_read_qc_stage_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Validate and serialize one read-QC cursor update."""

    return update_validated_stage_output(
        state,
        "read_qc",
        payload,
        READ_QC_STAGE_CURSOR_ADAPTER,
    )


def replace_read_qc_stage_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Replace the cursor after validating its compact committed shape."""

    cursor = READ_QC_STAGE_CURSOR_ADAPTER.validate_python(payload)
    serialized = serialize_read_qc_stage_output(cursor)
    outputs = dict(state.get("stage_outputs") or {})
    outputs["read_qc"] = serialized
    return {"stage_outputs": validate_stage_outputs(outputs)}


def serialize_read_qc_stage_output(
    cursor: ReadQCStageCursor,
) -> dict[str, JsonValue]:
    """Serialize a validated cursor for dynamic field selection at commit."""

    serialized = READ_QC_STAGE_CURSOR_ADAPTER.dump_python(cursor, mode="json")
    if not isinstance(serialized, dict):
        raise TypeError("read_qc cursor must serialize to a JSON object")
    return cast(dict[str, JsonValue], serialized)
