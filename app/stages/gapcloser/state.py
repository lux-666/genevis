"""Strict checkpoint boundary for the GapCloser placeholder cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import update_validated_stage_output, validated_stage_output


@with_config(ConfigDict(extra="forbid"))
class GapCloserStageCursor(TypedDict):
    """Only the explicit not-implemented boundary may occupy this cursor."""

    status: Literal["blocked_not_implemented"]
    reason: Annotated[str, Field(min_length=1)]


GAPCLOSER_STAGE_CURSOR_ADAPTER = TypeAdapter(GapCloserStageCursor)


def read_gapcloser_stage_output(state: TopLevelState) -> GapCloserStageCursor:
    """Validate and narrow the GapCloser cursor at its read boundary."""

    return validated_stage_output(
        state,
        "gapcloser",
        GAPCLOSER_STAGE_CURSOR_ADAPTER,
    )


def update_gapcloser_stage_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge and strictly validate the GapCloser cursor."""

    return update_validated_stage_output(
        state,
        "gapcloser",
        payload,
        GAPCLOSER_STAGE_CURSOR_ADAPTER,
    )
