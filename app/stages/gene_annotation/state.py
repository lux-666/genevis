"""Typed checkpoint boundary for the gene-annotation stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypeAliasType, TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)

if TYPE_CHECKING:
    CursorJsonValue: TypeAlias = (
        None
        | bool
        | int
        | float
        | str
        | list["CursorJsonValue"]
        | dict[str, "CursorJsonValue"]
    )
else:
    CursorJsonValue = TypeAliasType(
        "CursorJsonValue",
        None
        | bool
        | int
        | float
        | str
        | list["CursorJsonValue"]
        | dict[str, "CursorJsonValue"],
    )

GenomeInputSource = Literal["softmasked_genome", "assembly"]
EvidenceProfile = Literal[
    "no_evidence",
    "rnaseq",
    "long_rna",
    "protein",
    "mixed_evidence",
    "provided_annotation",
]


class CandidateBundleCursor(TypedDict, total=False):
    """JSON projection of the validated ``CandidateBundle`` artifact."""

    method_name: str
    category: str
    annotation_gff_ref: str
    annotation_gff_artifact_ref: ArtifactRefJson
    proteins_fasta_ref: str
    proteins_fasta_artifact_ref: ArtifactRefJson
    cds_fasta_ref: str | None
    cds_fasta_artifact_ref: ArtifactRefJson | None
    metrics_ref: str | None
    provenance_ref: str | None
    evm_evidence_class: str
    evm_source_type: str
    status: Literal["materialized", "materialization_failed"]


@with_config(ConfigDict(extra="forbid"))
class GeneAnnotationCursor(TypedDict, total=False):
    """All JSON-only keys owned by ``stage_outputs.gene_annotation``."""

    status: str
    reason: str
    failure_kind: str
    execution_mode: str
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    method_name: str
    execution_attempts: list[dict[str, CursorJsonValue]]

    genome_input_ref: str | None
    genome_input_source: GenomeInputSource
    genome_input_artifact_ref: ArtifactRefJson
    genome_input_asset_id: str
    unmasked_fallback_reason: str | None
    repeat_annotation_ref: str | None
    rnaseq_read_refs: list[str]
    long_rna_read_refs: list[str]
    protein_evidence_refs: list[str]
    rnaseq_read_asset_ids: list[str]
    long_rna_read_asset_ids: list[str]
    protein_evidence_asset_ids: list[str]
    lineage_or_species_hints: dict[str, CursorJsonValue]
    evidence_profile: EvidenceProfile
    available_evidence: list[str]

    candidate_methods: list[str]
    selected_methods: list[str]
    helper_methods: list[str]
    confirmed_methods: list[str]
    executed_methods: list[str]
    method_selection_required: bool
    expected_outputs: dict[str, str]
    candidate_refs: dict[str, str]
    accepted_candidate_refs: dict[str, str]
    candidate_gene_counts: dict[str, int]
    candidate_artifact_refs: dict[str, dict[str, CursorJsonValue]]
    candidate_bundles: dict[str, CandidateBundleCursor]
    candidate_gff_ref: str
    miniprot_gene_count: int
    augustus_gene_count: int

    review_required: bool
    review_triggers: list[str]
    review_policy: dict[str, CursorJsonValue]
    active_gate_id: str | None
    review_request_artifact_ref: dict[str, CursorJsonValue]
    review_policy_outcome: dict[str, CursorJsonValue]
    auto_decidable: bool
    pending_human_confirmation: bool
    decision: str
    decided_by: str
    gene_method_planning_review_decision_ref: dict[str, CursorJsonValue]
    candidate_evidence_review_decision_ref: dict[str, CursorJsonValue]
    merge_review_decision_ref: dict[str, CursorJsonValue]
    final_gene_set_review_decision_ref: dict[str, CursorJsonValue]

    merge_weights: dict[str, int | float]
    merge_method: str
    final_gff_ref: str | None
    final_proteins_ref: str | None
    final_cds_ref: str | None
    final_gff_artifact_ref: dict[str, CursorJsonValue]
    final_proteins_artifact_ref: ArtifactRefJson
    final_cds_artifact_ref: ArtifactRefJson | None
    busco_completeness: int | float | None
    busco_completeness_status: Literal["pending", "available", "missing", "failed"]
    busco_note: str
    busco_metrics: dict[str, CursorJsonValue]
    busco_summary_ref: str
    busco_summary_artifact_ref: dict[str, CursorJsonValue]

    plan_artifact_ref: str
    artifact_refs: list[str]
    gene_models_ref: str
    gene_models: str
    genes_gff3_ref: str
    transcripts_fasta_ref: str | None
    predicted_proteins_ref: str
    predicted_proteins: str
    proteins_fasta_ref: str
    evidence_report_ref: str | None
    bundle_artifact_ref: str
    bundle_status: str
    notes: list[str]


GENE_ANNOTATION_CURSOR_ADAPTER = TypeAdapter(GeneAnnotationCursor)
CANDIDATE_BUNDLE_CURSOR_ADAPTER = TypeAdapter(CandidateBundleCursor)
CURSOR_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, CursorJsonValue])


def gene_annotation_output(state: TopLevelState) -> GeneAnnotationCursor:
    """Read and validate the gene-annotation cursor at its package boundary."""

    return validated_stage_output(
        state,
        "gene_annotation",
        GENE_ANNOTATION_CURSOR_ADAPTER,
    )


def update_gene_annotation_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge, validate, and JSON-serialize a gene-annotation cursor update."""

    return update_validated_stage_output(
        state,
        "gene_annotation",
        payload,
        GENE_ANNOTATION_CURSOR_ADAPTER,
    )
