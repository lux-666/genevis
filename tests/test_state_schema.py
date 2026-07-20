from typing import get_args, get_type_hints

import pytest

from app.schemas.state import (
    ActiveInterrupt,
    CheckpointBudgetWarning,
    ControlState,
    DomainStage,
    StageOutputs,
    SupervisionState,
    TopLevelState,
    initial_state,
    validate_stage_outputs,
    validate_state_field,
    validate_top_level_state,
)


def test_top_level_state_marks_runtime_invariants_as_required() -> None:
    assert TopLevelState.__required_keys__ == {
        "session_id",
        "data_dir",
        "control_state",
        "supervision_state",
        "current_stage",
        "input_manifest_ref",
        "review_notes",
        "route_trace",
        "stage_outputs",
        "skip_reasons",
    }
    assert {
        "thread_id",
        "requested_stages",
        "asset_registry_ref",
        "stage_plan_ref",
        "active_interrupt",
        "shared_parameters",
        "last_error",
    } <= TopLevelState.__optional_keys__


def test_initial_state_populates_all_declared_top_level_fields() -> None:
    state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")

    assert set(state) == set(TopLevelState.__annotations__)
    assert TopLevelState.__required_keys__ <= state.keys()


def test_lifecycle_literals_and_interrupt_contract_are_bounded() -> None:
    assert set(get_args(ControlState)) == {
        "running",
        "paused_manual",
        "stop_requested",
        "closed",
    }
    assert set(get_args(SupervisionState)) == {
        "idle",
        "running_stage",
        "reviewing",
        "blocked_human",
        "completed",
        "failed_terminal",
    }
    assert "done" in get_args(DomainStage)

    hints = get_type_hints(ActiveInterrupt)
    assert hints["stage"] is DomainStage
    assert ActiveInterrupt.__required_keys__ == {"stage"}
    assert {
        "gate_id",
        "reason",
        "blocked_at",
        "review_request_id",
        "request_ref",
        "request_hash",
    } <= ActiveInterrupt.__optional_keys__
    assert "decisions_allowed" not in ActiveInterrupt.__optional_keys__


def test_stage_outputs_are_typed_bounded_json_cursors() -> None:
    assert "Any" not in str(StageOutputs)
    assert validate_stage_outputs(
        {
            "assembly": {
                "status": "frozen",
                "frozen_attempt_ref": {
                    "schema_version": 2,
                    "path_ref": "artifacts/assembly/attempts/a/attempt.json",
                },
            }
        }
    )["assembly"]["status"] == "frozen"

    with pytest.raises(ValueError, match="unknown stages"):
        validate_stage_outputs({"not_a_stage": {}})
    with pytest.raises(ValueError, match="unknown stages"):
        validate_stage_outputs({"rnaseq": {"status": "available"}})
    with pytest.raises(TypeError, match="non-JSON value"):
        validate_stage_outputs({"assembly": {"path": object()}})
    with pytest.raises(ValueError, match="exceeds"):
        validate_stage_outputs({"assembly": {"payload": "x" * (64 * 1024)}})


def test_stage_outputs_reject_embedded_frozen_attempt_before_checkpoint() -> None:
    from langgraph.graph import StateGraph

    frozen_body = {
        "attempt_id": "s1:assembly:a",
        "session_id": "s1",
        "stage": "assembly",
        "method_name": "assembly.candidate.flye",
        "script_path": "/run/work/assembly/run.sh",
        "working_dir": "/run/work/assembly",
        "expected_outputs": ["/run/artifacts/assembly/draft.fasta"],
    }
    graph = StateGraph(TopLevelState)
    graph.add_node(
        "embed",
        lambda _state: {
            "stage_outputs": {"assembly": {"frozen_attempt": frozen_body}}
        },
    )
    graph.set_entry_point("embed")

    with pytest.raises(ValueError, match="embeds a FrozenAttemptSpec"):
        graph.compile().invoke(
            initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        )


def test_whole_state_budget_warns_at_target_and_rejects_at_hard_limit() -> None:
    target_state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    target_state["shared_parameters"] = {"padding": "x" * (70 * 1024)}
    with pytest.warns(CheckpointBudgetWarning, match="target"):
        validate_top_level_state(target_state)

    hard_state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    hard_state["shared_parameters"] = {"padding": "x" * (130 * 1024)}
    hard_state["skip_reasons"] = {"assembly": "x" * (130 * 1024)}
    with pytest.raises(ValueError, match="hard limit"):
        validate_top_level_state(hard_state)

    with pytest.raises(ValueError, match="whole-state hard limit"):
        validate_state_field(
            "shared_parameters", {"padding": "x" * (256 * 1024)}
        )


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("last_error", "x" * 4097, "last_error"),
        ("review_notes", ["note"] * 17, "review_notes"),
        ("review_notes", ["x" * 2049], r"review_notes\[0\]"),
        ("route_trace", ["route"] * 17, "route_trace"),
        (
            "active_interrupt",
            {"stage": "assembly", "reason": "x" * (4 * 1024)},
            "active_interrupt",
        ),
        (
            "active_interrupt",
            {"stage": "assembly", "unexpected_payload": "not allowed"},
            "unsupported fields",
        ),
        (
            "active_interrupt",
            {
                "stage": "assembly",
                "request_ref": {"path_ref": "artifacts/request.json"},
            },
            "valid ArtifactRef",
        ),
        ("shared_parameters", {"value": object()}, "non-JSON value"),
        ("shared_parameters", {"ratio": float("inf")}, "non-finite float"),
        (
            "shared_parameters",
            {
                "artifact": {
                    "artifact_id": "art_" + "a" * 32,
                    "path_ref": "artifacts/x.json",
                    "sha256": "b" * 64,
                    "body": {"large": "payload"},
                }
            },
            "artifact body",
        ),
    ],
)
def test_state_entrypoints_share_field_validation(
    name: str,
    value: object,
    match: str,
) -> None:
    state: dict[str, object] = dict(
        initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
    )
    state[name] = value

    with pytest.raises((TypeError, ValueError), match=match):
        validate_top_level_state(state)
    with pytest.raises((TypeError, ValueError), match=match):
        validate_state_field(name, value)
