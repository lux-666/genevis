from typing import get_args, get_type_hints

from app.schemas.state import (
    ActiveInterrupt,
    ControlState,
    DomainStage,
    SupervisionState,
    TopLevelState,
    initial_state,
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
    assert {"gate_id", "reason", "decisions_allowed", "blocked_at"} <= ActiveInterrupt.__optional_keys__
