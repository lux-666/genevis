"""Durable, sample-only intake contracts used by the conversational TUI.

These models intentionally do not reuse :class:`EntryRequest` or
``AskPlanState``.  A TUI draft exists before there is a complete manifest, so
requiring a manifest at this boundary would force premature materialization.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.shared_parameters import SharedParameters

IntakeRole = Literal["hifi_reads", "ont_reads", "hic_reads"]
AssetCandidateRole = Literal[
    "hifi_reads",
    "ont_reads",
    "hic_r1",
    "hic_r2",
    "no_hic",
]
TuiLifecycle = Literal[
    "intake",
    "planning",
    "ready_to_confirm",
    "frozen",
    "running",
    "blocked_review",
    "terminal",
]


class WorkflowIntent(BaseModel):
    """Goal facts collected before sample paths are known."""

    model_config = ConfigDict(extra="forbid")

    goal_text: str = Field(min_length=1)
    sample_id: str | None = Field(default=None, min_length=1)
    target_species: str | None = Field(default=None, min_length=1)
    target_assembly_level: Literal["contig", "scaffold", "chromosome"] = "contig"
    requested_stages: list[str] = Field(default_factory=list)
    requested_assembly_method: Literal[
        "flye",
        "nextdenovo",
        "hifiasm",
        "hifiasm_ont",
    ] | None = None
    use_ultralong_ont: bool = False
    use_hic_phasing: bool = False
    # Intake staging fields for T2T batched collection.  ``telomere_motif``
    # holds the operator-supplied DNA motif before it is projected into
    # ``SharedParameters.species_facts``; it is validated on assignment.
    # ``motif_choice_complete`` is True once the operator has either supplied a
    # valid motif or explicitly deferred the choice (omission / unknown).
    telomere_motif: str | None = Field(default=None, min_length=1)
    motif_choice_complete: bool = False

    @field_validator("goal_text", "sample_id", "target_species", "telomere_motif")
    @classmethod
    def _strip_non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("requested_stages")
    @classmethod
    def _deduplicate_stages(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for stage in value:
            normalized = str(stage).strip()
            if not normalized:
                raise ValueError("requested_stages must not contain blank values")
            if normalized not in result:
                result.append(normalized)
        return result


class HifiReadsFact(BaseModel):
    """Confirmed PacBio HiFi read files for this sample."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["hifi_reads"] = "hifi_reads"
    paths: list[str] = Field(min_length=1)

    @field_validator("paths")
    @classmethod
    def _validate_paths(cls, value: list[str]) -> list[str]:
        normalized = [str(path).strip() for path in value]
        if any(not path for path in normalized):
            raise ValueError("HiFi paths must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("HiFi paths must be unique")
        return normalized


class OntReadsFact(BaseModel):
    """Confirmed ONT read files for this sample."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ont_reads"] = "ont_reads"
    paths: list[str] = Field(min_length=1)

    @field_validator("paths")
    @classmethod
    def _validate_paths(cls, value: list[str]) -> list[str]:
        normalized = [str(path).strip() for path in value]
        if any(not path for path in normalized):
            raise ValueError("ONT paths must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("ONT paths must be unique")
        return normalized


class HiCReadPair(BaseModel):
    """A user-declared Hi-C R1/R2 pair; file names are not interpreted."""

    model_config = ConfigDict(extra="forbid")

    r1: str = Field(min_length=1)
    r2: str = Field(min_length=1)

    @field_validator("r1", "r2")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Hi-C mate path must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_distinct_mates(self) -> "HiCReadPair":
        if self.r1 == self.r2:
            raise ValueError("Hi-C R1 and R2 must be different paths")
        return self


class HiCPairedReadsFact(BaseModel):
    """Confirmed explicit Hi-C pairs for this sample."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["hic_paired_reads"] = "hic_paired_reads"
    pairs: list[HiCReadPair] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_paths(self) -> "HiCPairedReadsFact":
        paths = [path for pair in self.pairs for path in (pair.r1, pair.r2)]
        if len(set(paths)) != len(paths):
            raise ValueError("Hi-C mate paths must be unique across all pairs")
        return self


class NoHiCFact(BaseModel):
    """An explicit operator statement that this sample has no Hi-C reads."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["no_hic"] = "no_hic"


class AssetPathSpan(BaseModel):
    """One exact path-shaped span recovered from a raw TUI answer."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate_span(self) -> "AssetPathSpan":
        if self.end <= self.start:
            raise ValueError("path span end must be greater than start")
        return self


class AssetAnswerCandidate(BaseModel):
    """A role-attributed candidate extracted from one raw TUI answer."""

    model_config = ConfigDict(extra="forbid")

    role: AssetCandidateRole
    paths: list[str] = Field(default_factory=list)
    explicit_role: bool = False
    source_spans: list[AssetPathSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_role_payload(self) -> "AssetAnswerCandidate":
        if self.role == "no_hic":
            if self.paths or self.source_spans:
                raise ValueError("no_hic candidate cannot contain paths")
            return self
        if not self.paths:
            raise ValueError(f"{self.role} candidate requires at least one path")
        if len(set(self.paths)) != len(self.paths):
            raise ValueError(f"{self.role} candidate paths must be unique")
        span_paths = [span.path for span in self.source_spans]
        if span_paths and span_paths != self.paths:
            raise ValueError("candidate source_spans must preserve candidate paths")
        return self


class UnresolvedAssetCandidate(BaseModel):
    """A candidate retained for inspection but not promoted to a fact."""

    model_config = ConfigDict(extra="forbid")

    candidate: AssetAnswerCandidate
    reason: str = Field(min_length=1)


class IntakeAnswerRecord(BaseModel):
    """Append-only audit record for one TUI asset-collection response."""

    model_config = ConfigDict(extra="forbid")

    raw_answer: str
    focused_role: IntakeRole | None = None
    candidates: list[AssetAnswerCandidate] = Field(default_factory=list)
    accepted_roles: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    llm_used: bool = False


class AssetCollectionState(BaseModel):
    """Mutable intake facts plus auditable unresolved response material."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[IntakeRole] = Field(default_factory=list)
    hifi_reads: HifiReadsFact | None = None
    ont_reads: OntReadsFact | None = None
    hic_reads: HiCPairedReadsFact | NoHiCFact | None = None
    unresolved_candidates: list[UnresolvedAssetCandidate] = Field(default_factory=list)
    answer_history: list[IntakeAnswerRecord] = Field(default_factory=list)

    @field_validator("requirements")
    @classmethod
    def _deduplicate_requirements(cls, value: list[IntakeRole]) -> list[IntakeRole]:
        return list(dict.fromkeys(value))


class DegradationDecision(BaseModel):
    """Explicit acknowledgement required after the operator reports no Hi-C."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["no_hic"] = "no_hic"
    confirmed: bool = False


class TuiSessionDraft(BaseModel):
    """Versioned source of truth for a mutable pre-freeze TUI session."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    session_id: str = Field(min_length=1)
    data_dir: str = Field(min_length=1)
    parent_session_id: str | None = Field(default=None, min_length=1)
    request_text: str = Field(min_length=1)
    intent: WorkflowIntent
    intake: AssetCollectionState
    selected_stages: list[str] | None = None
    resolved_execution_order: list[str] | None = None
    selected_methods: dict[str, str | list[str]] | None = None
    shared_parameters: SharedParameters | None = None
    method_parameters: dict[str, dict[str, object]] | None = None
    stage_parameters: dict[str, dict[str, object]] | None = None
    degradation_decision: DegradationDecision | None = None
    lifecycle: TuiLifecycle = "intake"
    revision: int = Field(default=1, ge=1)

    @field_validator("session_id", "parent_session_id", "request_text")
    @classmethod
    def _strip_session_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_degradation_state(self) -> "TuiSessionDraft":
        if self.degradation_decision is not None:
            if not isinstance(self.intake.hic_reads, NoHiCFact):
                raise ValueError(
                    "a no_hic degradation decision requires an explicit no_hic fact"
                )
        return self


# The design document uses ``TuiSessionState`` while the implementation calls
# this mutable pre-freeze object a draft.  Keep one canonical model and a
# semantic alias for callers that prefer the former name.
TuiSessionState = TuiSessionDraft
