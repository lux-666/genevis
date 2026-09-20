"""Strict compact cursor for the HiFi polishing lifecycle."""

from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import update_validated_stage_output, validated_stage_output


@with_config(ConfigDict(extra="forbid"))
class BasePolishStageCursor(TypedDict, total=False):
    status: str
    reason: str
    failure_kind: str
    execution_mode: str
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    method_name: str
    threads: int
    review_policy: dict[str, Any]
    execution_attempts: list[dict[str, Any]]
    input_record_ref: ArtifactRefJson
    source_candidate_ref: ArtifactRefJson
    output_ref: ArtifactRefJson
    comparison_ref: ArtifactRefJson
    polished_candidate_ref: ArtifactRefJson
    candidate_ref: ArtifactRefJson
    decision_ref: ArtifactRefJson
    active_interrupt: dict[str, Any] | None


BASE_POLISH_STAGE_CURSOR_ADAPTER = TypeAdapter(BasePolishStageCursor)


def read_base_polish_stage_output(state: TopLevelState) -> BasePolishStageCursor:
    return validated_stage_output(
        state, "base_polish", BASE_POLISH_STAGE_CURSOR_ADAPTER
    )


def update_base_polish_stage_output(
    state: TopLevelState, payload: Mapping[str, object]
) -> StateUpdate:
    return update_validated_stage_output(
        state, "base_polish", payload, BASE_POLISH_STAGE_CURSOR_ADAPTER
    )
