"""Shared utilities for stage nodes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import TypeAdapter

from app.control.workflow_plan import current_workflow_step_id
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import (
    JsonValue,
    StateUpdate,
    TopLevelState,
    stage_output,
    update_stage_output,
)
from app.storage.asset_registry import load_asset_records
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
    """Return a path below this occurrence's ``artifacts/steps/<id>/`` root."""

    step_id = current_workflow_step_id(state, expected_stage=stage)
    return session_paths(state).workflow_step_artifact_path(step_id, stage, *parts)


def stage_work_dir(state: TopLevelState, stage: str, *parts: str) -> Path:
    """Return a path below this occurrence's ``work/steps/<id>/`` root."""

    step_id = current_workflow_step_id(state, expected_stage=stage)
    return session_paths(state).workflow_step_work_path(step_id, stage, *parts)


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


_ValidatedCursorT = TypeVar("_ValidatedCursorT")


def validated_stage_output(
    state: TopLevelState,
    stage_name: str,
    adapter: TypeAdapter[_ValidatedCursorT],
) -> _ValidatedCursorT:
    """Validate and narrow one generic JSON cursor at its stage boundary."""

    return adapter.validate_python(stage_output(state, stage_name))


def update_validated_stage_output(
    state: TopLevelState,
    stage_name: str,
    payload: Mapping[str, object],
    adapter: TypeAdapter[_ValidatedCursorT],
) -> StateUpdate:
    """Merge and validate a typed stage cursor before checkpoint serialization."""

    merged: dict[str, object] = {
        **stage_output(state, stage_name),
        **payload,
    }
    validated = adapter.validate_python(merged)
    serialized = adapter.dump_python(validated, mode="json")
    if not isinstance(serialized, dict):
        raise TypeError(f"{stage_name} stage cursor must serialize to a JSON object")
    return update_stage_output(
        state,
        stage_name,
        cast(dict[str, JsonValue], serialized),
    )


def append_route_trace(state: TopLevelState, *items: str) -> list[str]:
    """Append step-qualified route items with bounded checkpoint history."""

    workflow_step_id = current_workflow_step_id(state)
    return [
        *list(state.get("route_trace") or []),
        *(f"{workflow_step_id}:{item}" for item in items),
    ][-ROUTE_TRACE_LIMIT:]


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
    if not state.get("asset_registry_ref"):
        return []
    return [
        record.model_dump(mode="json")
        for record in load_asset_records(state, set(asset_types))
    ]
