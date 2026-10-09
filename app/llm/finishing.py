"""Strict, evidence-driven finishing actions; execution remains stage-owned."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.llm.recommender import OutputMethod, StructuredRecommender


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


class TelomereParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    min_motif_copies: int = Field(ge=1)
    terminal_window_bp: int = Field(ge=1)
    min_mapq: int = Field(ge=0, le=255)
    max_target_terminal_distance_bp: int = Field(ge=0)
    min_overlap_bp: int = Field(ge=1)
    min_supporting_reads: int = Field(ge=1)


PARAMETERS: dict[str, type[BaseModel]] = {
    "gapcloser": GapParameters,
    "base_polish": PolishParameters,
    "t2t": TelomereParameters,
}


class FinishingRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    action: Literal["run", "accept", "keep_source", "human_review"]
    parameters: GapParameters | PolishParameters | TelomereParameters | None
    selected_attempt: int | None = Field(ge=0)
    reason: str = Field(min_length=1)
    evidence_gaps: list[str]

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
            "t2t": "Only the existing terminal motif/minimap2 evidence and "
            "compatible-tail "
            "patch method is implemented. Inspect terminal motif-copy observations, "
            "read support, MAPQ, overlap, junction distances and candidate rejection "
            "reasons. A telomere signal does not establish chromosome completeness. "
            "Do not lower support merely to produce more extensions.",
            "base_polish": "Only a single HiFi Racon round per candidate is "
            "implemented. "
            "All configurations start from the original source, never serially polish "
            "an earlier candidate. Before/after alignment uses the correction reads; "
            "this is in-sample agreement, not independent QV. It alone cannot justify "
            "acceptance or additional attempts. If independent accuracy evidence is "
            "missing, prefer keep_source or human_review unless a new configuration "
            "has a specific, evidence-grounded hypothesis.",
        }
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
                + ". Human approval is required for every action. Response schema: "
                + json.dumps(FinishingRecommendation.model_json_schema()),
            },
            {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
        ]
