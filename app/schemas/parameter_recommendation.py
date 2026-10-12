"""Canonical parameter-recommendation facts and snapshot contracts (v3).

Replaces the three stage-specific dictionary-shaped input profiles with one
typed canonical facts model shared by Assembly, AutoHiC, and T2T.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domains.assembly.selection import NormalizedReadType
from app.integrity import SHA256_PATTERN
from app.schemas.artifacts import (
    ArtifactRef,
    ParameterAttemptStatus,
    ParameterAttemptTask,
)
from app.schemas.shared_parameters import DnaMotif

# ---------------------------------------------------------------------------
# Quantity facts
# ---------------------------------------------------------------------------

QuantityUnit = Literal[
    "bp",
    "x",
    "fraction",
    "relative_change",
    "count",
    "phred",
]

QuantityOriginKind = Literal["provided", "measured", "derived", "resolved"]

QuantityProducer = Literal[
    "run_config",
    "read_qc",
    "genome_survey",
    "assembly",
    "hic",
    "t2t",
    "parameter_recommendation",
]


class QuantityOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: QuantityOriginKind
    producer: QuantityProducer


class QuantityFact(BaseModel):
    """A single numeric measurement with explicit unit and semantic origin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | int
    unit: QuantityUnit
    origin: QuantityOrigin

    @field_validator("value", mode="before")
    @classmethod
    def reject_non_finite_and_bool(cls, v: Any) -> float | int:
        if isinstance(v, bool):
            raise ValueError("QuantityFact.value must not be a boolean")
        if not isinstance(v, (int, float)):
            raise ValueError("QuantityFact.value must be numeric")
        if not math.isfinite(float(v)):
            raise ValueError("QuantityFact.value must be finite")
        return v

    @model_validator(mode="after")
    def validate_unit_constraints(self) -> "QuantityFact":
        v = float(self.value)
        if self.unit == "fraction" and not (0.0 <= v <= 1.0):
            raise ValueError("fraction values must be in [0, 1]")
        if self.unit == "relative_change" and v <= -1.0:
            raise ValueError("relative_change must be greater than -1")
        if self.unit == "count" and (not isinstance(self.value, int) or self.value < 0):
            raise ValueError("count must be a non-negative integer")
        return self


# ---------------------------------------------------------------------------
# Canonical species facts
# ---------------------------------------------------------------------------


class CanonicalSpeciesFacts(BaseModel):
    """Species-level facts that all stage recommenders share."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scientific_name: str | None = None
    taxon_id: str | None = None
    clade: str | None = None
    ploidy: QuantityFact | None = None
    expected_chromosome_count: QuantityFact | None = None
    supplied_telomere_motif: DnaMotif | None = None


# ---------------------------------------------------------------------------
# Canonical read facts
# ---------------------------------------------------------------------------


class CanonicalReadFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    read_type: NormalizedReadType | None = None
    read_count: QuantityFact | None = None
    total_bases: QuantityFact | None = None
    read_n50: QuantityFact | None = None
    mean_read_length: QuantityFact | None = None
    longest_read: QuantityFact | None = None
    q20_fraction: QuantityFact | None = None
    mean_quality: QuantityFact | None = None


# ---------------------------------------------------------------------------
# Canonical genome facts
# ---------------------------------------------------------------------------


class CanonicalGenomeFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effective_genome_size: QuantityFact | None = None
    measured_genome_size: QuantityFact | None = None
    provided_genome_size: QuantityFact | None = None
    provided_vs_measured_relative_change: QuantityFact | None = None
    heterozygosity: QuantityFact | None = None
    repeat_fraction: QuantityFact | None = None
    estimate_confidence: Literal["high", "low", "none"] | None = None


# ---------------------------------------------------------------------------
# Canonical sequencing facts
# ---------------------------------------------------------------------------


class CanonicalSequencingFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_coverage: QuantityFact | None = None


# ---------------------------------------------------------------------------
# Canonical asset-presence flags
# ---------------------------------------------------------------------------


class CanonicalAssetPresence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    has_hifi: bool | None = None
    has_ont: bool | None = None
    has_hic: bool | None = None
    has_rnaseq: bool | None = None


# ---------------------------------------------------------------------------
# Combined canonical facts
# ---------------------------------------------------------------------------


class CanonicalRecommendationFacts(BaseModel):
    """Common biological and quantitative facts for parameter recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    species: CanonicalSpeciesFacts = Field(default_factory=CanonicalSpeciesFacts)
    reads: CanonicalReadFacts = Field(default_factory=CanonicalReadFacts)
    genome: CanonicalGenomeFacts = Field(default_factory=CanonicalGenomeFacts)
    sequencing: CanonicalSequencingFacts = Field(
        default_factory=CanonicalSequencingFacts
    )
    asset_presence: CanonicalAssetPresence = Field(
        default_factory=CanonicalAssetPresence
    )


# ---------------------------------------------------------------------------
# Retry evidence models
# ---------------------------------------------------------------------------


class ParameterExecutionEvidence(BaseModel):
    """Bounded execution evidence for retry context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str | None = None
    return_code: int | None = None
    failure_class: str | None = Field(default=None, max_length=120)
    status_detail: str | None = Field(default=None, max_length=500)
    resource_metrics: list[float] = Field(default_factory=list, max_length=16)
    stderr_tail: str | None = Field(default=None, max_length=4000)

    @field_validator("resource_metrics", mode="before")
    @classmethod
    def validate_finite_metrics(cls, v: Any) -> list[float]:
        if not isinstance(v, list):
            raise ValueError("resource_metrics must be a list")
        validated = []
        for item in v:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError("resource_metrics entries must be numeric")
            if not math.isfinite(float(item)):
                raise ValueError("resource_metrics entries must be finite")
            validated.append(float(item))
        return validated


class AssemblyReviewEvidence(BaseModel):
    """Bounded assembly review evidence for retry context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_mode: str | None = None
    assembly_size: float | None = None
    assembly_size_ratio: float | None = None
    contig_count: int | None = None
    contig_n50: float | None = None
    contig_n90: float | None = None
    largest_contig: float | None = None
    busco_complete: float | None = None
    busco_single: float | None = None
    busco_duplicate: float | None = None
    busco_fragmented: float | None = None
    busco_missing: float | None = None
    quast_metrics: dict[str, Any] | None = None


class AutoHiCReviewEvidence(BaseModel):
    """Bounded AutoHiC reviewer evidence for retry context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: (
        Literal["accept_candidate", "keep_prior_assembly", "request_parameter_retry"]
        | None
    ) = None
    notes: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("notes", mode="before")
    @classmethod
    def truncate_note_length(cls, v: Any) -> list[str]:
        if not isinstance(v, list):
            raise ValueError("notes must be a list")
        return [str(n)[:500] for n in v]


class ParameterAttemptEvidenceV3(BaseModel):
    """Bounded evidence for one sealed predecessor attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: int = Field(ge=0)
    task: ParameterAttemptTask
    status: ParameterAttemptStatus
    recommended_parameters: dict[str, Any]
    approved_parameters: dict[str, Any]
    effective_parameters: dict[str, Any]
    effective_sources: dict[str, Literal["l0", "l1", "l2"]]
    execution: ParameterExecutionEvidence | None = None
    review: AssemblyReviewEvidence | AutoHiCReviewEvidence | None = None


# ---------------------------------------------------------------------------
# Retry context
# ---------------------------------------------------------------------------


class ExecutionFailureTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str | None = None
    return_code: int | None = None
    failure_class: str | None = Field(default=None, max_length=120)
    status_detail: str | None = Field(default=None, max_length=500)


class ReviewFailureTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review: AssemblyReviewEvidence | AutoHiCReviewEvidence


class RecommendationRetryContext(BaseModel):
    """Retry evidence present only when retrying after failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger: ExecutionFailureTrigger | ReviewFailureTrigger
    attempt_history: tuple[ParameterAttemptEvidenceV3, ...]


# ---------------------------------------------------------------------------
# Stage-owned context models (discriminated union on ``stage``)
# ---------------------------------------------------------------------------


class AssemblyMaterialContext(BaseModel):
    """Resolved input roles shared by method and parameter recommendations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hifi_files: int = Field(default=0, ge=0)
    ont_files: int = Field(default=0, ge=0)
    ultralong_ont_files: int = Field(default=0, ge=0)
    hic_pairs: int = Field(default=0, ge=0)
    trio_maternal_files: int = Field(default=0, ge=0)
    trio_paternal_files: int = Field(default=0, ge=0)
    phasing_mode: Literal["none", "hic", "trio"] = "none"


class AssemblyRecommendationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Literal["assembly"]
    assembly_route: Literal["hifi", "ont", "cyclone"]
    read_type: NormalizedReadType
    target_level: Literal["draft", "near_t2t", "strict_t2t"]
    material_inputs: AssemblyMaterialContext = Field(
        default_factory=AssemblyMaterialContext
    )
    retry: RecommendationRetryContext | None = None


class AutoHiCRecommendationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Literal["hic_scaffolding"]
    library_protocols: tuple[str, ...]
    provided_restriction_enzyme: str | None = None
    retry: RecommendationRetryContext | None = None

    @field_validator("library_protocols", mode="before")
    @classmethod
    def validate_protocols(cls, v: Any) -> tuple[str, ...]:
        if not isinstance(v, (list, tuple)):
            raise ValueError("library_protocols must be a list")
        protocols = sorted({str(p).strip()[:120] for p in v if str(p).strip()})
        if len(protocols) > 64:
            raise ValueError("library_protocols may contain at most 64 entries")
        return tuple(protocols)


class T2TRecommendationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Literal["t2t"]
    resolved_telomere_motif: DnaMotif
    motif_source: Literal["provided_species_fact", "llm_species_resolution"]
    long_read_asset_count: int = Field(ge=1)


StageRecommendationContext = Annotated[
    AssemblyRecommendationContext
    | AutoHiCRecommendationContext
    | T2TRecommendationContext,
    Field(discriminator="stage"),
]


def _contains_quantity_producer(value: Any, producer: str) -> bool:
    if isinstance(value, dict):
        origin = value.get("origin")
        if isinstance(origin, dict) and origin.get("producer") == producer:
            return True
        return any(
            _contains_quantity_producer(item, producer) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_quantity_producer(item, producer) for item in value)
    return False


# ---------------------------------------------------------------------------
# Dependency refs
# ---------------------------------------------------------------------------


class RecommendationDependencyRefs(BaseModel):
    """Audit-only artifact references for one parameter recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_source_refs: dict[
        Literal["read_qc_summary", "genome_survey_result"],
        ArtifactRef,
    ] = Field(default_factory=dict)
    stage_input_refs: tuple[ArtifactRef, ...] = Field(default=())
    initial_input_snapshot_ref: ArtifactRef | None = None
    prior_attempt_manifest_refs: tuple[ArtifactRef, ...] = Field(default=())
    motif_resolution_ref: ArtifactRef | None = None

    @field_validator("stage_input_refs", mode="before")
    @classmethod
    def limit_stage_input_refs(cls, v: Any) -> tuple[ArtifactRef, ...]:
        refs = tuple(v) if not isinstance(v, tuple) else v
        if len(refs) > 1024:
            raise ValueError("stage_input_refs must not exceed 1024 entries")
        return refs

    @model_validator(mode="after")
    def validate_canonical_refs(self) -> "RecommendationDependencyRefs":
        stage_keys = [
            (ref.artifact_type, ref.path_ref, ref.sha256)
            for ref in self.stage_input_refs
        ]
        if len(stage_keys) != len(set(stage_keys)):
            raise ValueError("stage_input_refs must not contain duplicates")

        prior_keys = [
            (ref.artifact_type, ref.path_ref, ref.sha256)
            for ref in self.prior_attempt_manifest_refs
        ]
        if len(prior_keys) != len(set(prior_keys)):
            raise ValueError("prior_attempt_manifest_refs must not contain duplicates")
        return self


# ---------------------------------------------------------------------------
# Input snapshot
# ---------------------------------------------------------------------------


class RecommendationInputSnapshotV3(BaseModel):
    """Persisted input snapshot for one parameter recommendation point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3]
    snapshot_kind: Literal["parameter_recommendation"] = "parameter_recommendation"
    method_id: str = Field(min_length=1)
    task: ParameterAttemptTask
    facts: CanonicalRecommendationFacts
    stage_context: StageRecommendationContext
    dependency_refs: RecommendationDependencyRefs = Field(
        default_factory=RecommendationDependencyRefs
    )

    @model_validator(mode="after")
    def validate_stage_matches_method(self) -> "RecommendationInputSnapshotV3":
        stage = self.stage_context.stage
        method = self.method_id
        prefix_map = {
            "assembly": "assembly.",
            "hic_scaffolding": "hic_scaffolding.",
            "t2t": "t2t.",
        }
        prefix = prefix_map.get(stage, "")
        if prefix and not method.startswith(prefix):
            raise ValueError(
                f"method_id {method!r} does not match stage context {stage!r}"
            )
        return self

    @model_validator(mode="after")
    def validate_retry_refs_consistency(self) -> "RecommendationInputSnapshotV3":
        ctx = self.stage_context
        retry = (
            ctx.retry
            if isinstance(
                ctx,
                (AssemblyRecommendationContext, AutoHiCRecommendationContext),
            )
            else None
        )
        refs = self.dependency_refs
        is_retry_task = self.task != "recommend_initial"
        if is_retry_task != (retry is not None):
            raise ValueError(
                "retry recommendation tasks require typed retry context, and "
                "initial tasks must not contain it"
            )
        if retry is None:
            if refs.initial_input_snapshot_ref is not None:
                raise ValueError(
                    "initial_input_snapshot_ref must be absent for an initial attempt"
                )
            if refs.prior_attempt_manifest_refs:
                raise ValueError(
                    "prior_attempt_manifest_refs must be empty for an initial attempt"
                )
        else:
            if refs.initial_input_snapshot_ref is None:
                raise ValueError(
                    "initial_input_snapshot_ref is required for a retry attempt"
                )
            history_len = len(retry.attempt_history)
            if len(refs.prior_attempt_manifest_refs) != history_len:
                raise ValueError(
                    "prior_attempt_manifest_refs must have one entry per history item"
                )
        if self.stage_context.stage == "t2t":
            if refs.motif_resolution_ref is None:
                raise ValueError("T2T parameter snapshots require motif_resolution_ref")
            has_exact_asset_refs = (
                len(refs.stage_input_refs)
                == self.stage_context.long_read_asset_count + 1
            )
            registry_ref_count = sum(
                ref.artifact_type == "asset_registry" for ref in refs.stage_input_refs
            )
            has_registry_manifest = registry_ref_count == 1 and len(
                refs.stage_input_refs
            ) in {1, 2}
            if not has_exact_asset_refs and not has_registry_manifest:
                raise ValueError(
                    "T2T parameter snapshot stage_input_refs must contain the input "
                    "assembly identity plus either every long-read artifact or "
                    "one asset_registry ref covering registered inputs"
                )
        elif refs.motif_resolution_ref is not None:
            raise ValueError(
                "motif_resolution_ref is allowed only for T2T parameter snapshots"
            )

        facts_payload = self.facts.model_dump(mode="json", exclude_none=True)
        required_fact_refs = {
            "read_qc_summary": _contains_quantity_producer(facts_payload, "read_qc"),
            "genome_survey_result": _contains_quantity_producer(
                facts_payload, "genome_survey"
            ),
        }
        for role, required in required_fact_refs.items():
            if required and role not in refs.fact_source_refs:
                raise ValueError(
                    f"dependency_refs.{role} is required by canonical fact origins"
                )
        return self


# ---------------------------------------------------------------------------
# T2T motif input snapshot
# ---------------------------------------------------------------------------

T2T_MOTIF_RESOLVER_ID: Literal["t2t.telomere_motif_resolver"] = (
    "t2t.telomere_motif_resolver"
)


class T2TMotifInputSnapshotV3(BaseModel):
    """Persisted input snapshot for T2T telomere motif resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3]
    snapshot_kind: Literal["t2t_motif_resolution"] = "t2t_motif_resolution"
    resolver_id: Literal["t2t.telomere_motif_resolver"] = T2T_MOTIF_RESOLVER_ID
    facts: CanonicalSpeciesFacts
    dependency_refs: RecommendationDependencyRefs = Field(
        default_factory=RecommendationDependencyRefs
    )

    @model_validator(mode="after")
    def validate_no_retry_refs(self) -> "T2TMotifInputSnapshotV3":
        refs = self.dependency_refs
        if refs.initial_input_snapshot_ref is not None:
            raise ValueError(
                "motif snapshot must not contain initial_input_snapshot_ref"
            )
        if refs.prior_attempt_manifest_refs:
            raise ValueError(
                "motif snapshot must not contain prior_attempt_manifest_refs"
            )
        if refs.motif_resolution_ref is not None:
            raise ValueError("motif snapshot must not contain motif_resolution_ref")
        return self


# ---------------------------------------------------------------------------
# Persisted recommendation-chain manifests
# ---------------------------------------------------------------------------


ParameterRecommendationStage = Literal["assembly", "hic_scaffolding", "t2t"]


class ParameterRecommendationManifestRefsV3(BaseModel):
    """Exact immutable records owned by one v3 recommendation manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_snapshot_ref: ArtifactRef
    basis_snapshot_ref: ArtifactRef
    model_request_ref: ArtifactRef
    model_response_ref: ArtifactRef
    response_parse_ref: ArtifactRef
    recommendation_ref: ArtifactRef


class ParameterRecommendationManifestV3(BaseModel):
    """Strict root of one parameter-recommendation evidence chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3]
    recommendation_contract: Literal["parameter_recommendation_v1"]
    stage: ParameterRecommendationStage
    method_id: str = Field(min_length=1)
    task: ParameterAttemptTask
    recommendation_id: int = Field(ge=0)
    status: Literal["response_invalid", "pending_approval"]
    previous_attempt_id: int | None = Field(default=None, ge=0)
    previous_attempt_manifest_ref: ArtifactRef | None = None
    previous_attempt_seal_hash: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    refs: ParameterRecommendationManifestRefsV3
    summary: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_attempt_chain_identity(self) -> "ParameterRecommendationManifestV3":
        if self.task == "recommend_initial":
            if self.recommendation_id != 0 or self.previous_attempt_id is not None:
                raise ValueError(
                    "initial recommendation manifest must be recommendation 0 "
                    "without a predecessor"
                )
            if self.previous_attempt_manifest_ref is not None:
                raise ValueError(
                    "initial recommendation manifest cannot bind a predecessor manifest"
                )
            if self.previous_attempt_seal_hash is not None:
                raise ValueError(
                    "initial recommendation manifest cannot bind a predecessor seal"
                )
        else:
            if (
                self.recommendation_id == 0
                or self.previous_attempt_id != self.recommendation_id - 1
            ):
                raise ValueError(
                    "retry recommendation manifest must bind the immediately "
                    "preceding attempt"
                )
            if self.previous_attempt_manifest_ref is None:
                raise ValueError(
                    "retry recommendation manifest requires predecessor manifest ref"
                )
            if self.previous_attempt_seal_hash is None:
                raise ValueError(
                    "retry recommendation manifest requires predecessor seal hash"
                )
        return self


class T2TMotifResolutionManifestRefsV2(BaseModel):
    """Conditional references for one T2T motif resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_snapshot_ref: ArtifactRef
    resolution_ref: ArtifactRef
    model_request_ref: ArtifactRef | None = None
    model_response_ref: ArtifactRef | None = None
    response_parse_ref: ArtifactRef | None = None


class T2TMotifResolutionManifestV2(BaseModel):
    """Strict schema-2 motif manifest with provided/LLM conditional refs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    resolution_contract: Literal["t2t_telomere_motif_v1"]
    resolver_id: Literal["t2t.telomere_motif_resolver"]
    resolution_mode: Literal[
        "provided_species_fact",
        "llm_species_resolution",
    ]
    status: Literal["resolved", "unresolved", "invalid"]
    refs: T2TMotifResolutionManifestRefsV2

    @model_validator(mode="after")
    def validate_conditional_model_refs(self) -> "T2TMotifResolutionManifestV2":
        model_refs = (
            self.refs.model_request_ref,
            self.refs.model_response_ref,
            self.refs.response_parse_ref,
        )
        if self.resolution_mode == "provided_species_fact":
            if any(ref is not None for ref in model_refs):
                raise ValueError("provided motif manifests must not contain model refs")
            if self.status != "resolved":
                raise ValueError("provided motif resolution must be resolved")
        elif any(ref is None for ref in model_refs):
            raise ValueError("LLM motif manifests require all model refs")
        return self


# ---------------------------------------------------------------------------
# Resolved parameter sources (frozen evidence)
# ---------------------------------------------------------------------------


class ResolvedParameterSources(BaseModel):
    """Provenance-separated view of all effective parameter values after resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    l0_executor: dict[str, Any] = Field(default_factory=dict)
    l1_facts: dict[str, Any] = Field(default_factory=dict)
    l2_approved: dict[str, Any] = Field(default_factory=dict)
    effective_parameters: dict[str, Any] = Field(default_factory=dict)
    effective_source_by_parameter: dict[str, Literal["l0", "l1", "l2"]] = Field(
        default_factory=dict
    )
    applications: dict[str, Any] = Field(default_factory=dict)
