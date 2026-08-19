from __future__ import annotations

import dataclasses

import pytest

from app.control.lifecycle import inspect_state
from app.control.projections import (
    ProgressProjectionInput,
    ProjectionBoundary,
    progress_projection_input,
    reduce_workflow_progress,
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


def test_inspect_state_does_not_treat_checkpoint_cursor_as_workflow_progress() -> None:
    """Detached checkpoint inspection must not guess prior terminal facts."""

    state = initial_state(
        session_id="s1",
        input_manifest_ref="manifest.yaml",
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-001-read_qc",
                "stage": "read_qc",
                "allow_local_execution": False,
            },
        ],
    )
    # This is exactly the stale-checkpoint shape that used to be rendered as
    # "read_qc is current" merely because the cursor had advanced.
    state["workflow_step_index"] = 1
    state["current_stage"] = "read_qc"

    summary = inspect_state(state)

    assert summary["checkpoint_current_stage"] == "read_qc"
    assert summary["current_workflow_step_id"] is None
    assert summary["current_stage"] is None
    assert summary["workflow_progress"]["plan_source"] == "checkpoint_only"
    assert (
        summary["workflow_progress"]["projection_authority"]
        == "degraded_stage_terminal_projection"
    )
    assert summary["workflow_progress"]["integrity_diagnostics"][0]["kind"] == (
        "checkpoint_only_stage_terminal_prefix"
    )


def test_failed_progress_scope_controls_authority_requirements() -> None:
    steps = [
        {
            "step_id": "step-000-inventory",
            "stage": "inventory",
            "allow_local_execution": False,
        },
        {
            "step_id": "step-001-read_qc",
            "stage": "read_qc",
            "allow_local_execution": False,
        },
    ]
    boundary = reduce_workflow_progress(
        steps,
        stage_terminals={
            ("step-000-inventory", "inventory"): {"event_type": "stage_completed"}
        },
        session_outcome="failed",
        failure_scope="controller_boundary",
    )
    assert boundary["projection_authority"] == "authoritative"

    occurrence = reduce_workflow_progress(
        steps,
        stage_terminals={},
        session_outcome="failed",
        failure_scope="occurrence",
    )
    assert occurrence["projection_authority"] == "degraded_stage_terminal_projection"
    assert occurrence["integrity_diagnostics"][-1]["kind"] == (
        "failed_session_has_no_failed_occurrence"
    )

    legacy = reduce_workflow_progress(
        steps,
        stage_terminals={},
        session_outcome="failed",
    )
    assert legacy["projection_authority"] == "degraded_stage_terminal_projection"
    assert legacy["integrity_diagnostics"][-1]["kind"] == (
        "legacy_failed_session_missing_failure_scope"
    )


def test_attached_failed_occurrence_projects_scope_from_durable_terminal() -> None:
    state = initial_state(
        session_id="s1",
        input_manifest_ref="manifest.yaml",
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-001-read_qc",
                "stage": "read_qc",
                "allow_local_execution": False,
            },
        ],
    )
    state["workflow_step_index"] = 1
    state["entered_workflow_step_index"] = 1
    state["current_stage"] = "read_qc"
    state["supervision_state"] = "failed_terminal"
    state["workflow_step_outcome"] = {
        "workflow_step_id": "step-001-read_qc",
        "stage": "read_qc",
        "outcome": "failed",
        "reason": "preflight failed",
    }

    projected = progress_projection_input(
        state,
        stage_terminals={
            ("step-000-inventory", "inventory"): {"event_type": "stage_completed"},
            ("step-001-read_qc", "read_qc"): {"event_type": "stage_failed"},
        },
    )

    assert projected.projection_authority == "authoritative"
    assert projected.completed_workflow_steps == 1
    assert projected.failed_workflow_steps == 1
    assert projected.workflow_step_id is None
