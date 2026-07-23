"""Canonical parameter-recommendation facts and snapshot contracts (v3).

Replaces the three stage-specific dictionary-shaped input profiles with one
typed canonical facts model shared by Assembly, AutoHiC, and T2T.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.artifacts import ArtifactRef, ParameterAttemptStatus, ParameterAttemptTask


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
    supplied_telomere_motif: str | None = None


# ---------------------------------------------------------------------------
# Canonical read facts
# ---------------------------------------------------------------------------


class CanonicalReadFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    read_type: str | None = None
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
    """The sole common biological and quantitative facts model for parameter recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    species: CanonicalSpeciesFacts = Field(default_factory=CanonicalSpeciesFacts)
    reads: CanonicalReadFacts = Field(default_factory=CanonicalReadFacts)
    genome: CanonicalGenomeFacts = Field(default_factory=CanonicalGenomeFacts)
    sequencing: CanonicalSequencingFacts = Field(default_factory=CanonicalSequencingFacts)
    asset_presence: CanonicalAssetPresence = Field(default_factory=CanonicalAssetPresence)


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

    decision: Literal[
        "accept_candidate", "keep_prior_assembly", "request_parameter_retry"
    ] | None = None
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

    trigger: ExecutionFailureTrigger | AutoHiCReviewEvidence | AssemblyReviewEvidence
    attempt_history: tuple[ParameterAttemptEvidenceV3, ...]


# ---------------------------------------------------------------------------
# Stage-owned context models (discriminated union on ``stage``)
# ---------------------------------------------------------------------------

NormalizedReadType = Literal["hifi", "ont", "pacbio_raw", "nano_raw", "nano_hq"]


class AssemblyRecommendationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Literal["assembly"]
    assembly_route: Literal["hifi", "ont", "cyclone"]
    read_type: NormalizedReadType
    target_level: Literal["draft", "near_t2t", "strict_t2t"]
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
    resolved_telomere_motif: str = Field(min_length=2)
    motif_source: Literal["provided_species_fact", "llm_species_resolution"]
    ont_asset_count: int = Field(ge=1)


StageRecommendationContext = Annotated[
    AssemblyRecommendationContext
    | AutoHiCRecommendationContext
    | T2TRecommendationContext,
    Field(discriminator="stage"),
]

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


# ---------------------------------------------------------------------------
# Input snapshot
# ---------------------------------------------------------------------------


class RecommendationInputSnapshotV3(BaseModel):
    """Persisted input snapshot for one parameter recommendation point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = 3
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
        retry = getattr(ctx, "retry", None)
        refs = self.dependency_refs
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
        return self


# ---------------------------------------------------------------------------
# T2T motif input snapshot
# ---------------------------------------------------------------------------

T2T_MOTIF_RESOLVER_ID = "t2t.telomere_motif_resolver"


class T2TMotifInputSnapshotV3(BaseModel):
    """Persisted input snapshot for T2T telomere motif resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = 3
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
            raise ValueError(
                "motif snapshot must not contain motif_resolution_ref"
            )
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
