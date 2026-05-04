"""Planner contracts shared by stages."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MethodCandidate(BaseModel):
    """One selectable method route inside a stage."""

    method_name: str
    route: str
    reason: str
    required_assets: list[str] = Field(default_factory=list)
    optional_assets: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StagePlan(BaseModel):
    """A small plan referenceable from graph state or ledger."""

    stage: str
    status: Literal["planned", "skipped", "completed", "failed"] = "planned"
    candidates: list[MethodCandidate] = Field(default_factory=list)
    selected_method: str | None = None
    skip_reason: str | None = None
    review_required: bool = False
    depends_on: list[str] = Field(default_factory=list)


class StageSubstepPlan(BaseModel):
    """A compact plan record for stage-internal substeps."""

    name: str
    status: Literal["planned", "skipped", "executed", "failed", "blocked_human"]
    method_name: str | None = None
    skip_reason: str | None = None
    artifact_ref: str | None = None
    thresholds: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
