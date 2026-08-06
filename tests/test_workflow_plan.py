"""Contracts for the frozen, occurrence-addressed workflow plan."""

from pathlib import Path

import pytest

from app.control.workflow_plan import (
    advance_workflow_step,
    bind_attempt_to_current_workflow_step,
    seed_current_workflow_step,
    workflow_steps_from_run_config,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.run_config import RunConfig
from app.schemas.state import (
    freeze_workflow_steps,
    initial_state,
    validate_workflow_steps,
)
from app.storage.paths import SessionPaths


def _apply(state: dict, update: dict) -> None:
    state.update(update)


def _attempt() -> FrozenAttemptSpec:
    return FrozenAttemptSpec(
        attempt_id="s1:attempt-001",
        session_id="s1",
        stage="t2t",
        method_name="t2t.test",
        script_path=Path("/tmp/run.sh"),
        script_sha256="0" * 64,
        working_dir=Path("/tmp"),
        expected_outputs=[],
        input_dependencies=[],
    )


def test_repeated_stages_keep_order_reset_cursor_and_scope_local_consent() -> None:
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "t2t", "allow_local_execution": False},
                {"stage": "assembly_review", "allow_local_execution": False},
                {"stage": "t2t", "allow_local_execution": True},
                {"stage": "assembly_review", "allow_local_execution": False},
            ]
        }
    )
    steps = workflow_steps_from_run_config(config)
    assert [step["stage"] for step in steps] == [
        "inventory",
        "t2t",
        "assembly_review",
        "t2t",
        "assembly_review",
    ]
    assert [step["step_id"] for step in steps] == [
        "step-000-inventory",
        "step-001-t2t",
        "step-002-assembly_review",
        "step-003-t2t",
        "step-004-assembly_review",
    ]
    assert steps[2]["review_target"] == {
        "resource_type": "assembly",
        "producer_step_id": "step-001-t2t",
    }
    assert steps[4]["review_target"] == {
        "resource_type": "assembly",
        "producer_step_id": "step-003-t2t",
    }

    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/input_manifest.yaml",
        workflow_steps=steps,
        stage_seeds={"t2t": {"status": "seeded"}},
    )

    _apply(state, seed_current_workflow_step(state))
    _apply(state, advance_workflow_step(state, "inventory"))
    _apply(state, seed_current_workflow_step(state))
    assert state["stage_outputs"]["t2t"] == {"status": "seeded"}
    first_attempt = bind_attempt_to_current_workflow_step(state, _attempt())

    state["stage_outputs"]["t2t"] = {"status": "first-completed"}
    _apply(state, advance_workflow_step(state, "t2t"))
    _apply(state, seed_current_workflow_step(state))
    _apply(state, advance_workflow_step(state, "assembly_review"))
    _apply(state, seed_current_workflow_step(state))

    assert state["stage_outputs"]["t2t"] == {
        "status": "seeded",
        "execution_mode": "local",
    }
    second_attempt = bind_attempt_to_current_workflow_step(state, _attempt())
    assert first_attempt.attempt_id != second_attempt.attempt_id
    assert first_attempt.metadata["workflow_step_id"] == "step-001-t2t"
    assert second_attempt.metadata["workflow_step_id"] == "step-003-t2t"

    paths = SessionPaths("/tmp/gene-agent-data", "s1")
    assert paths.workflow_step_work_dir(
        "step-001-t2t", "t2t"
    ) != paths.workflow_step_work_dir("step-003-t2t", "t2t")
    assert paths.workflow_step_artifacts_dir(
        "step-001-t2t", "t2t"
    ) != paths.workflow_step_artifacts_dir("step-003-t2t", "t2t")


def test_frozen_workflow_plan_rejects_runtime_append_or_reorder() -> None:
    original = workflow_steps_from_run_config(
        RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "t2t", "allow_local_execution": False},
                    {
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                    },
                ]
            }
        )
    )
    reordered = workflow_steps_from_run_config(
        RunConfig.model_validate(
            {
                "requested_stages": [
                    {
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                    },
                    {"stage": "t2t", "allow_local_execution": False},
                    {
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                    },
                ]
            }
        )
    )
    appended = workflow_steps_from_run_config(
        RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "t2t", "allow_local_execution": False},
                    {
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                    },
                    {"stage": "t2t", "allow_local_execution": False},
                    {
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                    },
                ]
            }
        )
    )

    assert freeze_workflow_steps(None, original) == original
    assert freeze_workflow_steps([], original) == original
    with pytest.raises(ValueError, match="immutable"):
        freeze_workflow_steps(original, reordered)
    with pytest.raises(ValueError, match="immutable"):
        freeze_workflow_steps(original, appended)


def test_review_target_binds_the_nearest_assembly_handoff_occurrence() -> None:
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "assembly", "allow_local_execution": False},
                {"stage": "assembly_dedup", "allow_local_execution": False},
                {"stage": "assembly_review", "allow_local_execution": False},
                {"stage": "hic_scaffolding", "allow_local_execution": False},
                {"stage": "t2t", "allow_local_execution": False},
                {"stage": "assembly_review", "allow_local_execution": False},
            ]
        }
    )

    steps = workflow_steps_from_run_config(config)

    assert steps[3]["review_target"] == {
        "resource_type": "assembly",
        "producer_step_id": "step-002-assembly_dedup",
    }
    assert steps[6]["review_target"] == {
        "resource_type": "assembly",
        "producer_step_id": "step-005-t2t",
    }


def test_workflow_step_validation_rejects_bad_review_targets() -> None:
    steps = workflow_steps_from_run_config(
        RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "t2t", "allow_local_execution": False},
                    {"stage": "assembly_review", "allow_local_execution": False},
                ]
            }
        )
    )

    missing = [dict(step) for step in steps]
    missing[2].pop("review_target")
    with pytest.raises(ValueError, match="review_target is required"):
        validate_workflow_steps(missing)

    forbidden = [dict(step) for step in steps]
    forbidden[1]["review_target"] = {
        "resource_type": "assembly",
        "producer_step_id": "step-000-inventory",
    }
    with pytest.raises(ValueError, match="allowed only"):
        validate_workflow_steps(forbidden)

    forged = [dict(step) for step in steps]
    forged[2]["review_target"] = {
        "resource_type": "assembly",
        "producer_step_id": "step-000-inventory",
    }
    with pytest.raises(ValueError, match="must be 'step-001-t2t'"):
        validate_workflow_steps(forged)


def test_workflow_step_validation_rejects_terminal_placeholder_before_review() -> None:
    steps = [
        {
            "step_id": "step-000-inventory",
            "stage": "inventory",
            "allow_local_execution": False,
        },
        {
            "step_id": "step-001-t2t",
            "stage": "t2t",
            "allow_local_execution": False,
        },
        {
            "step_id": "step-002-base_polish",
            "stage": "base_polish",
            "allow_local_execution": False,
        },
        {
            "step_id": "step-003-assembly_review",
            "stage": "assembly_review",
            "allow_local_execution": False,
            "review_target": {
                "resource_type": "assembly",
                "producer_step_id": "step-001-t2t",
            },
        },
    ]

    with pytest.raises(ValueError, match="before base_polish"):
        validate_workflow_steps(steps)


def test_prebound_attempt_still_receives_canonical_occurrence_namespace() -> None:
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "t2t", "allow_local_execution": True},
                {"stage": "assembly_review", "allow_local_execution": False},
            ]
        }
    )
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/input_manifest.yaml",
        workflow_steps=workflow_steps_from_run_config(config),
    )
    _apply(state, seed_current_workflow_step(state))
    _apply(state, advance_workflow_step(state, "inventory"))
    _apply(state, seed_current_workflow_step(state))

    prebound = _attempt().model_copy(
        update={"metadata": {"workflow_step_id": "step-001-t2t"}}
    )
    bound = bind_attempt_to_current_workflow_step(state, prebound)

    assert bound.attempt_id == "s1:step-001-t2t:attempt-001"
    assert bound.metadata["workflow_step_id"] == "step-001-t2t"
    assert bound.metadata["workflow_step_index"] == 1


def test_prebound_attempt_rejects_another_occurrence_namespace() -> None:
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "t2t", "allow_local_execution": True},
                {"stage": "assembly_review", "allow_local_execution": False},
            ]
        }
    )
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/input_manifest.yaml",
        workflow_steps=workflow_steps_from_run_config(config),
    )
    _apply(state, seed_current_workflow_step(state))
    _apply(state, advance_workflow_step(state, "inventory"))
    _apply(state, seed_current_workflow_step(state))

    prebound = _attempt().model_copy(
        update={
            "attempt_id": "s1:step-999-t2t:attempt-001",
            "metadata": {"workflow_step_id": "step-001-t2t"},
        }
    )
    with pytest.raises(ValueError, match="another workflow step"):
        bind_attempt_to_current_workflow_step(state, prebound)
