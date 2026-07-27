"""Typed checkpoint boundary for the T2T stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.parameter_cursors import ParameterAttemptJson, ParameterRecommendationJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)
from app.stages.t2t.parameters.catalog import T2TMode


@with_config(ConfigDict(extra="forbid"))
class T2TOntReadInputJson(TypedDict):
    """One canonical ONT input stored in the compact stage cursor."""

    asset_id: str
    path: str


@with_config(ConfigDict(extra="forbid"))
class T2TThresholdsJson(TypedDict):
    """Resolved T2T thresholds mirrored from frozen evidence."""

    min_motif_copies: int
    terminal_window_bp: int
    min_mapq: int
    max_target_terminal_distance_bp: int
    min_overlap_bp: int
    min_supporting_reads: int


@with_config(ConfigDict(extra="forbid"))
class T2TInterruptJson(TypedDict, total=False):
    """Persisted pointer to a T2T human-review request."""

    stage: Literal["t2t"]
    gate_id: str | None
    reason: str
    blocked_at: str
    review_request_id: str
    request_ref: ArtifactRefJson
    request_hash: str
    attempt_id: int
    previous_attempt_id: int | None
    task: str
    recommendation_hash: str


T2TStatus = Literal[
    "resolved",
    "motif_resolved",
    "parameter_response_invalid",
    "blocked_human",
    "parameters_approved",
    "parameters_ready",
    "assembly_scanned",
    "reads_prepared",
    "reads_scanned",
    "alignment_frozen",
    "alignment_ran",
    "alignment_skipped",
    "candidates_built",
    "patch_not_applied",
    "patched",
    "patched_validated",
    "skipped",
    "skipped_no_long_reads",
    "completed",
    "completed_with_warnings",
    "failed",
    "failed_terminal",
    "blocked_env",
    "missing_tool",
]


@with_config(ConfigDict(extra="forbid"))
class T2TStageCursor(TypedDict, total=False):
    """All JSON-serializable keys owned by the T2T stage."""

    # Run configuration copied into the stage slot.
    execution_mode: Literal["local"]
    env: dict[str, str]
    mode: T2TMode
    chromosome_list_ref: str | None
    assembly_fasta_ref: str | None
    ont_read_refs: list[str]

    # Input resolution and immutable execution inputs.
    status: T2TStatus
    reason: str
    failure_kind: str
    source_stage: str
    ont_read_inputs: list[T2TOntReadInputJson]
    ont_reads_source: Literal[
        "run_config", "clean_long_reads", "raw_long_reads", "missing"
    ]
    execution_enabled: bool
    input_assembly_ref: str
    out_dir: str
    work_dir: str
    output_assembly_ref: str
    notes: list[str]
    thresholds: T2TThresholdsJson

    # Motif-resolution evidence chain.
    resolved_telomere_motif: str
    resolved_telomere_motif_source: Literal[
        "provided_species_fact", "llm_species_resolution"
    ]
    resolved_telomere_motif_summary: str | None
    telomere_motif_input_snapshot_ref: ArtifactRefJson
    telomere_motif_model_request_ref: ArtifactRefJson
    telomere_motif_model_response_ref: ArtifactRefJson
    telomere_motif_response_parse_ref: ArtifactRefJson
    telomere_motif_resolution_ref: ArtifactRefJson
    telomere_motif_resolution_manifest_ref: ArtifactRefJson

    # Parameter recommendation, approval, and execution cursors.
    active_interrupt: T2TInterruptJson | None
    block_reason: str
    current_parameter_attempt_id: int
    proposed_parameter_attempt_id: int | None
    parameter_attempts: list[ParameterAttemptJson]
    current_parameter_recommendation: ParameterRecommendationJson

    # Tool preflight fields returned by the shared execution boundary.
    env_target: str
    missing_tools: list[str]
    method_name: str

    # Assembly/read scan and alignment evidence.
    assembly_telomere_scan_ref: str
    assembly_sequence_count: int
    assembly_missing_end_count: int
    ont_read_paths: list[str]
    reads_telomere_scan_ref: str
    telomeric_reads_ref: str
    telomeric_reads_fasta_ref: str
    read_count: int
    telomeric_read_count: int
    candidate_skip_reason: str
    alignment_paf_ref: str
    alignment_paf_artifact_ref: ArtifactRefJson
    telomere_extension_candidates_ref: str
    telomere_extension_candidates_artifact_ref: ArtifactRefJson
    candidate_count: int
    selected_patch_count: int
    proposed_candidate_count: int

    # Patch, report, and human-review outputs.
    patch_reason: str
    patched_assembly_ref: str | None
    patch_records_ref: str | None
    patched_telomere_scan_ref: str
    skip_reason: str
    telomere_status_summary_ref: str
    t2t_finishing_report_ref: str
    bundle_ref: str
    artifact_refs: list[str]
    review_request_artifact_ref: ArtifactRefJson
    auto_decidable: bool
    decision: Literal[
        "accept_selected_candidates", "continue_without_patch", "stop_pipeline"
    ]
    selected_candidate_ids: list[str]
    review_decision_artifact_ref: ArtifactRefJson
    decided_by: Literal["human", "llm_provisional", "auto_rule"]
    rationale_artifact_ref: str | None

    # Recognized only so enter_t2t can reject legacy checkpoints explicitly.
    configured_telomere_motif: str | None
    legacy_motif_hint: str | None
    telomere_motif: str | None
    telomere_motif_input_profile_ref: str | ArtifactRefJson | None


T2T_STAGE_CURSOR_ADAPTER = TypeAdapter(T2TStageCursor)


def read_t2t_stage_output(state: TopLevelState) -> T2TStageCursor:
    """Validate and narrow the T2T cursor at its read boundary."""

    return validated_stage_output(state, "t2t", T2T_STAGE_CURSOR_ADAPTER)


def update_t2t_stage_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge, validate, and JSON-serialize a T2T cursor update."""

    return update_validated_stage_output(
        state,
        "t2t",
        payload,
        T2T_STAGE_CURSOR_ADAPTER,
    )
