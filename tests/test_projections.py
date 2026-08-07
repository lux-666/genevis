from __future__ import annotations

import dataclasses

import pytest

from app.control.projections import (
    ProgressProjectionInput,
    ProjectionBoundary,
    progress_projection_input,
)
from app.schemas.state import initial_state


@pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError("closed"),
        OSError("unavailable"),
        ValueError("bad render"),
        RuntimeError("renderer bug"),
    ],
)
def test_projection_boundary_absorbs_exception_subclasses(error: Exception) -> None:
    boundary = ProjectionBoundary()

    result = boundary.run(
        "progress",
        lambda _value: (_ for _ in ()).throw(error),
        "input",
    )

    assert result.completed is False
    assert result.warning is not None
    assert result.warning.name == "progress"
    assert result.warning.error_type == type(error).__name__
    assert len(result.warning.message) <= 512
    assert boundary.warnings == (result.warning,)


def test_projection_boundary_does_not_absorb_base_exception() -> None:
    boundary = ProjectionBoundary()

    with pytest.raises(KeyboardInterrupt):
        boundary.run(
            "progress",
            lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        )


def test_progress_sink_is_disabled_after_its_first_failure() -> None:
    boundary = ProjectionBoundary()
    calls = 0

    def fail() -> None:
        nonlocal calls
        calls += 1
        raise BrokenPipeError("closed")

    first = boundary.run("progress", fail, disable_after_failure=True)
    second = boundary.run("progress", fail, disable_after_failure=True)

    assert first.warning is not None
    assert second.skipped is True
    assert second.warning is None
    assert calls == 1
    assert len(boundary.warnings) == 1


def test_projection_arguments_are_defensive_copies() -> None:
    boundary = ProjectionBoundary()
    state = {"nested": {"items": ["original"]}}

    def mutate(copy_of_state: dict) -> None:
        copy_of_state["nested"]["items"].append("mutated")

    result = boundary.run("report", mutate, state)

    assert result.completed is True
    assert state == {"nested": {"items": ["original"]}}


def test_progress_projection_input_is_frozen_and_contains_no_mutable_state() -> None:
    state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    state["active_interrupt"] = {
        "stage": "inventory",
        "workflow_step_id": "step-000-inventory",
        "gate_id": "inventory_review",
        "review_request_id": "review_001",
    }

    projected = progress_projection_input(state)

    assert isinstance(projected, ProgressProjectionInput)
    assert projected.session_id == "s1"
    assert projected.workflow_step_id == "step-000-inventory"
    assert projected.review_request_id == "review_001"
    assert all(
        not isinstance(value, (dict, list, set))
        for value in dataclasses.asdict(projected).values()
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        projected.current_stage = "done"  # type: ignore[misc]
