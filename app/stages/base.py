"""Shared helpers for placeholder stage subgraphs."""

from __future__ import annotations

from pathlib import Path

from app.graph.routing import next_stage_name
from app.schemas.state import TopLevelState


def absolute_data_dir(state: TopLevelState) -> Path:
    """Return data_dir as a resolved absolute path.

    The runner sets cwd to the attempt working directory, which is nested
    under data_dir. A relative data_dir interpolated into a generated shell
    script therefore resolves from inside that workdir and points at a
    non-existent nested location. Resolve the root once before scripts are
    generated so local and cloud runs use stable absolute paths.
    """
    return Path(state.get("data_dir", "data")).expanduser().resolve(strict=False)


def enter_stage(stage: str, state: TopLevelState) -> dict:
    """Mark a stage as running without external side effects."""
    return {"supervision_state": "running_stage"}


def plan_stage(stage: str, state: TopLevelState) -> dict:
    """Create a minimal stage output placeholder."""
    outputs = dict(state.get("stage_outputs") or {})
    outputs.setdefault(stage, {"status": "planned", "methods": []})
    return {"stage_outputs": outputs}


def commit_stage(stage: str, state: TopLevelState) -> dict:
    """Advance to the next top-level stage."""
    trace = [*state.get("route_trace", []), f"{stage}_stage"]
    next_stage = next_stage_name(stage)
    return {
        "current_stage": next_stage,
        "supervision_state": "completed" if next_stage == "done" else "running_stage",
        "route_trace": trace,
    }
