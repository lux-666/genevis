"""Typed checkpoint boundary for the assembly-dedup stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.execution import MethodExecutionAttemptStatus
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState
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
    purge_dups_status: Literal[
        "not_started", "pending", "skipped", "completed", "failed"
    ]
    purge_dups_skip_reason: str
    execution_mode: str
    env: dict[str, str]
    review_policy: dict[str, int]
    env_target: str
    missing_tools: list[str]
    method_name: str

    candidate_bundle_ref: ArtifactRefJson
    candidate_id: str | None
    input_primary_output_ref: ArtifactRefJson
    input_assembly_ref: str
    output_assembly_ref: str
    input_member_ids: list[str]
    input_member_platforms: list[str]
    cutoffs_strategy: Literal["calcuts_auto", "none"]
    automatic_purge_approved: bool
    threads: int
    get_seqs_exclude_haplotigs: bool
    recommendation_mode: Literal["advisor", "deterministic_baseline"]
    recommendation_output_method: Literal[
        "prompt_json", "json_schema", "function_calling"
    ]
    max_attempts: int
    coverage_mode: Literal["read_depth", "self_alignment"]
    self_alignment_preset: Literal["asm5", "asm10", "asm20"]
    current_attempt: int
    history_refs: list[ArtifactRefJson]
    recommendation_ref: ArtifactRefJson
    active_interrupt: ActiveInterrupt | None
    decision_ref: ArtifactRefJson
    adopted_attempt: int | None
    quast_report_ref: ArtifactRefJson
    quast_execution_ref: ArtifactRefJson

    execution_attempts: list[MethodExecutionAttemptJson]
    purged_assembly_ref: str | None
    purged_candidate_bundle_ref: ArtifactRefJson | None
    purged_primary_output_ref: ArtifactRefJson | None
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
