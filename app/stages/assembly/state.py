"""Typed checkpoint boundary for the Assembly stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias, cast

from pydantic import ConfigDict, TypeAdapter, with_config
from pydantic import JsonValue as PydanticJsonValue
from typing_extensions import TypedDict

from app.domains.assembly.selection import (
    AssemblySelectionSource,
    NormalizedReadType,
)
from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)

JsonObject: TypeAlias = dict[str, PydanticJsonValue]
AssemblyRoute: TypeAlias = Literal["auto", "cyclone", "ont", "hifi"]
AssemblyTargetLevel: TypeAlias = Literal["draft", "near_t2t", "strict_t2t"]


@with_config(ConfigDict(extra="forbid"))
class ResolvedGenomeSizeCursor(TypedDict):
    """JSON representation of the canonical genome-size resolution."""

    value_bp: int
    source: str
    source_path: str


@with_config(ConfigDict(extra="forbid"))
class AssemblySelectionCursor(TypedDict, total=False):
    """JSON representation of the canonical Assembly selection."""

    read_type: str
    primary_method: str
    source: AssemblySelectionSource
    rationale: str
    metadata: JsonObject


@with_config(ConfigDict(extra="forbid"))
class AssemblyInputProfileCursor(TypedDict, total=False):
    """JSON representation of the persisted input profile."""

    session_id: str
    primary_long_reads_ref: str | list[str] | None
    primary_long_read_platform: str | None
    ont_reads_ref: str | None
    hifi_reads_ref: str | None
    hic_reads_ref: str | None
    estimated_coverage: float | None
    estimated_genome_size: int | None
    target_level: AssemblyTargetLevel
    has_hifi: bool
    has_ont: bool
    has_hic: bool
    has_rnaseq: bool
    recommended_route: Literal["cyclone", "ont", "hifi", "insufficient_data"]
    read_type: str
    recommendation_reasoning: str
    uncertainty_notes: list[str]
    profile_source: Literal[
        "manifest", "run_config", "run_config_explicit", "llm_inferred"
    ]
    notes: list[str]
    metadata: JsonObject


@with_config(ConfigDict(extra="forbid"))
class AssemblyCandidatePlanCursor(TypedDict, total=False):
    """JSON representation of one candidate plan."""

    method_name: str
    status: Literal["compatible", "rejected", "selected"]
    role: Literal["candidate", "primary"]
    required_inputs: list[str]
    optional_inputs: list[str]
    requires_genome_size: bool
    rejection_reason: str | None
    metadata: JsonObject


@with_config(ConfigDict(extra="forbid"))
class AssemblyActiveInterruptCursor(TypedDict, total=False):
    """Assembly's compact copy of an unresolved review request pointer."""

    stage: Literal["assembly"]
    gate_id: str | None
    reason: str
    blocked_at: str
    review_request_id: str
    request_ref: JsonObject
    request_hash: str
    attempt_id: int
    previous_attempt_id: int | None
    task: str
    recommendation_hash: str


@with_config(ConfigDict(extra="forbid"))
class AssemblyStageCursor(TypedDict, total=False):
    """All compact JSON values owned or consumed by the Assembly stage."""

    status: str
    reason: str
    block_reason: str
    method_name: str
    missing_params: list[str]
    invalid_params: list[JsonObject]

    assembly_route: AssemblyRoute
    target_level: AssemblyTargetLevel
    explicit_route: Literal["cyclone", "ont", "hifi"] | None
    selected_route: Literal["cyclone", "ont", "hifi"]
    normalized_read_type: NormalizedReadType | None
    primary_method: str
    execution_mode: str
    method_application_notes: list[str]

    primary_long_reads_ref: str
    primary_long_read_refs: list[str]
    primary_long_read_platform: str | None
    ont_reads_ref: str | None
    ultralong_read_refs: list[str]
    hifi_reads_ref: str | None
    hifi_read_refs: list[str]
    hic_reads_ref: str | None
    hic_r1_reads: list[str]
    hic_r2_reads: list[str]
    assembly_input_member_ids: dict[str, list[str]]
    assembly_input_member_platforms: dict[str, list[str]]
    assembly_input_artifact_refs: dict[str, list[ArtifactRefJson | None]]
    use_ultralong_ont: bool
    use_hic_phasing: bool
    input_ref: str | list[str]
    input_type: str
    input_source: str
    genome_survey_result_ref: str | None
    genome_size_prior: int | None
    resolved_genome_size: ResolvedGenomeSizeCursor | None
    platform: str | None
    has_hifi: bool
    has_hic: bool

    assembly_selection: AssemblySelectionCursor
    recommended_assembly_selection: AssemblySelectionCursor | None
    input_profile: AssemblyInputProfileCursor
    input_profile_ref: JsonObject
    route_decision: JsonObject
    route_decision_ref: JsonObject
    read_type_prior: dict[str, str] | None
    shared_parameters: JsonObject

    candidate_methods: list[str]
    candidate_plans: list[AssemblyCandidatePlanCursor]
    rejected_candidates: dict[str, str]
    planned_candidate_bundle_ref: ArtifactRefJson
    executed_candidate_bundle_ref: ArtifactRefJson

    artifact_refs: list[str]
    draft_assembly_ref: str
    substeps: dict[str, PydanticJsonValue]
    quality_gate: JsonObject

    assembly_execution_contract: JsonObject
    flye_parameters: JsonObject
    nextdenovo_parameters: JsonObject
    hifiasm_parameters: JsonObject
    hifiasm_ont_parameters: JsonObject
    flye_output_dir: str
    hifiasm_output_dir: str
    hifiasm_ont_output_dir: str
    nextdenovo_run_cfg_path: str
    nextdenovo_work_dir: str
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    threads: int
    read_profile: JsonObject

    current_parameter_recommendation: JsonObject
    current_parameter_attempt_id: int
    proposed_parameter_attempt_id: int | None
    parameter_attempts: list[JsonObject]
    parameter_recommendation: JsonObject
    parameter_recommendation_config: JsonObject
    parameter_retry_request: JsonObject | None
    parameter_rejection: JsonObject
    retry_counts: dict[str, int]
    review_metrics: JsonObject
    review_policy: JsonObject
    active_interrupt: AssemblyActiveInterruptCursor | None


ASSEMBLY_STAGE_CURSOR_ADAPTER = TypeAdapter(AssemblyStageCursor)


def validated_assembly_stage_payload(
    payload: Mapping[str, object],
) -> AssemblyStageCursor:
    """Validate and narrow a partial payload received from a shared service."""

    return ASSEMBLY_STAGE_CURSOR_ADAPTER.validate_python(payload)


def assembly_stage_output(state: TopLevelState) -> AssemblyStageCursor:
    """Validate and narrow the Assembly cursor at its checkpoint boundary."""

    return validated_stage_output(
        state,
        "assembly",
        ASSEMBLY_STAGE_CURSOR_ADAPTER,
    )


def update_assembly_stage_output(
    state: TopLevelState,
    payload: AssemblyStageCursor,
) -> StateUpdate:
    """Merge a typed Assembly cursor update and serialize it as JSON."""

    return update_validated_stage_output(
        state,
        "assembly",
        cast(dict[str, object], payload),
        ASSEMBLY_STAGE_CURSOR_ADAPTER,
    )
