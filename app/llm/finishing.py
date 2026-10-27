"""Strict, evidence-driven finishing actions; execution remains stage-owned."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.llm.recommender import OutputMethod, StructuredRecommender
from app.schemas.t2t import T2TThresholds


class GapParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    min_identity: float = Field(gt=0, le=1)
    min_match: int = Field(ge=1)
    gap_size_check: bool


class PolishParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    window_length: int = Field(ge=1, le=10000)
    error_threshold: float = Field(gt=0, le=1)
    # Racon scoring values affect consensus, not just runtime.
    match: int = Field(ge=1, le=20)
    mismatch: int = Field(ge=-20, le=-1)
    gap: int = Field(ge=-20, le=-1)


PARAMETERS: dict[str, type[BaseModel]] = {
    "gapcloser": GapParameters,
    "base_polish": PolishParameters,
    "t2t": T2TThresholds,
}


class TargetDiagnostic(BaseModel):
    """Per-target evidence summary. It does not authorize a quality veto."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    target_id: str = Field(min_length=1)
    artifact_refs: list[str]
    uncertainty: str = ""
    next_action: Literal["retry", "switch_method", "next_cluster", "stop"]


class MethodChoice(BaseModel):
    """Applicability of one registered method. Empty means the current method."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    method_id: str = Field(min_length=1)
    applicable: bool
    reason: str = Field(min_length=1)
    unavailable_reason: str = ""


class FinishingRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    action: Literal["run", "accept", "keep_source", "human_review"]
    parameters: GapParameters | PolishParameters | T2TThresholds | None = None
    selected_attempt: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1)
    evidence_gaps: list[str]
    diagnostics: list[TargetDiagnostic] = Field(default_factory=list)
    methods: list[MethodChoice] = Field(default_factory=list)
    expected_new_evidence: str = ""

    @model_validator(mode="after")
    def action_shape(self) -> FinishingRecommendation:
        if (self.action == "run") != (self.parameters is not None):
            raise ValueError("only run supplies parameters")
        if (self.action == "accept") != (self.selected_attempt is not None):
            raise ValueError("only accept selects an attempt")
        return self


class FinishingRecommender(StructuredRecommender[FinishingRecommendation]):
    def __init__(self, stage: str, method: OutputMethod = "json_schema") -> None:
        self.stage = stage
        super().__init__(
            FinishingRecommendation.model_validate,
            schema=FinishingRecommendation,
            method=method,
        )

    def messages(self, evidence: dict[str, Any]) -> list[dict[str, str]]:
        instructions = {
            "gapcloser": "Only TGS-GapCloser HiFi --ne is implemented. Use "
            "actual gap sizes, "
            "fill details and validation. Fewer Ns do not establish correct sequence; "
            "segment consistency is structural evidence only.",
            "t2t": "The implemented method maps all manifest-declared long reads, "
            "clusters terminal anchors, reconstructs tails with bounded POA consensus, "
            "and maps reads back to the complete candidate assembly. Inspect terminal "
            "motifs, molecule support, competing placements, MAPQ, overlap, junction "
            "distances, rejection reasons, junction support and extension coverage. "
            "Read-back uses the same input reads: it is in-sample evidence, not "
            "independent QV. Representative tails are comparators, not a separate "
            "assembler. No de novo local assembler or alternate-method switching is "
            "implemented. A telomere signal does not establish chromosome "
            "completeness. "
            "Do not lower support merely to produce more extensions. A retry must name "
            "expected_new_evidence. switch_method is unavailable until another "
            "registered method is applicable. Quality status does not change "
            "the zero-wait adoption action.",
            "base_polish": "Only a single HiFi Racon round per candidate is "
            "implemented. "
            "All configurations start from the original source, never serially polish "
            "an earlier candidate. Before/after alignment uses the correction reads; "
            "this is in-sample agreement, not independent QV. It alone cannot justify "
            "acceptance or additional attempts. If independent accuracy evidence is "
            "missing, prefer keep_source or human_review unless a new configuration "
            "has a specific, evidence-grounded hypothesis.",
        }
        zero_wait = evidence.get("execution_policy") == "run_valid_outputs"
        if zero_wait and self.stage == "base_polish":
            instructions["base_polish"] = (
                "Only a single HiFi Racon round per candidate is implemented. "
                "All configurations start from the original source. Before/after "
                "alignment uses correction reads and is not independent QV. "
                "Report that limitation without using it as an execution veto."
            )
        policy = (
            " Current execution policy is run_valid_outputs. Missing quality "
            "evidence is advisory and must not prevent execution. Before the "
            "first attempt, recommend valid run parameters when budget remains. "
            "After a materialized result, recommend accept; if no new result "
            "was materialized, prefer keep_source. Do not request human review "
            "solely for missing independent accuracy evidence. "
            if zero_wait
            else ""
        )
        return [
            {
                "role": "system",
                "content": "Advise genome finishing using only the supplied evidence. "
                + instructions[self.stage]
                + " Every attempt starts from the same source assembly. "
                "QUAST without a reference "
                "provides contiguity statistics, not accuracy. Do not invent "
                "BUSCO, QV, k-mer "
                "completeness or independent validation. Report evidence gaps. "
                "Choose run, "
                "accept an existing attempt, keep_source, or human_review. Never "
                "repeat a "
                "configuration. Respect remaining_attempts. Only use the "
                "parameter shape for "
                + self.stage
                + ": "
                + json.dumps(PARAMETERS[self.stage].model_json_schema())
                + policy
                + ". Actions use the configured review countdown policy. "
                "Response schema: "
                + json.dumps(FinishingRecommendation.model_json_schema()),
            },
            {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
        ]
