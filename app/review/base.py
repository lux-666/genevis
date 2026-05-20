"""Review result contracts and shared helpers.

Thresholds and policies are not defined here.  This module holds the shape
of review signals and decisions so that stages can reference them without
inventing policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ReviewDecision = Literal[
    "pending",
    "accept",
    "pass",
    "rerun",
    "purge",
    "polish",
    "continue_low_confidence",
    "stop_human_review",
]
ReviewResumeStage = Literal["assembly_review", "hic_scaffolding"]
ResumeReviewDecision = Literal[
    "accept",
    "pass",
    "purge",
    "polish",
    "continue_low_confidence",
    "stop_human_review",
    "select_method",
    "skip",
    "rerun",
]
ReviewDecisionOrigin = Literal["human", "llm_provisional", "auto_rule"]


class ReviewSignal(BaseModel):
    """One review signal collected or referenced for a candidate."""

    signal_name: str
    method_name: str
    candidate_id: str | None = None
    status: Literal["pending", "available", "missing", "failed"] = "pending"
    metric_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewPayload(BaseModel):
    """Payload for a human review interrupt."""

    stage: str
    reason: str
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    signals: list[ReviewSignal] = Field(default_factory=list)
    decisions_allowed: list[ResumeReviewDecision] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    auto_decidable: bool = True
    report_refs: dict[str, str] = Field(default_factory=dict)
    candidate_comparison_context: dict[str, Any] = Field(default_factory=dict)


class ResumeReviewRequest(BaseModel):
    """Structured human decision used to resume from a review gate."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    stage: ReviewResumeStage
    decision: ResumeReviewDecision
    candidate_id: str | None = None
    method_name: str | None = None
    final_assembly_ref: str | None = None
    rejected_candidates: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    decided_by: ReviewDecisionOrigin = "human"
    rationale_artifact_ref: str | None = None

    @model_validator(mode="after")
    def _require_rationale_for_non_human(self) -> "ResumeReviewRequest":
        if self.decided_by != "human" and not self.rationale_artifact_ref:
            raise ValueError("rationale_artifact_ref is required when decided_by is not human")
        return self


def load_resume_review_request(path: str | Path) -> ResumeReviewRequest:
    """Load and validate a JSON decision file.

    Raises ``FileNotFoundError`` when the file is missing and
    ``ValueError`` when the file is unreadable, not valid JSON, or fails
    schema validation. The CLI should call this before mutating
    checkpoint, artifacts, or ledger.
    """

    decision_path = Path(path)
    if not decision_path.exists():
        raise FileNotFoundError(f"decision file not found: {decision_path}")
    try:
        text = decision_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"decision file is not readable: {decision_path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"decision file is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("decision file must be a JSON object at the top level")
    try:
        return ResumeReviewRequest.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"decision file failed validation: {exc}") from exc
