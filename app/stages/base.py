"""Shared utilities for stage nodes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import (
    DOMAIN_STAGES,
    DomainStage,
    JsonValue,
    StageCursor,
    TopLevelState,
    validate_stage_outputs,
)
from app.storage.paths import SessionPaths


def ordered_unique(values: list[str]) -> list[str]:
    """Return strings in first-seen order with duplicates removed."""

    return list(dict.fromkeys(values))


def absolute_data_dir(state: TopLevelState) -> Path:
    """Return data_dir as a resolved absolute path.

    The runner sets cwd to the attempt working directory, which is nested
    under data_dir. A relative data_dir interpolated into a generated shell
    script therefore resolves from inside that workdir and points at a
    non-existent nested location. Resolve the root once before scripts are
    generated so local and cloud runs use stable absolute paths.
    """
    return Path(state.get("data_dir", "data")).expanduser().resolve(strict=False)


def session_paths(state: TopLevelState) -> SessionPaths:
    """Return the validated v2 path owner for the current run.

    Runtime state carries the run root, not the shared ``data/`` root.  Keeping
    this conversion here prevents stage code from reconstructing storage roots
    or appending the session identifier below an already session-scoped path.
    """

    paths = SessionPaths.from_run_dir(absolute_data_dir(state))
    paths.require_session_id(state["session_id"])
    return paths


def stage_artifacts_dir(state: TopLevelState, stage: str, *parts: str) -> Path:
    """Return a path below ``artifacts/<stage>/`` for this run."""

    return session_paths(state).artifact_path(stage, *parts)


def stage_work_dir(state: TopLevelState, stage: str, *parts: str) -> Path:
    """Return a path below ``work/<stage>/`` for this run."""

    return session_paths(state).work_path(stage, *parts)


def resolve_run_ref(state: TopLevelState, path_ref: str | Path) -> Path:
    """Resolve a durable run-relative ref without accepting path traversal."""

    path = Path(path_ref)
    if path.is_absolute():
        # External biological inputs remain absolute execution dependencies;
        # internal durable refs are always run-relative and take this branch.
        return path.resolve(strict=False)
    return session_paths(state).resolve_ref(path.as_posix())


# ============================================================================
# Stage output read/write helpers
# ============================================================================


def stage_output(state: TopLevelState, stage_name: str) -> StageCursor:
    """Return a mutable copy of a stage's slot in stage_outputs."""
    if stage_name not in DOMAIN_STAGES:
        raise ValueError(f"unknown stage output slot: {stage_name}")
    stage = cast(DomainStage, stage_name)
    return dict((state.get("stage_outputs") or {}).get(stage, {}))


def update_stage_output(
    state: TopLevelState, stage_name: str, payload: dict[str, JsonValue]
) -> dict[str, object]:
    """Merge payload into a stage's output slot and return the state delta."""
    outputs = dict(state.get("stage_outputs") or {})
    outputs[cast(DomainStage, stage_name)] = {
        **stage_output(state, stage_name),
        **payload,
    }
    return {"stage_outputs": validate_stage_outputs(outputs)}


def append_route_trace(state: TopLevelState, *items: str) -> list[str]:
    """Append route items while retaining only the newest bounded history."""

    return [*list(state.get("route_trace") or []), *items][-ROUTE_TRACE_LIMIT:]


def load_assets_from_registry(
    state: TopLevelState,
    asset_types: list[str],
) -> list[dict[str, Any]]:
    """
    Load assets from registry by type.

    Helper for reducing asset registry query boilerplate.
    Handles JSON parsing errors gracefully.

    Args:
        state: TopLevelState
        asset_types: List of asset_type values to match

    Returns:
        List of matching assets, or empty list if not found/error
    """
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return []

    registry_path = Path(str(registry_ref))
    if not registry_path.exists():
        return []

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        assets = registry.get("assets", [])
        return [asset for asset in assets if asset.get("asset_type") in asset_types]
    except (OSError, json.JSONDecodeError):
        return []
