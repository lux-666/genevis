"""Planner contracts shared by stages."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AssemblyCandidatePlan(BaseModel):
    """Candidate selection plan for assembly before tool execution."""

    method_name: str
    status: Literal["compatible", "rejected", "selected"]
    role: Literal["candidate", "primary", "fallback"] = "candidate"
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    requires_genome_size: bool = False
    rejection_reason: str | None = None
    candidate_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyReviewPlan(BaseModel):
    """Plan for assembly review evaluation signals and candidate decisions.

    Thresholds are not stored here; they are human-approved policy.
    """

    stage: str = "assembly_review"
    status: Literal["planned", "evaluating", "blocked_human", "completed", "failed"]
    candidate_ids: list[str] = Field(default_factory=list)
    planned_signals: list[str] = Field(default_factory=list)
    available_signals: list[str] = Field(default_factory=list)
    missing_signals: list[str] = Field(default_factory=list)
    decision: Literal[
        "pending",
        "accept",
        "rerun",
        "purge",
        "continue_low_confidence",
        "stop_human_review",
    ] = "pending"
    selected_candidate_id: str | None = None
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
