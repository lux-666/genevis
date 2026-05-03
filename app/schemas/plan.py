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
