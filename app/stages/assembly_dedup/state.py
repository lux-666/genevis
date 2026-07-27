"""Typed checkpoint boundary for the assembly-dedup stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.execution import MethodExecutionAttemptStatus
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)


@with_config(ConfigDict(extra="forbid"))
class MethodExecutionAttemptJson(TypedDict):
    """Compact direct-execution recovery cursor."""

    schema_version: Literal[2]
    attempt_id: str
    method_id: str
    status: MethodExecutionAttemptStatus
    frozen_attempt_ref: ArtifactRefJson
    execution_ref: ArtifactRefJson | None


@with_config(ConfigDict(extra="forbid"))
class AssemblyDedupCursor(TypedDict, total=False):
    """All compact JSON values owned by assembly deduplication."""

    status: str
    reason: str
    failure_kind: str
    skip_reason: str
    execution_mode: str
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    method_name: str

    selected_candidate_refs: list[str]
    candidate_ref: str
    candidate_id: str | None
    input_assembly_ref: str
    output_assembly_ref: str
    long_reads_ref: str
    long_read_refs: list[str]
    read_mapping_preset: Literal["map-hifi", "map-pb", "map-ont"]
    cutoffs_strategy: Literal["calcuts_auto"]
    get_seqs_exclude_haplotigs: bool

    execution_attempts: list[MethodExecutionAttemptJson]
    purged_assembly_ref: str | None
    purged_candidate_ref: str | None
    artifact_refs: list[str]


ASSEMBLY_DEDUP_CURSOR_ADAPTER = TypeAdapter(AssemblyDedupCursor)


def assembly_dedup_output(state: TopLevelState) -> AssemblyDedupCursor:
    """Validate and narrow the dedup cursor at its read boundary."""

    return validated_stage_output(
        state,
        "assembly_dedup",
        ASSEMBLY_DEDUP_CURSOR_ADAPTER,
    )


def update_assembly_dedup_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge, validate, and JSON-serialize a dedup cursor update."""

    return update_validated_stage_output(
        state,
        "assembly_dedup",
        payload,
        ASSEMBLY_DEDUP_CURSOR_ADAPTER,
    )
