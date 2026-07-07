"""Artifact metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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
ASSEMBLY_PARAMETER_PROMPT_PAYLOAD = "assembly.parameter.prompt_payload"
ASSEMBLY_PARAMETER_LLM_RESPONSE = "assembly.parameter.llm_response"
ASSEMBLY_PARAMETER_VALIDATION = "assembly.parameter.validation"
ASSEMBLY_PARAMETER_COMMAND = "assembly.parameter.command"
ASSEMBLY_PARAMETER_REVIEW_FAILURE = "assembly.parameter.review_failure"
ASSEMBLY_REVIEW_EVAL_BUNDLE = "assembly_review.eval_bundle"
ASSEMBLY_REVIEW_QUAST_REPORT_TSV = "assembly_review.quast_report_tsv"
ASSEMBLY_REVIEW_QUAST_REPORT_TXT = "assembly_review.quast_report_txt"
ASSEMBLY_REVIEW_QUAST_SUMMARY = "assembly_review.quast_summary"
ASSEMBLY_REVIEW_BUSCO_SHORT_SUMMARY = "assembly_review.busco_short_summary"
ASSEMBLY_REVIEW_BUSCO_SUMMARY = "assembly_review.busco_summary"
ASSEMBLY_REVIEW_ACTION = "assembly_review.action"
ASSEMBLY_REVIEW_REPORT = "assembly_review.report"
ASSEMBLY_REVIEW_REPORT_DATA = "assembly_review.report_data"
ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK = "assembly_review.related_species_evidence_pack"
ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK = "assembly_review.related_species_statistics_pack"
ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK = "assembly_review.comparative_notebook"
ASSEMBLY_CANDIDATE_COMPARISON = "assembly_review.candidate_comparison"
ASSEMBLY_CANDIDATE_COMPARISON_REPORT = "assembly_review.candidate_comparison_report"
HIC_SCAFFOLD_PLAN = "hic_scaffolding.plan"
HIC_SCAFFOLDED_ASSEMBLY = "hic_scaffolding.chromosome_scaffolded_assembly"
HIC_SCAFFOLD_AGP = "hic_scaffolding.scaffold_agp"
HIC_REVIEW_BUNDLE = "hic_scaffolding.review_bundle"
HIC_QC_REPORT = "hic_scaffolding.qc_report"
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


def file_sha256(path: Path) -> str:
    """Return a file SHA256 digest."""
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            file_hash=file_sha256(path),
            file_size_bytes=path.stat().st_size,
            artifact_type=artifact_type,
            stage=stage,
            method_name=method_name,
            metadata=metadata or {},
        )


class ArtifactRef(BaseModel):
    """Path ref plus integrity hash for a runtime artifact."""

    path_ref: str
    sha256: str
    artifact_type: str


class ParameterAttempt(BaseModel):
    """Compact replay record for one assembly parameter recommendation attempt."""

    attempt_id: int
    method_id: str
    status: str
    input_profile_ref: ArtifactRef
    prompt_payload_ref: ArtifactRef
    llm_response_ref: ArtifactRef | None = None
    validation_ref: ArtifactRef | None = None
    command_ref: ArtifactRef | None = None
    review_failure_ref: ArtifactRef | None = None
    catalog_ref: str | None = None
    knowledge_pack_ref: str | None = None
    selected_case_ids: list[str] = Field(default_factory=list)
    recommended_params: dict[str, Any] = Field(default_factory=dict)
    omitted_parameters: list[str] = Field(default_factory=list)
    parameter_sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    argv: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    reasoning: str | None = None
    confidence: float | None = None
    previous_attempt_id: int | None = None
    execution_result: dict[str, Any] | None = None
    failure_log_tail: str | None = None
    review_failure: dict[str, Any] | None = None


class AssemblyParameterLoopState(BaseModel):
    """Typed compact state for Phase10 parameter-loop control."""

    current_parameter_attempt_id: int = 0
    parameter_attempts: list[ParameterAttempt] = Field(default_factory=list)
    retry_counts: dict[str, int] = Field(default_factory=dict)


class GenomeSurveyResult(BaseModel):
    """Compact genome survey contract written by the genome_survey stage."""

    session_id: str
    mode: Literal["full", "skipped"]
    status: Literal["planned", "completed", "skipped"]
    confidence: Literal["high", "low", "none"]
    input_asset_refs: list[str] = Field(default_factory=list)
    candidate_methods: list[str] = Field(default_factory=list)
    estimates: dict[str, Any] = Field(default_factory=dict)
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
    input_ref: str
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
    final_assembly_for_scaffolding_or_annotation: str
    action_execution_mode: Literal["none", "external"]
    method_name: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HiCSkipRecord(BaseModel):
    """Explicit non-blocking Hi-C skip record."""

    session_id: str
    mode: Literal["skipped_no_hic", "skipped_not_chromosome_level", "human_review_skip"]
    skip_reason: Literal["no_hic_reads", "not_chromosome_level_requested", "human_review_skip"]
    pre_hic_assembly_ref: str
    final_assembly_for_annotation: str
    hic_read_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HiCScaffoldDecisionArtifact(BaseModel):
    """Human Hi-C scaffolding decision recorded without running tools."""

    session_id: str
    stage: Literal["hic_scaffolding"] = "hic_scaffolding"
    decision: Literal["select_method", "accept_candidate", "skip", "stop_human_review"]
    method_name: str | None = None
    scaffold_plan_ref: str | None = None
    pre_hic_assembly_ref: str
    final_assembly_for_annotation: str
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
    final_assembly_for_annotation: str
    patched_assembly_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class T2TFinishingBundle(BaseModel):
    """Compact provenance for a T2T finishing run and its annotation handoff."""

    session_id: str
    status: str
    input_assembly_ref: str
    final_assembly_for_annotation: str
    ont_reads_ref: str | None = None
    mode: Literal["propose_only", "manual_review", "auto_patch"] = "auto_patch"
    patched_assembly_ref: str | None = None
    artifact_refs: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HiCScaffoldPlan(BaseModel):
    """Planned Hi-C scaffolding bundle without running tools."""

    session_id: str
    status: Literal["planned"]
    pre_hic_assembly_ref: str
    fallback_assembly_ref: str
    hic_read_refs: list[str] = Field(default_factory=list)
    hic_alignment_refs: list[str] = Field(default_factory=list)
    candidate_methods: list[str] = Field(default_factory=list)
    expected_outputs: dict[str, str] = Field(default_factory=dict)
    review_required: bool = True
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepeatAnnotationPlan(BaseModel):
    """Planned repeat annotation contract without running repeat tools."""

    session_id: str
    status: Literal["planned"]
    strategy: Literal["provided", "de_novo"]
    repeat_library_source: Literal["provided", "de_novo"]
    final_assembly_for_annotation: str
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
    final_assembly_for_annotation: str
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
    genome_input_source: Literal["softmasked_genome", "final_assembly_for_annotation"]
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
    genome_input_source: Literal["softmasked_genome", "final_assembly_for_annotation"]
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
    execution_result_ref: str | None = None
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
ASSEMBLY_QC_SUMMARY = "assembly.qc_summary"
ASSEMBLY_T2T_READINESS = "assembly.t2t_readiness"
ASSEMBLY_REVIEW_TASKS = "assembly.review_tasks"


class AssemblyInputProfile(BaseModel):
    """InputProfiler output: data profile plus route recommendation."""

    session_id: str
    primary_long_reads_ref: str | None = None
    primary_long_read_platform: str | None = None
    ont_reads_ref: str | None = None
    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    estimated_coverage: float | None = None
    estimated_genome_size: int | None = None
    target_level: Literal["draft", "near_t2t", "strict_t2t"]
    has_hifi: bool = False
    has_hic: bool = False
    recommended_route: Literal["cyclone", "ont", "hifi", "insufficient_data"]
    recommendation_confidence: float
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
