"""Strict compact cursor for the HiFi gap-closing lifecycle."""

from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import update_validated_stage_output, validated_stage_output
from app.stages.finishing.state import FinishingCursor


@with_config(ConfigDict(extra="forbid"))
class GapCloserStageCursor(TypedDict, total=False):
    recommendation_mode: str
    recommendation_output_method: str
    max_attempts: int
    finishing: FinishingCursor

    status: str
    reason: str
    failure_kind: str
    execution_mode: str
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    method_name: str
    threads: int
    min_identity: float
    min_match: int
    gap_size_check: bool
    review_policy: dict[str, Any]
    execution_attempts: list[dict[str, Any]]
    input_record_ref: ArtifactRefJson
    source_candidate_ref: ArtifactRefJson
    output_ref: ArtifactRefJson
    details_ref: ArtifactRefJson
    validation_ref: ArtifactRefJson
    candidate_ref: ArtifactRefJson
    decision_ref: ArtifactRefJson
    active_interrupt: dict[str, Any] | None


GAPCLOSER_STAGE_CURSOR_ADAPTER = TypeAdapter(GapCloserStageCursor)


def read_gapcloser_stage_output(state: TopLevelState) -> GapCloserStageCursor:
    return validated_stage_output(state, "gapcloser", GAPCLOSER_STAGE_CURSOR_ADAPTER)


def update_gapcloser_stage_output(
    state: TopLevelState, payload: Mapping[str, object]
) -> StateUpdate:
    return update_validated_stage_output(
        state, "gapcloser", payload, GAPCLOSER_STAGE_CURSOR_ADAPTER
    )
