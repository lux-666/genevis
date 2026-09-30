"""Typed purge_dups proposals, independent of execution and review state."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.llm.recommender import OutputMethod, StructuredRecommender


class PurgeParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    coverage_mode: Literal["read_depth", "self_alignment"]
    self_alignment_preset: Literal["asm5", "asm10", "asm20"]
    end_only: bool


class PurgeRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    action: Literal["run", "accept", "keep_source", "human_review"]
    parameters: PurgeParameters | None
    selected_attempt: int | None
    reason: str = Field(min_length=1)
    evidence_gaps: list[str]

    @model_validator(mode="after")
    def validate_action(self) -> PurgeRecommendation:
        if (self.action == "run") != (self.parameters is not None):
            raise ValueError("only run recommendations must supply parameters")
        if (self.action == "accept") != (self.selected_attempt is not None):
            raise ValueError("only accept recommendations must select an attempt")
        if self.selected_attempt is not None and self.selected_attempt < 0:
            raise ValueError("selected_attempt must be non-negative")
        return self


class PurgeRecommender(StructuredRecommender[PurgeRecommendation]):
    """An example subclass whose fields differ from sparse parameter advisors."""

    def __init__(self, method: OutputMethod = "json_schema") -> None:
        super().__init__(
            PurgeRecommendation.model_validate,
            schema=PurgeRecommendation,
            method=method,
        )

    def messages(self, evidence: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Recommend a purge_dups strategy using only supplied observations. "
                    "Every attempt starts from the same original assembly. read_depth "
                    "maps reads, runs pbcstat/calcuts, then purge_dups -2 -T "
                    "cutoffs -c PB.base.cov. self_alignment runs purge_dups on the "
                    "self PAF without -2/-T/-c. It avoids depth-based removal "
                    "but is not universally superior. end_only controls get_seqs -e; "
                    "true restricts overlap trimming to contig ends. Prefer preserving "
                    "sequence when evidence is insufficient. Length, N50 and duplicate "
                    "interval counts alone do not prove biological improvement. Do not "
                    "invent BUSCO, k-mer completeness or accuracy measurements. Report "
                    "missing evidence explicitly. Never repeat attempted parameters. "
                    "When the attempt budget is exhausted, choose an existing result, "
                    "keep_source, or human_review. Actions pass through the configured "
                    "human/foreground-countdown review policy. "
                    "Return one JSON object matching this response schema: "
                    + json.dumps(PurgeRecommendation.model_json_schema())
                ),
            },
            {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
        ]
