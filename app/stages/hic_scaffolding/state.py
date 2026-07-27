"""Typed checkpoint boundary for the Hi-C scaffolding stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias

from pydantic import (
    ConfigDict,
    TypeAdapter,
    with_config,
)
from pydantic import (
    JsonValue as PydanticJsonValue,
)
from typing_extensions import TypedDict

from app.schemas.artifacts import (
    ArtifactRefJson,
    ParameterAttemptStatus,
    ParameterAttemptTask,
    ParameterRecommendationStatus,
)
from app.schemas.parameter_cursors import ParameterAttemptJson, ParameterRecommendationJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)

JsonObject: TypeAlias = dict[str, PydanticJsonValue]


@with_config(ConfigDict(extra="forbid"))
class HiCInterruptJson(TypedDict, total=False):
    """Compact pointer to an unresolved Hi-C review request."""

    stage: Literal["hic_scaffolding"]
    gate_id: str | None
    reason: str
    blocked_at: str
    review_request_id: str
    request_ref: ArtifactRefJson
    request_hash: str
    attempt_id: int
    previous_attempt_id: int | None
    task: ParameterAttemptTask
    recommendation_hash: str


@with_config(ConfigDict(extra="forbid"))
class AutoHiCParametersJson(TypedDict, total=False):
    """Run-config values accepted by the AutoHiC adapter."""

    restriction_enzyme: str
    threads: int
    python_command: str
    number_of_edit_rounds: int
    translocation_adjust: bool
    inversion_adjust: bool
    debris_adjust: bool
    error_max_epoch: int
    error_min_len: int
    error_max_len: int
    error_filter_score: float
    error_filter_iou_score: float


@with_config(ConfigDict(extra="forbid"))
class ParameterRetryPolicyJson(TypedDict, total=False):
    """Operator-owned parameter retry policy."""

    retry_on_execution_failure: bool
    max_execution_retries: int
    max_review_retries: int


@with_config(ConfigDict(extra="forbid"))
class ParameterRetryRequestJson(TypedDict, total=False):
    """Ref-only cursor binding a retry to its sealed predecessor."""

    origin: Literal["autohic_execution", "autohic_review"]
    previous_attempt_id: int
    previous_attempt_seal_hash: str
    failure_ref: ArtifactRefJson


@with_config(ConfigDict(extra="forbid"))
class AutoHiCCandidateJson(TypedDict, total=False):
    """Reviewable result produced by one AutoHiC execution."""

    status: Literal["executed", "failed"]
    scaffold_fasta: str
    contact_map: str | None
    assembly_file: str | None
    html_report: str | None
    review_bundle_ref: str
    qc_report_ref: str
    error: str | None


@with_config(ConfigDict(extra="forbid"))
class HiCScaffoldingCursor(TypedDict, total=False):
    """All compact JSON values owned by the Hi-C scaffolding stage."""

    execution_mode: Literal["local"]
    env: dict[str, str]
    autohic_parameters: AutoHiCParametersJson
    parameter_retry_policy: ParameterRetryPolicyJson
    review_policy: JsonObject

    status: str
    reason: str
    failure_kind: str
    block_reason: str
    method_name: str
    selected_method_name: str
    missing_requirements: list[str]
    invalid_params: list[JsonObject]
    env_target: str
    missing_tools: list[str]
    notes: list[str]

    pre_hic_assembly_ref: str | None
    output_assembly_ref: str | None
    hic_read_refs: list[str]
    hic_read_paths: list[str]
    hic_library_metadata: list[JsonObject]
    target_assembly_level: str
    assembly_source: str
    species_name: str

    active_interrupt: HiCInterruptJson | None
    review_request_artifact_ref: ArtifactRefJson
    review_decision_artifact_ref: ArtifactRefJson
    review_policy_outcome: JsonObject
    auto_decidable: bool
    pending_human_confirmation: bool
    decision: str
    decided_by: str
    rationale_artifact_ref: str | None

    current_parameter_attempt_id: int | None
    proposed_parameter_attempt_id: int | None
    parameter_attempts: list[ParameterAttemptJson]
    current_parameter_recommendation: ParameterRecommendationJson
    parameter_retry_request: ParameterRetryRequestJson | None
    retry_counts: dict[str, int]

    chromosome_scaffolded_assembly_candidate_ref: str
    hic_review_bundle_ref: str
    hic_qc_report_ref: str
    method_candidates: dict[str, AutoHiCCandidateJson]
    artifact_refs: list[str]
    autohic_error: str | None


HIC_SCAFFOLDING_CURSOR_ADAPTER = TypeAdapter(HiCScaffoldingCursor)


def hic_scaffolding_output(state: TopLevelState) -> HiCScaffoldingCursor:
    """Validate and narrow the Hi-C cursor at its checkpoint boundary."""

    return validated_stage_output(
        state,
        "hic_scaffolding",
        HIC_SCAFFOLDING_CURSOR_ADAPTER,
    )


def update_hic_scaffolding_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge, validate, and JSON-serialize a Hi-C cursor update."""

    return update_validated_stage_output(
        state,
        "hic_scaffolding",
        payload,
        HIC_SCAFFOLDING_CURSOR_ADAPTER,
    )
