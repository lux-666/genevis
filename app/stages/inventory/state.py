"""Typed checkpoint boundary for the inventory stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)


InventoryStatus = Literal[
    "validated",
    "completed",
    "blocked_human",
    "failed",
    "blocked_env",
    "missing_tool",
]


@with_config(ConfigDict(extra="forbid"))
class InventoryStageCursor(TypedDict, total=False):
    """All JSON-only fields owned by ``stage_outputs.inventory``."""

    status: InventoryStatus
    reason: str
    valid: bool
    errors: list[str]
    warnings: list[str]
    asset_count: int
    entry_mode: Literal["raw_inputs", "existing_assembly"]
    genome_size_estimate: int | None
    target_assembly_level: Literal["contig", "scaffold", "chromosome"]
    registered_assets: list[str]
    review_request_artifact_ref: ArtifactRefJson
    review_decision_artifact_ref: ArtifactRefJson


INVENTORY_STAGE_CURSOR_ADAPTER = TypeAdapter(InventoryStageCursor)


def read_inventory_stage_output(state: TopLevelState) -> InventoryStageCursor:
    """Validate and narrow the inventory cursor at its read boundary."""

    return validated_stage_output(
        state,
        "inventory",
        INVENTORY_STAGE_CURSOR_ADAPTER,
    )


def update_inventory_stage_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Validate and serialize one inventory cursor update."""

    return update_validated_stage_output(
        state,
        "inventory",
        payload,
        INVENTORY_STAGE_CURSOR_ADAPTER,
    )
