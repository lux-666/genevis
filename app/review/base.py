"""Review result contracts and shared helpers.

Thresholds and policies are not defined here.  This module holds the shape
of review signals and decisions so that stages can reference them without
inventing policy.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ReviewDecision = Literal[
    "pending",
    "accept",
    "rerun",
    "purge",
    "continue_low_confidence",
    "stop_human_review",
]
ReviewResumeStage = Literal["assembly_review", "hic_scaffolding"]
ResumeReviewDecision = Literal[
    "accept",
    "purge",
    "continue_low_confidence",
    "stop_human_review",
    "select_method",
    "skip",
    "rerun",
]


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
    decisions_allowed: list[ReviewDecision] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


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
