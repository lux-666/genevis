"""Strict checkpoint boundary for the Base Polish placeholder cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import update_validated_stage_output, validated_stage_output


@with_config(ConfigDict(extra="forbid"))
class BasePolishStageCursor(TypedDict):
    """Only the explicit not-implemented boundary may occupy this cursor."""

    status: Literal["blocked_not_implemented"]
    reason: Annotated[str, Field(min_length=1)]


BASE_POLISH_STAGE_CURSOR_ADAPTER = TypeAdapter(BasePolishStageCursor)


def read_base_polish_stage_output(state: TopLevelState) -> BasePolishStageCursor:
    """Validate and narrow the Base Polish cursor at its read boundary."""

    return validated_stage_output(
        state,
        "base_polish",
        BASE_POLISH_STAGE_CURSOR_ADAPTER,
    )


def update_base_polish_stage_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge and strictly validate the Base Polish cursor."""

    return update_validated_stage_output(
        state,
        "base_polish",
        payload,
        BASE_POLISH_STAGE_CURSOR_ADAPTER,
    )
