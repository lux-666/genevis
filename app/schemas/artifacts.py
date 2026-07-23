"""Artifact metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.integrity import sha256_file

READ_QC_LONG_READ_STATS = "read_qc.long_read_stats"
READ_QC_LONG_READ_SUMMARY = "read_qc.long_read_summary"
READ_QC_FILTERED_LONG_READS = "read_qc.filtered_long_reads"
READ_QC_LONG_READS_FOFN = "read_qc.long_reads.fofn"
READ_QC_FILTERED_LONG_READS_FOFN = "read_qc.filtered_long_reads.fofn"
GENOME_SURVEY_KMER_DB = "genome_survey.kmer_db"
GENOME_SURVEY_KMER_HIST = "genome_survey.kmer_hist"
GENOME_SURVEY_MODEL_SUMMARY = "genome_survey.model_summary"
ASSEMBLY_DRAFT_ASSEMBLY = "assembly.draft_assembly"
ASSEMBLY_CANDIDATE_BUNDLE = "draft_assembly.candidate"
ASSEMBLY_PARAMETER_INPUT_PROFILE = "assembly.parameter.input_profile"
ASSEMBLY_PARAMETER_MODEL_REQUEST = "assembly.parameter.model_request"
ASSEMBLY_PARAMETER_MODEL_RESPONSE = "assembly.parameter.model_response"
ASSEMBLY_PARAMETER_BASIS = "assembly.parameter.basis_snapshot"
ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST = "assembly.parameter.recommendation_manifest"
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
ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK = "assembly_review.related_species_evidence_pack"
ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK = "assembly_review.related_species_statistics_pack"
ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK = "assembly_review.comparative_notebook"
ASSEMBLY_CANDIDATE_COMPARISON = "assembly_review.candidate_comparison"
HIC_SCAFFOLDED_ASSEMBLY = "hic_scaffolding.chromosome_scaffolded_assembly"
HIC_REVIEW_BUNDLE = "hic_scaffolding.review_bundle"
HIC_QC_REPORT = "hic_scaffolding.qc_report"
HIC_CONTACT_MAP = "hic_scaffolding.contact_map"
HIC_ASSEMBLY_FILE = "hic_scaffolding.assembly"
HIC_HTML_REPORT = "hic_scaffolding.html_report"
HIC_PARAMETER_INPUT_PROFILE = "hic_scaffolding.parameter.input_profile"
HIC_PARAMETER_BASIS = "hic_scaffolding.parameter.basis_snapshot"
HIC_PARAMETER_MODEL_REQUEST = "hic_scaffolding.parameter.model_request"
HIC_PARAMETER_MODEL_RESPONSE = "hic_scaffolding.parameter.model_response"
HIC_PARAMETER_RECOMMENDATION = "hic_scaffolding.parameter.recommendation"
HIC_PARAMETER_RESPONSE_PARSE = "hic_scaffolding.parameter.response_parse"
HIC_PARAMETER_RECOMMENDATION_MANIFEST = "hic_scaffolding.parameter.recommendation_manifest"
HIC_PARAMETER_APPROVAL_DECISION = "hic_scaffolding.parameter_approval.decision"
HIC_PARAMETER_FROZEN_PARAMETERS = "hic_scaffolding.parameter.frozen_parameters"
HIC_PARAMETER_ATTEMPT_MANIFEST = "hic_scaffolding.parameter.attempt_manifest"
HIC_PARAMETER_REVIEW = "hic_scaffolding.parameter.review"
T2T_PARAMETER_INPUT_PROFILE = "t2t.parameter.input_profile"
T2T_PARAMETER_BASIS = "t2t.parameter.basis_snapshot"
T2T_PARAMETER_MODEL_REQUEST = "t2t.parameter.model_request"
T2T_PARAMETER_MODEL_RESPONSE = "t2t.parameter.model_response"
T2T_PARAMETER_RECOMMENDATION = "t2t.parameter.recommendation"
T2T_PARAMETER_RESPONSE_PARSE = "t2t.parameter.response_parse"
T2T_PARAMETER_RECOMMENDATION_MANIFEST = "t2t.parameter.recommendation_manifest"
T2T_PARAMETER_APPROVAL_DECISION = "t2t.parameter_approval.decision"
T2T_PARAMETER_FROZEN_PARAMETERS = "t2t.parameter.frozen_parameters"
T2T_MOTIF_INPUT_PROFILE = "t2t.telomere_motif.input_profile"
T2T_MOTIF_MODEL_REQUEST = "t2t.telomere_motif.model_request"
T2T_MOTIF_MODEL_RESPONSE = "t2t.telomere_motif.model_response"
T2T_MOTIF_RESOLUTION = "t2t.telomere_motif.resolution"
T2T_MOTIF_RESPONSE_PARSE = "t2t.telomere_motif.response_parse"
T2T_MOTIF_RESOLUTION_MANIFEST = "t2t.telomere_motif.resolution_manifest"
T2T_READ_ALIGNMENT_PAF = "t2t.read_alignment_paf"
T2T_ASSEMBLY_TELOMERE_SCAN = "t2t.assembly_telomere_scan"
T2T_READS_TELOMERE_SCAN = "t2t.reads_telomere_scan"
T2T_TELOMERIC_READS = "t2t.telomeric_reads"
T2T_EXTENSION_CANDIDATES = "t2t.extension_candidates"
T2T_STATUS_SUMMARY = "t2t.status_summary"
T2T_FINISHING_REPORT = "t2t.finishing_report"
T2T_PATCHED_ASSEMBLY = "t2t.patched_assembly"
T2T_PATCH_RECORDS = "t2t.patch_records"
REPEAT_ANNOTATION_PLAN = "repeat_annotation.plan"
REPEAT_ANNOTATION_BUNDLE = "repeat_annotation.bundle"
REPEAT_ANNOTATION_GFF3 = "repeat_annotation.gff3"
REPEAT_SOFTMASKED_GENOME = "repeat_annotation.softmasked_genome"
REPEAT_HARDMASKED_GENOME = "repeat_annotation.hardmasked_genome"

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
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
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

    schema_version: Literal[2] = 2
    recommendation_id: int = Field(ge=0)
    method_id: str = Field(min_length=1)
    task: ParameterAttemptTask
    status: ParameterRecommendationStatus
    previous_attempt_id: int | None = Field(default=None, ge=0)
    manifest_ref: ArtifactRef
    recommendation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_request_id: str | None = None
    review_request_ref: ArtifactRef | None = None
    previous_attempt_seal_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
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

    schema_version: Literal[2] = 2
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
    recommendation_contract: Literal[
        "parameter_recommendation_v1",
    ] = "parameter_recommendation_v1"
    previous_attempt_seal_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    sealed_at: str | None = None
    seal_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_attempt_kind_for_task(self) -> "ParameterAttempt":
        """Keep the immutable attempt chain consistent with its task."""

        if self.task == "recommend_initial":
            if self.attempt_id != 0 or self.previous_attempt_id is not None:
                raise ValueError("initial parameter attempt must be attempt 0 without a predecessor")
            if self.previous_attempt_seal_hash is not None:
                raise ValueError("initial parameter attempt cannot bind a previous seal")
        else:
            if self.attempt_id == 0 or self.previous_attempt_id != self.attempt_id - 1:
                raise ValueError("retry parameter attempt must bind the immediately preceding attempt")
            if self.previous_attempt_seal_hash is None:
                raise ValueError("retry parameter attempt requires previous_attempt_seal_hash")
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
            raise ValueError("frozen_attempt_ref must reference frozen-attempt evidence")
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
    estimate_sources: dict[str, Literal["provided", "survey"]] = Field(default_factory=dict)
    estimate_source_paths: dict[str, str] = Field(default_factory=dict)
    genome_size_prior: int | None = Field(default=None, gt=0)
    genome_size_source: Literal["provided", "survey"] | None = None
    genome_size_source_path: str | None = None
    skip_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyCandidateBundle(BaseModel):
    """Assembly candidate bundle consumed by assembly_review."""

    session_id: str
    candidate_id: str
    method_name: str
    status: Literal["planned", "executed", "failed", "rejected"]
    artifact_type: str = ASSEMBLY_CANDIDATE_BUNDLE
    input_ref: str | list[str]
    input_type: str
    genome_size_prior: int | None = None
    expected_outputs: dict[str, str] = Field(default_factory=dict)
    logs: dict[str, str] = Field(default_factory=dict)
    rejection_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    decision: Literal["accept_selected_candidates", "continue_without_patch", "stop_pipeline"]
    candidate_ids: list[str] = Field(default_factory=list)
    input_assembly_ref: str
    output_assembly_ref: str
    patched_assembly_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class T2TFinishingBundle(BaseModel):
    """Compact provenance for a T2T finishing run and its annotation handoff."""

    session_id: str
    status: str
    input_assembly_ref: str
    output_assembly_ref: str
    ont_read_refs: list[str] = Field(default_factory=list)
    mode: Literal["propose_only", "manual_review", "auto_patch"] = "auto_patch"
    resolved_telomere_motif: str | None = None
    resolved_telomere_motif_source: str | None = None
    patched_assembly_ref: str | None = None
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepeatAnnotationPlan(BaseModel):
    """Planned repeat annotation contract without running repeat tools."""

    session_id: str
    status: Literal["planned"]
    strategy: Literal["provided", "de_novo"]
    repeat_library_source: Literal["provided", "de_novo"]
    input_assembly_ref: str
    provided_repeat_library_ref: str | None = None
    library_candidate_methods: list[str] = Field(default_factory=list)
    helper_methods: list[str] = Field(default_factory=list)
    masking_method: str
    expected_outputs: dict[str, str] = Field(default_factory=dict)
    confidence: Literal["high", "low"] = "high"
    low_confidence_reasons: list[str] = Field(default_factory=list)
    method_selection_required: bool = False
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepeatAnnotationBundle(BaseModel):
    """Repeat annotation bundle consumed by downstream gene annotation."""

    session_id: str
    status: Literal["planned", "executed"]
    strategy: Literal["provided", "de_novo"]
    repeat_library_source: Literal["provided", "de_novo"]
    input_assembly_ref: str
    repeat_library_ref: str
    repeat_annotation_gff3_ref: str
    softmasked_genome_ref: str
    hardmasked_genome_ref: str | None = None
    confidence: Literal["high", "low"] = "high"
    low_confidence_reasons: list[str] = Field(default_factory=list)
    candidate_methods: list[str] = Field(default_factory=list)
    plan_artifact_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GeneAnnotationPlan(BaseModel):
    """Planned gene annotation contract without running annotation tools."""

    session_id: str
    status: Literal["planned"]
    genome_input_ref: str
    genome_input_source: Literal["softmasked_genome", "assembly"]
    unmasked_fallback_reason: str | None = None
    repeat_annotation_ref: str | None = None
    rnaseq_read_refs: list[str] = Field(default_factory=list)
    long_rna_read_refs: list[str] = Field(default_factory=list)
    protein_evidence_refs: list[str] = Field(default_factory=list)
    lineage_or_species_hints: dict[str, Any] = Field(default_factory=dict)
    evidence_profile: Literal["no_evidence", "rnaseq", "long_rna", "protein", "mixed_evidence"]
    candidate_methods: list[str] = Field(default_factory=list)
    helper_methods: list[str] = Field(default_factory=list)
    expected_outputs: dict[str, str] = Field(default_factory=dict)
    review_required: bool = False
    review_triggers: list[str] = Field(default_factory=list)
    method_selection_required: bool = True
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GeneAnnotationBundle(BaseModel):
    """Gene annotation bundle consumed by downstream function annotation."""

    session_id: str
    status: Literal["planned", "adopted_existing_annotation"]
    genome_input_ref: str
    genome_input_source: Literal["softmasked_genome", "assembly"]
    genes_gff3_ref: str
    transcripts_fasta_ref: str | None = None
    predicted_proteins_ref: str
    evidence_report_ref: str | None = None
    repeat_annotation_ref: str | None = None
    evidence_profile: Literal["no_evidence", "rnaseq", "long_rna", "protein", "mixed_evidence", "provided_annotation"]
    candidate_methods: list[str] = Field(default_factory=list)
    helper_methods: list[str] = Field(default_factory=list)
    review_required: bool = False
    review_triggers: list[str] = Field(default_factory=list)
    plan_artifact_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateBundle(BaseModel):
    """Standardized gene-annotation candidate: a structural GFF plus a real,
    on-disk proteins FASTA. A candidate is valid only when annotation_gff_ref
    and proteins_fasta_ref point at files that actually exist; methods without
    native protein output materialize proteins.fa from the GFF + genome before
    the bundle is recorded.
    """

    method_name: str
    category: str
    annotation_gff_ref: str
    proteins_fasta_ref: str
    cds_fasta_ref: str | None = None
    metrics_ref: str | None = None
    provenance_ref: str | None = None
    evm_evidence_class: str = "ABINITIO_PREDICTION"
    evm_source_type: str = ""
    status: Literal["materialized", "materialization_failed"] = "materialized"


FunctionAnnotationProvenanceMethod = Literal["function_annotation.candidate.diamond"]


class FunctionDatabaseProvenance(BaseModel):
    """Database provenance required for planned function annotation."""

    method_name: FunctionAnnotationProvenanceMethod
    database_name: str
    database_version: str
    snapshot_date: str
    database_source_ref: str
    # Optional: when omitted, function_annotation adopts the resolved
    # proteins_fasta_ref at runtime. The ask layer cannot know the runtime
    # path for manifest-provided proteins, so it leaves this unset.
    input_proteins_ref: str | None = None
    command: str | None = None
    descriptor_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "method_name",
        "database_name",
        "database_version",
        "snapshot_date",
        "database_source_ref",
    )
    @classmethod
    def _required_text_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _require_command_or_descriptor_version(self):
        if not (self.command and self.command.strip()) and not (
            self.descriptor_version and self.descriptor_version.strip()
        ):
            raise ValueError("function database provenance requires command or descriptor_version")
        return self


class FunctionMethodResult(BaseModel):
    """Materialized result from one function annotation method."""

    method_name: str
    status: Literal["materialized", "skipped", "failed"] = "materialized"
    result_refs: dict[str, str] = Field(default_factory=dict)
    database_provenance: list[FunctionDatabaseProvenance] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FunctionAnnotationPlan(BaseModel):
    """Planned function annotation contract without running tools."""

    session_id: str
    status: Literal["planned"]
    proteins_fasta_ref: str
    genes_gff3_ref: str | None = None
    transcripts_fasta_ref: str | None = None
    candidate_methods: list[str] = Field(default_factory=list)
    expected_outputs: dict[str, str] = Field(default_factory=dict)
    database_provenance: list[FunctionDatabaseProvenance] = Field(default_factory=list)
    method_selection_required: bool = True
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FunctionAnnotationBundle(BaseModel):
    """Function annotation bundle for final workflow outputs."""

    session_id: str
    status: Literal["planned"]
    proteins_fasta_ref: str
    genes_gff3_ref: str | None = None
    transcripts_fasta_ref: str | None = None
    diamond_hits_ref: str | None = None
    gene_function_tsv_ref: str | None = None
    go_annotation_table_ref: str | None = None
    kegg_annotation_table_ref: str | None = None
    pfam_interpro_table_ref: str | None = None
    annotated_gene_models_gff3_ref: str | None = None
    method_results: list[FunctionMethodResult] = Field(default_factory=list)
    candidate_methods: list[str] = Field(default_factory=list)
    database_provenance: list[FunctionDatabaseProvenance] = Field(default_factory=list)
    plan_artifact_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


ASSEMBLY_INPUT_PROFILE = "assembly.input_profile"
ASSEMBLY_ROUTE_DECISION = "assembly.route_decision"


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
    profile_source: Literal["manifest", "run_config", "run_config_explicit", "llm_inferred"]
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyRouteDecision(BaseModel):
    """Final route decision confirmed by run config, policy, or human review."""

    session_id: str
    selected_route: Literal["cyclone", "ont", "hifi"]
    decided_by: Literal["llm_auto", "human_override", "run_config_explicit"]
    rationale: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyQCSummary(BaseModel):
    """QC summary for one assembly candidate."""

    session_id: str
    candidate_id: str
    method_name: str
    n50_mb: float | None = None
    contig_count: int | None = None
    total_length_mb: float | None = None
    busco_completeness: float | None = None
    busco_lineage: str | None = None
    qv_score: float | None = None
    gap_count: int | None = None
    telomere_detected: bool | None = None
    qc_passed: bool
    qc_notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyT2TReadiness(BaseModel):
    """T2TFinishingAgent readiness assessment."""

    session_id: str
    candidate_id: str
    t2t_verdict: Literal["adopt_draft", "near_t2t", "strict_t2t_candidate", "insufficient", "request_data"]
    llm_confidence: float
    llm_reasoning: str
    uncertainty_notes: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    telomere_coverage_pct: float | None = None
    gap_count: int | None = None
    total_gap_bases: int | None = None
    hic_misassembly_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyReviewTaskItem(BaseModel):
    """One human-review task generated during T2T readiness assessment."""

    priority: Literal["high", "medium", "low"]
    category: str
    finding: str
    action: str
    can_auto_resolve: bool = False


class AssemblyReviewTasks(BaseModel):
    """T2TFinishingAgent human-review task list."""

    session_id: str
    candidate_id: str
    t2t_status_estimate: Literal["near_t2t", "strict_t2t_candidate", "insufficient"]
    confidence: float
    tasks: list[AssemblyReviewTaskItem] = Field(default_factory=list)
    blocking_for_strict_t2t: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
