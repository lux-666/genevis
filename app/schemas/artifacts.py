"""Artifact metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

READ_QC_LONG_READ_STATS = "read_qc.long_read_stats"
READ_QC_FILTERED_LONG_READS = "read_qc.filtered_long_reads"
READ_QC_LONG_READS_FOFN = "read_qc.long_reads.fofn"
READ_QC_FILTERED_LONG_READS_FOFN = "read_qc.filtered_long_reads.fofn"


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


class GenomeSurveyResult(BaseModel):
    """Compact genome survey contract written by the genome_survey stage."""

    session_id: str
    mode: Literal["full", "fallback_low_confidence", "skipped"]
    status: Literal["planned", "skipped"]
    confidence: Literal["high", "low", "none"]
    input_asset_refs: list[str] = Field(default_factory=list)
    candidate_methods: list[str] = Field(default_factory=list)
    estimates: dict[str, Any] = Field(default_factory=dict)
    skip_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyCandidateBundle(BaseModel):
    """Planned assembly candidate bundle without running an assembler."""

    session_id: str
    candidate_id: str
    method_name: str
    status: Literal["planned", "rejected"]
    artifact_type: str = "draft_assembly.candidate"
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
        "rerun",
        "purge",
        "continue_low_confidence",
        "stop_human_review",
    ]
    candidate_id: str | None = None
    rejected_candidates: list[str] = Field(default_factory=list)
    final_assembly_ref: str | None = None
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
    decision: Literal["select_method", "skip", "stop_human_review"]
    method_name: str | None = None
    scaffold_plan_ref: str | None = None
    pre_hic_assembly_ref: str
    final_assembly_for_annotation: str
    hic_read_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HiCScaffoldPlan(BaseModel):
    """Planned Hi-C scaffolding bundle without running tools."""

    session_id: str
    status: Literal["planned"]
    pre_hic_assembly_ref: str
    fallback_assembly_ref: str
    hic_read_refs: list[str] = Field(default_factory=list)
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
    status: Literal["planned"]
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
    status: Literal["planned"]
    genome_input_ref: str
    genome_input_source: Literal["softmasked_genome", "final_assembly_for_annotation"]
    genes_gff3_ref: str
    transcripts_fasta_ref: str
    predicted_proteins_ref: str
    evidence_report_ref: str
    repeat_annotation_ref: str | None = None
    evidence_profile: Literal["no_evidence", "rnaseq", "long_rna", "protein", "mixed_evidence"]
    candidate_methods: list[str] = Field(default_factory=list)
    helper_methods: list[str] = Field(default_factory=list)
    review_required: bool = False
    review_triggers: list[str] = Field(default_factory=list)
    plan_artifact_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FunctionDatabaseProvenance(BaseModel):
    """Database provenance required for planned function annotation."""

    method_name: str
    database_name: str
    database_version: str
    snapshot_date: str
    input_proteins_ref: str
    command: str | None = None
    descriptor_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_command_or_descriptor_version(self):
        if not self.command and not self.descriptor_version:
            raise ValueError("function database provenance requires command or descriptor_version")
        return self


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
    gene_function_tsv_ref: str
    go_annotation_table_ref: str
    kegg_annotation_table_ref: str
    pfam_interpro_table_ref: str
    annotated_gene_models_gff3_ref: str | None = None
    candidate_methods: list[str] = Field(default_factory=list)
    database_provenance: list[FunctionDatabaseProvenance] = Field(default_factory=list)
    plan_artifact_ref: str | None = None
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
