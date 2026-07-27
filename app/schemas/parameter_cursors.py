"""Shared TypedDict cursors for parameter-recommendation workflows.

These definitions are used identically by hic_scaffolding and t2t stage
cursors.  Keeping them here avoids duplicate definitions in each state.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.artifacts import (
    ParameterAttemptStatus,
    ParameterAttemptTask,
    ParameterRecommendationStatus,
)


@with_config(ConfigDict(extra="forbid"))
class ParameterAttemptJson(TypedDict, total=False):
    """JSON representation of a compact parameter attempt cursor."""

    schema_version: Literal[3]
    attempt_id: int
    method_id: str
    status: ParameterAttemptStatus
    task: ParameterAttemptTask
    previous_attempt_id: int | None
    recommendation_manifest_ref: ArtifactRefJson
    approval_decision_ref: ArtifactRefJson
    frozen_parameters_ref: ArtifactRefJson | None
    command_ref: ArtifactRefJson | None
    frozen_attempt_ref: ArtifactRefJson | None
    execution_ref: ArtifactRefJson | None
    review_ref: ArtifactRefJson | None
    attempt_manifest_ref: ArtifactRefJson | None
    recommendation_contract: Literal["parameter_recommendation_v1"]
    previous_attempt_seal_hash: str | None
    sealed_at: str | None
    seal_hash: str | None


@with_config(ConfigDict(extra="forbid"))
class ParameterRecommendationJson(TypedDict):
    """JSON representation of the active recommendation cursor."""

    schema_version: Literal[3]
    recommendation_id: int
    method_id: str
    task: ParameterAttemptTask
    status: ParameterRecommendationStatus
    previous_attempt_id: int | None
    manifest_ref: ArtifactRefJson
    recommendation_hash: str
    review_request_id: str | None
    review_request_ref: ArtifactRefJson | None
    previous_attempt_seal_hash: str | None
