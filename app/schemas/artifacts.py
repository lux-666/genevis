"""Artifact metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from typing_extensions import TypedDict

from app.integrity import SHA256_PATTERN, sha256_file

READ_QC_LONG_READ_STATS = "read_qc.long_read_stats"
READ_QC_LONG_READ_AGGREGATE_STATS = "read_qc.long_read_aggregate_stats"
READ_QC_LONG_READ_SUMMARY = "read_qc.long_read_summary"
READ_QC_FILTERED_LONG_READS = "read_qc.filtered_long_reads"
READ_QC_READ_SET_MANIFEST = "read_qc.read_set_manifest"
READ_QC_LONG_READS_FOFN = "read_qc.long_reads.fofn"
READ_QC_FILTERED_LONG_READS_FOFN = "read_qc.filtered_long_reads.fofn"
GENOME_SURVEY_KMER_DB = "genome_survey.kmer_db"
GENOME_SURVEY_KMER_HIST = "genome_survey.kmer_hist"
GENOME_SURVEY_MODEL_SUMMARY = "genome_survey.model_summary"
ASSEMBLY_DRAFT_ASSEMBLY = "assembly.draft_assembly"
GAPCLOSER_ASSEMBLY = "gapcloser.assembly"
BASE_POLISH_ASSEMBLY = "base_polish.assembly"
ASSEMBLY_CANDIDATE_BUNDLE = "draft_assembly.candidate"
ASSEMBLY_PARAMETER_INPUT_SNAPSHOT = "assembly.parameter_recommendation.input_snapshot"
ASSEMBLY_PARAMETER_MODEL_REQUEST = "assembly.parameter.model_request"
ASSEMBLY_PARAMETER_MODEL_RESPONSE = "assembly.parameter.model_response"
ASSEMBLY_PARAMETER_BASIS = "assembly.parameter.basis_snapshot"
ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST = (
    "assembly.parameter.recommendation_manifest"
)
ASSEMBLY_PARAMETER_RETRIEVAL_QUERY = "assembly.parameter.retrieval_query"
ASSEMBLY_PARAMETER_RETRIEVAL_TRACE = "assembly.parameter.retrieval_trace"
ASSEMBLY_PARAMETER_EVIDENCE_PAYLOAD = "assembly.parameter.evidence_payload"
ASSEMBLY_PARAMETER_RESPONSE_PARSE = "assembly.parameter.response_parse"
ASSEMBLY_PARAMETER_COMMAND = "assembly.parameter.command"
ASSEMBLY_PARAMETER_FROZEN_PARAMETERS = "assembly.parameter.frozen_parameters"
ASSEMBLY_PARAMETER_REVIEW = "assembly.parameter.review"
ASSEMBLY_PARAMETER_ATTEMPT_MANIFEST = "assembly.parameter.attempt_manifest"
ASSEMBLY_PARAMETER_REVIEW_FAILURE = "assembly.parameter.review_failure"
ASSEMBLY_PARAMETER_RECOMMENDATION = "assembly.parameter.recommendation"
ASSEMBLY_PARAMETER_APPROVAL_DECISION = "assembly.parameter_approval.decision"
ASSEMBLY_REVIEW_EVAL_BUNDLE = "assembly_review.eval_bundle"
ASSEMBLY_REVIEW_QUAST_REPORT_TSV = "assembly_review.quast_report_tsv"
ASSEMBLY_REVIEW_QUAST_REPORT_TXT = "assembly_review.quast_report_txt"
ASSEMBLY_REVIEW_QUAST_SUMMARY = "assembly_review.quast_summary"
ASSEMBLY_REVIEW_BUSCO_SHORT_SUMMARY = "assembly_review.busco_short_summary"
ASSEMBLY_REVIEW_BUSCO_SUMMARY = "assembly_review.busco_summary"
ASSEMBLY_REVIEW_ACTION = "assembly_review.action"
ASSEMBLY_REVIEW_PACKET = "assembly_review.review_packet"
ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK = (
    "assembly_review.related_species_evidence_pack"
)
ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK = (
    "assembly_review.related_species_statistics_pack"
)
ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK = "assembly_review.comparative_notebook"
ASSEMBLY_CANDIDATE_COMPARISON = "assembly_review.candidate_comparison"
HIC_SCAFFOLDED_ASSEMBLY = "hic_scaffolding.chromosome_scaffolded_assembly"
HIC_REVIEW_BUNDLE = "hic_scaffolding.review_bundle"
HIC_QC_REPORT = "hic_scaffolding.qc_report"
HIC_CONTACT_MAP = "hic_scaffolding.contact_map"
HIC_ASSEMBLY_FILE = "hic_scaffolding.assembly"
HIC_HTML_REPORT = "hic_scaffolding.html_report"
HIC_PARAMETER_INPUT_SNAPSHOT = "hic_scaffolding.parameter_recommendation.input_snapshot"
HIC_PARAMETER_BASIS = "hic_scaffolding.parameter.basis_snapshot"
HIC_PARAMETER_MODEL_REQUEST = "hic_scaffolding.parameter.model_request"
HIC_PARAMETER_MODEL_RESPONSE = "hic_scaffolding.parameter.model_response"
HIC_PARAMETER_RECOMMENDATION = "hic_scaffolding.parameter.recommendation"
HIC_PARAMETER_RESPONSE_PARSE = "hic_scaffolding.parameter.response_parse"
HIC_PARAMETER_RECOMMENDATION_MANIFEST = (
    "hic_scaffolding.parameter.recommendation_manifest"
)
HIC_PARAMETER_APPROVAL_DECISION = "hic_scaffolding.parameter_approval.decision"
HIC_PARAMETER_FROZEN_PARAMETERS = "hic_scaffolding.parameter.frozen_parameters"
HIC_PARAMETER_ATTEMPT_MANIFEST = "hic_scaffolding.parameter.attempt_manifest"
HIC_PARAMETER_REVIEW = "hic_scaffolding.parameter.review"
AUTOHIC_PARAMETER_CONFIG = "hic_scaffolding.parameter.config"
AUTOHIC_PARAMETER_SCRIPT = "hic_scaffolding.parameter.script"
AUTOHIC_PARAMETER_COMMAND = "hic_scaffolding.parameter.command"
T2T_PARAMETER_INPUT_SNAPSHOT = "t2t.parameter_recommendation.input_snapshot"
T2T_PARAMETER_BASIS = "t2t.parameter.basis_snapshot"
T2T_PARAMETER_POLICY_REQUEST = "t2t.parameter.policy_request"
T2T_PARAMETER_POLICY_RESPONSE = "t2t.parameter.policy_response"
T2T_PARAMETER_RECOMMENDATION = "t2t.parameter.recommendation"
T2T_PARAMETER_RESPONSE_PARSE = "t2t.parameter.response_parse"
T2T_PARAMETER_RECOMMENDATION_MANIFEST = "t2t.parameter.recommendation_manifest"
T2T_PARAMETER_APPROVAL_DECISION = "t2t.parameter_approval.decision"
T2T_PARAMETER_FROZEN_PARAMETERS = "t2t.parameter.frozen_parameters"
T2T_MOTIF_INPUT_SNAPSHOT = "t2t.telomere_motif.input_snapshot"
T2T_MOTIF_MODEL_REQUEST = "t2t.telomere_motif.model_request"
T2T_MOTIF_MODEL_RESPONSE = "t2t.telomere_motif.model_response"
T2T_MOTIF_RESOLUTION = "t2t.telomere_motif.resolution"
T2T_MOTIF_RESPONSE_PARSE = "t2t.telomere_motif.response_parse"
T2T_MOTIF_RESOLUTION_MANIFEST = "t2t.telomere_motif.resolution_manifest"
T2T_READ_ALIGNMENT_PAF = "t2t.read_alignment_paf"
T2T_ALIGNMENT_OBSERVATIONS = "t2t.alignment_observations"
T2T_ASSEMBLY_TELOMERE_SCAN = "t2t.assembly_telomere_scan"
T2T_READS_TELOMERE_SCAN = "t2t.reads_telomere_scan"
T2T_TELOMERIC_READS = "t2t.telomeric_reads"
T2T_EXTENSION_CANDIDATES = "t2t.extension_candidates"
T2T_STATUS_SUMMARY = "t2t.status_summary"
T2T_FINISHING_REPORT = "t2t.finishing_report"
T2T_PATCHED_ASSEMBLY = "t2t.patched_assembly"
T2T_PATCH_RECORDS = "t2t.patch_records"


class ArtifactMeta(BaseModel):
    """Metadata stored in state/ledger instead of large file contents."""

    artifact_id: str
    file_path: Path
    file_hash: str
    file_size_bytes: int
    artifact_type: str
    stage: str
    method_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        stage: str,
        method_name: str,
        artifact_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> "ArtifactMeta":
        """Build artifact metadata for an existing file."""
        return cls(
            artifact_id=f"{stage}:{method_name}:{path.name}",
            file_path=path,
            file_hash=sha256_file(path),
            file_size_bytes=path.stat().st_size,
            artifact_type=artifact_type,
            stage=stage,
            method_name=method_name,
            metadata=metadata or {},
        )


class ArtifactRef(BaseModel):
    """Portable, index-backed reference to one immutable run artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    artifact_id: str = Field(min_length=1)
    path_ref: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    artifact_type: str = Field(min_length=1)

    @field_validator("path_ref")
    @classmethod
    def validate_path_ref(cls, value: str) -> str:
        """Require a session-root-relative, POSIX artifact reference."""

        if value.startswith("data/") or value.startswith("/"):
            raise ValueError("artifact path_ref must be relative to the run root")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact path_ref must not escape the run root")
        return value

    @classmethod
    def from_meta(cls, artifact: ArtifactMeta, *, path_ref: str) -> "ArtifactRef":
        """Build the exact ref returned by the v2 artifact-index boundary."""

        return cls(
            artifact_id=artifact.artifact_id,
            path_ref=path_ref,
            sha256=artifact.file_hash,
            size_bytes=artifact.file_size_bytes,
            artifact_type=artifact.artifact_type,
        )


def canonical_artifact_refs(refs: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    """Return unique refs in canonical order for set-like wire collections.

    For unordered collections, the identity and ordering fields are:
    ``artifact_id``, ``path_ref``, ``sha256``, ``size_bytes``, and
    ``artifact_type``. Schemas in which reference order has domain meaning must
    preserve that order instead of using this helper.
    """

    by_identity = {
        (
            ref.artifact_id,
            ref.path_ref,
            ref.sha256,
            ref.size_bytes,
            ref.artifact_type,
        ): ref
        for ref in refs
    }
    return tuple(by_identity[identity] for identity in sorted(by_identity))


class ArtifactRefJson(TypedDict):
    """JSON (TypedDict) twin of ArtifactRef — used in stage checkpoint cursors."""

    schema_version: Literal[2]
    artifact_id: str
    path_ref: str
    sha256: str
    size_bytes: int
    artifact_type: str


ParameterAttemptStatus = Literal[
    "recommended",
    "frozen",
    "execution_failed",
    "executed",
    "review_failed",
    "reviewed",
]
ParameterAttemptTask = Literal[
    "recommend_initial",
    "retry_after_execution_failure",
    "retry_after_review_failure",
]
ParameterRecommendationStatus = Literal[
    "response_invalid",
    "pending_approval",
    "approved",
    "rejected",
]


class ParameterRecommendationCursor(BaseModel):
    """Bounded checkpoint cursor for one immutable recommendation manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3]
    recommendation_id: int = Field(ge=0)
    method_id: str = Field(min_length=1)
    task: ParameterAttemptTask
    status: ParameterRecommendationStatus
    previous_attempt_id: int | None = Field(default=None, ge=0)
    manifest_ref: ArtifactRef
    recommendation_hash: str = Field(pattern=SHA256_PATTERN)
    review_request_id: str | None = None
    review_request_ref: ArtifactRef | None = None
    previous_attempt_seal_hash: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )

    @model_validator(mode="after")
    def validate_manifest_hash(self) -> "ParameterRecommendationCursor":
        if self.recommendation_hash != self.manifest_ref.sha256:
            raise ValueError("recommendation_hash must match manifest_ref.sha256")
        if self.review_request_ref is None and self.review_request_id is not None:
            raise ValueError("review_request_id requires review_request_ref")
        return self


class ParameterAttempt(BaseModel):
    """Compact execution-attempt cursor; domain facts live in referenced artifacts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3]
    attempt_id: int = Field(ge=0)
    method_id: str = Field(min_length=1)
    status: ParameterAttemptStatus
    task: ParameterAttemptTask
    previous_attempt_id: int | None = Field(default=None, ge=0)
    recommendation_manifest_ref: ArtifactRef
    approval_decision_ref: ArtifactRef
    frozen_parameters_ref: ArtifactRef | None = None
    command_ref: ArtifactRef | None = None
    frozen_attempt_ref: ArtifactRef | None = None
    execution_ref: ArtifactRef | None = None
    review_ref: ArtifactRef | None = None
    attempt_manifest_ref: ArtifactRef | None = None
    recommendation_contract: Literal["parameter_recommendation_v1",] = (
        "parameter_recommendation_v1"
    )
    previous_attempt_seal_hash: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    sealed_at: str | None = None
    seal_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_attempt_kind_for_task(self) -> "ParameterAttempt":
        """Keep the immutable attempt chain consistent with its task."""

        if self.task == "recommend_initial":
            if self.attempt_id != 0 or self.previous_attempt_id is not None:
                raise ValueError(
                    "initial parameter attempt must be attempt 0 without a predecessor"
                )
            if self.previous_attempt_seal_hash is not None:
                raise ValueError(
                    "initial parameter attempt cannot bind a previous seal"
                )
        else:
            if self.attempt_id == 0 or self.previous_attempt_id != self.attempt_id - 1:
                raise ValueError(
                    "retry parameter attempt must bind the immediately "
                    "preceding attempt"
                )
            if self.previous_attempt_seal_hash is None:
                raise ValueError(
                    "retry parameter attempt requires previous_attempt_seal_hash"
                )
        if self.status == "recommended" and self.frozen_attempt_ref is not None:
            raise ValueError(
                "a recommended parameter attempt cannot own frozen runner input"
            )
        if self.status != "recommended" and self.frozen_attempt_ref is None:
            raise ValueError(
                "a frozen or completed parameter attempt requires frozen_attempt_ref"
            )
        if (
            self.frozen_attempt_ref is not None
            and not self.frozen_attempt_ref.artifact_type.endswith(".frozen_attempt")
        ):
            raise ValueError(
                "frozen_attempt_ref must reference frozen-attempt evidence"
            )
        return self


class GenomeSurveyResult(BaseModel):
    """Compact genome survey contract written by the genome_survey stage."""

    session_id: str
    mode: Literal["full", "skipped"]
    status: Literal["planned", "completed", "skipped"]
    confidence: Literal["high", "low", "none"]
    input_asset_refs: list[str] = Field(default_factory=list)
    candidate_methods: list[str] = Field(default_factory=list)
    estimates: dict[str, Any] = Field(default_factory=dict)
    measured_estimates: dict[str, Any] = Field(default_factory=dict)
    provided_overrides: dict[str, Any] = Field(default_factory=dict)
    estimate_sources: dict[str, Literal["provided", "survey"]] = Field(
        default_factory=dict
    )
    estimate_source_paths: dict[str, str] = Field(default_factory=dict)
    genome_size_prior: int | None = Field(default=None, gt=0)
    genome_size_source: Literal["provided", "survey"] | None = None
    genome_size_source_path: str | None = None
    skip_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyNativeOutput(BaseModel):
    """One method-native Assembly deliverable retained beside the backbone."""

    model_config = ConfigDict(extra="forbid")

    artifact_class: Literal[
        "assembly_graph",
        "alternate_contigs",
        "haplotype_contigs",
        "report",
        "config",
        "log",
        "other",
    ]
    native_role: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    artifact_ref: ArtifactRef


class AssemblyCandidateBundle(BaseModel):
    """Status-aware Assembly product manifest consumed by assembly_review."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    method_name: str = Field(min_length=1)
    status: Literal["planned", "executed"]
    artifact_type: Literal["draft_assembly.candidate"] = "draft_assembly.candidate"
    input_type: str = Field(min_length=1)
    input_member_ids: dict[str, list[str]]
    input_member_platforms: dict[str, list[str]]
    parameter_attempt_id: int | None = Field(default=None, ge=0)
    genome_size_prior: int | None = Field(default=None, gt=0)
    expected_outputs: list[str] = Field(min_length=1)
    log_refs: dict[str, ArtifactRef] = Field(default_factory=dict)
    plan_ref: ArtifactRef | None = None
    primary_output: ArtifactRef | None = None
    native_outputs: list[AssemblyNativeOutput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_status_contract(self) -> "AssemblyCandidateBundle":
        if set(self.input_member_ids) != set(self.input_member_platforms):
            raise ValueError("candidate input member ID and platform roles must match")
        if not self.input_member_ids.get("primary"):
            raise ValueError("candidate bundle requires primary input member IDs")
        for role, member_ids in self.input_member_ids.items():
            if not role:
                raise ValueError("candidate input role cannot be empty")
            platforms = self.input_member_platforms[role]
            if len(member_ids) != len(platforms):
                raise ValueError(
                    f"candidate input role {role!r} has mismatched member/platform "
                    "cardinality"
                )
            if any(not member_id for member_id in member_ids):
                raise ValueError(
                    f"candidate input role {role!r} contains an empty member ID"
                )
            if any(not platform for platform in platforms):
                raise ValueError(
                    f"candidate input role {role!r} contains an empty platform"
                )
            if len(member_ids) != len(set(member_ids)):
                raise ValueError(
                    f"candidate input role {role!r} contains duplicate member IDs"
                )
        if any(not role for role in self.expected_outputs):
            raise ValueError("candidate expected output role cannot be empty")
        if len(self.expected_outputs) != len(set(self.expected_outputs)):
            raise ValueError("duplicate candidate expected output role")
        if "primary_assembly" not in self.expected_outputs:
            raise ValueError(
                "candidate bundle must declare the primary_assembly output role"
            )
        if self.log_refs:
            expected_log_types = {
                "stdout": "command.stdout",
                "stderr": "command.stderr",
            }
            if set(self.log_refs) != set(expected_log_types):
                raise ValueError(
                    "candidate logs must be the canonical stdout/stderr pair"
                )
            for role, expected_type in expected_log_types.items():
                if self.log_refs[role].artifact_type != expected_type:
                    raise ValueError(
                        f"candidate {role} has artifact type "
                        f"{self.log_refs[role].artifact_type!r}, expected "
                        f"{expected_type!r}"
                    )
        if self.status == "planned":
            if (
                self.plan_ref is not None
                or self.primary_output is not None
                or self.native_outputs
                or self.log_refs
            ):
                raise ValueError(
                    "planned candidate bundle cannot contain materialized output refs"
                )
            if self.parameter_attempt_id is None:
                raise ValueError(
                    "planned candidate bundle requires parameter_attempt_id"
                )
        elif self.status == "executed":
            if self.plan_ref is None or self.primary_output is None:
                raise ValueError(
                    "executed candidate bundle requires plan_ref and primary_output"
                )
            if self.parameter_attempt_id is None:
                raise ValueError(
                    "executed candidate bundle requires parameter_attempt_id"
                )
        native_roles = [output.native_role for output in self.native_outputs]
        if len(native_roles) != len(set(native_roles)):
            raise ValueError("duplicate native output role")
        return self


class AssemblyEvalSignal(BaseModel):
    """One evaluation signal for an assembly candidate.

    Thresholds are not stored here; they are human-approved policy
    applied during rule evaluation.
    """

    signal_name: str
    method_name: str
    candidate_id: str
    status: Literal["pending", "available", "missing", "failed"]
    metric_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyEvalBundle(BaseModel):
    """Collection of evaluation signals for one or more assembly candidates.

    Does not contain thresholds or decisions; only what was collected
    or referenced.
    """

    session_id: str
    candidate_ids: list[str] = Field(default_factory=list)
    signals: list[AssemblyEvalSignal] = Field(default_factory=list)
    available_inputs: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewDecisionArtifact(BaseModel):
    """Human or deterministic review decision recorded as an artifact."""

    session_id: str
    stage: str
    decision: Literal[
        "accept",
        "pass",
        "rerun",
        "purge",
        "polish",
        "continue_low_confidence",
        "stop_human_review",
    ]
    candidate_id: str | None = None
    rejected_candidates: list[str] = Field(default_factory=list)
    final_assembly_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyReviewActionArtifact(BaseModel):
    """Post-review assembly action provenance."""

    session_id: str
    stage: Literal["assembly_review"] = "assembly_review"
    decision: Literal["accept", "pass", "purge", "polish", "continue_low_confidence"]
    action_type: Literal["pass", "pass_low_confidence", "purge", "polish"]
    candidate_id: str
    pre_action_assembly_ref: str
    post_action_assembly_ref: str
    output_assembly_ref: str
    action_execution_mode: Literal["none", "external"]
    method_name: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HiCScaffoldDecisionArtifact(BaseModel):
    """Human adoption decision for the completed AutoHiC result."""

    session_id: str
    stage: Literal["hic_scaffolding"] = "hic_scaffolding"
    decision: Literal[
        "accept_candidate",
        "keep_prior_assembly",
        "request_parameter_retry",
        "stop_human_review",
    ]
    method_name: str | None = None
    pre_hic_assembly_ref: str
    output_assembly_ref: str
    hic_read_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class T2TReviewDecisionArtifact(BaseModel):
    """Human T2T finishing decision recorded as an artifact."""

    session_id: str
    stage: Literal["t2t"] = "t2t"
    decision: Literal[
        "accept_selected_candidates", "continue_without_patch", "stop_pipeline"
    ]
    candidate_ids: list[str] = Field(default_factory=list)
    input_assembly_ref: str
    output_assembly_ref: str
    patched_assembly_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class T2TFinishingBundle(BaseModel):
    """Compact provenance for a T2T finishing run."""

    session_id: str
    evidence_phase: Literal["pre_review", "final"]
    status: str
    input_assembly_ref: str
    output_assembly_ref: str
    long_read_refs: list[str] = Field(default_factory=list)
    mode: Literal["propose_only", "manual_review", "auto_patch"]
    resolved_telomere_motif: str | None = None
    resolved_telomere_motif_source: str | None = None
    patched_assembly_ref: str | None = None
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


ASSEMBLY_INPUT_PROFILE = "assembly.input_profile"
ASSEMBLY_ROUTE_DECISION = "assembly.route_decision"
ASSEMBLY_METHOD_MODEL_REQUEST = "assembly.method.model_request"
ASSEMBLY_METHOD_MODEL_RESPONSE = "assembly.method.model_response"
ASSEMBLY_METHOD_RECOMMENDATION = "assembly.method.recommendation"


class AssemblyInputProfile(BaseModel):
    """InputProfiler output: data profile plus route recommendation."""

    session_id: str
    primary_long_reads_ref: str | list[str] | None = None
    primary_long_read_platform: str | None = None
    ont_reads_ref: str | None = None
    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    estimated_coverage: float | None = None
    estimated_genome_size: int | None = None
    target_level: Literal["draft", "near_t2t", "strict_t2t"]
    has_hifi: bool = False
    has_hic: bool = False
    has_rnaseq: bool = False
    recommended_route: Literal["cyclone", "ont", "hifi", "insufficient_data"]
    recommendation_reasoning: str
    uncertainty_notes: list[str] = Field(default_factory=list)
    profile_source: Literal[
        "manifest",
        "run_config",
        "run_config_explicit",
        "llm_inferred",
        "human_confirmed",
        "policy_timeout",
    ]
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyRouteDecision(BaseModel):
    """Final route decision confirmed by run config, policy, or human review."""

    session_id: str
    selected_route: Literal["cyclone", "ont", "hifi"]
    decided_by: Literal["auto_rule", "human_override", "run_config_explicit"]
    rationale: str
    metadata: dict[str, Any] = Field(default_factory=dict)
