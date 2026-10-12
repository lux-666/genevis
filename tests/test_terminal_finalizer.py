from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.control.terminal import (
    TerminalFinalizationError,
    finalize_authoritative_terminal,
)
from app.schemas.state import initial_state
from app.storage.ledger import Ledger
from tests.data_v2 import v2_run_dir


class _TerminalGraph:
    def __init__(self, state: dict, *, raise_after_update: bool = False) -> None:
        self.state = dict(state)
        self.raise_after_update = raise_after_update
        self.actions: list[str] = []
        self.checkpointer = None

    def get_state(self, _config):
        self.actions.append("get")
        return SimpleNamespace(values=dict(self.state))

    def update_state(self, _config, update):
        self.actions.append("update")
        self.state.update(update)
        if self.raise_after_update:
            raise RuntimeError("ambiguous update result")
        return True


def _completed_state(run_dir) -> dict:
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
    )
    state.update(
        {
            "current_stage": "done",
            "workflow_step_index": 1,
            "entered_workflow_step_index": 0,
            "supervision_state": "completed",
            "active_interrupt": None,
            "workflow_step_outcome": {
                "workflow_step_id": "step-000-inventory",
                "stage": "inventory",
                "outcome": "completed",
            },
        }
    )
    return state


def test_completed_terminal_is_recorded_only_after_checkpoint_proof(tmp_path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    Ledger(run_dir).create_session("s1", "control/manifest.yaml")
    state = _completed_state(run_dir)
    graph = _TerminalGraph(state)

    result = finalize_authoritative_terminal(
        graph,
        {"configurable": {"thread_id": "s1"}},
        state,
        "s1",
        run_dir,
        "memory",
    )

    assert graph.actions == ["get"]
    assert result.terminal_event["summary"]["outcome"] == "completed"
    assert [
        row["event_type"]
        for row in Ledger(run_dir).read_event_rows("s1")
        if row["event_type"] in {"stage_completed", "session_terminal"}
    ] == ["stage_completed", "session_terminal"]
    terminals = [
        row
        for row in Ledger(run_dir).read_event_rows("s1")
        if row["event_type"] == "session_terminal"
    ]
    assert terminals == [result.terminal_event]


def test_legacy_workflow_step_without_roster_remains_terminal_compatible(
    tmp_path,
) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    Ledger(run_dir).create_session("s1", "control/manifest.yaml")
    state = _completed_state(run_dir)
    state["workflow_steps"][0].pop("selected_method_names")
    result = finalize_authoritative_terminal(
        _TerminalGraph(state),
        {"configurable": {"thread_id": "s1"}},
        state,
        "s1",
        run_dir,
        "memory",
    )
    assert result.terminal_event["summary"]["outcome"] == "completed"


def test_failure_update_that_raises_after_commit_is_reconciled(tmp_path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    Ledger(run_dir).create_session("s1", "control/manifest.yaml")
    running = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
    )
    running["entered_workflow_step_index"] = 0
    graph = _TerminalGraph(running, raise_after_update=True)
    failed = {
        **running,
        "supervision_state": "failed_terminal",
        "last_error": "RuntimeError: stage crashed",
        "active_interrupt": None,
    }

    result = finalize_authoritative_terminal(
        graph,
        {"configurable": {"thread_id": "s1"}},
        failed,
        "s1",
        run_dir,
        "memory",
        normalize_failure=True,
    )

    assert graph.actions == ["update", "get"]
    assert result.state["supervision_state"] == "failed_terminal"
    assert result.terminal_event["summary"]["outcome"] == "failed"
    assert result.terminal_event["summary"]["failure_scope"] == "occurrence"
    assert [
        row["event_type"]
        for row in Ledger(run_dir).read_event_rows("s1")
        if row["event_type"] in {"stage_failed", "session_terminal"}
    ] == ["stage_failed", "session_terminal"]


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([("a", "attempt_failed")], "failed"),
        ([("a", "attempt_started")], "failed"),
        ([("a", "attempt_started"), ("a", "attempt_succeeded")], "completed"),
        (
            [
                ("a", "attempt_failed"),
                ("b", "attempt_started"),
                ("b", "attempt_succeeded"),
            ],
            "completed",
        ),
        ([("a", "attempt_succeeded"), ("b", "attempt_started")], "failed"),
        ([("a", "attempt_started"), ("b", "attempt_succeeded")], "failed"),
    ],
)
def test_runtime_failure_closes_missing_fixed_method_dispositions(
    tmp_path, events, expected
) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    state = _failed_middle_occurrence_state(run_dir)
    state["workflow_steps"][1]["selected_method_names"] = [
        "read_qc.long_read_stats.seqkit_stats",
        "read_qc.length_filter.skip",
    ]
    ledger.record_stage_completed(
        "s1", "inventory", workflow_step_id="step-000-inventory"
    )
    for subject, event in events:
        ledger.record_event(
            "s1",
            event,
            {"method_name": "read_qc.long_read_stats.seqkit_stats"},
            stage="read_qc",
            workflow_step_id="step-001-read_qc",
            subject_id=f"s1:step-001-read_qc:seqkit:{subject}",
            idempotency_key=f"attempt:seqkit:{subject}:{event}",
        )
    result = finalize_authoritative_terminal(
        _TerminalGraph(state),
        {"configurable": {"thread_id": "s1"}},
        state,
        "s1",
        run_dir,
        "memory",
        normalize_failure=True,
    )

    dispositions = {
        row["summary"]["method_name"]: row["summary"]["disposition"]
        for row in ledger.read_event_rows("s1")
        if row["event_type"] == "method_disposition_recorded"
    }
    assert dispositions == {
        "read_qc.long_read_stats.seqkit_stats": expected,
        "read_qc.length_filter.skip": "not_started",
    }
    assert result.terminal_event["summary"]["outcome"] == "failed"


def test_inter_occurrence_failure_settles_prefix_without_faking_stage_failure(
    tmp_path,
) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
            {
                "step_id": "step-001-read_qc",
                "stage": "read_qc",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
        ],
    )
    state.update(
        {
            "current_stage": "read_qc",
            "workflow_step_index": 1,
            "entered_workflow_step_index": 0,
            "workflow_step_outcome": {
                "workflow_step_id": "step-000-inventory",
                "stage": "inventory",
                "outcome": "completed",
            },
            "supervision_state": "failed_terminal",
            "last_error": "controller crashed before Read QC entry",
        }
    )
    graph = _TerminalGraph(state)

    result = finalize_authoritative_terminal(
        graph,
        {"configurable": {"thread_id": "s1"}},
        state,
        "s1",
        run_dir,
        "memory",
        normalize_failure=True,
    )

    events = ledger.read_event_rows("s1")
    assert result.terminal_event["summary"]["outcome"] == "failed"
    assert result.terminal_event["summary"]["failure_scope"] == "controller_boundary"
    assert [
        event["event_type"]
        for event in events
        if event["event_type"].startswith("stage_")
    ] == ["stage_completed"]


def _failed_middle_occurrence_state(run_dir) -> dict:
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
            {
                "step_id": "step-001-read_qc",
                "stage": "read_qc",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
            {
                "step_id": "step-002-genome_survey",
                "stage": "genome_survey",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
        ],
    )
    state.update(
        {
            "current_stage": "read_qc",
            "workflow_step_index": 1,
            "entered_workflow_step_index": 1,
            "workflow_step_outcome": {
                "workflow_step_id": "step-001-read_qc",
                "stage": "read_qc",
                "outcome": "failed",
                "reason": "read qc failed",
            },
            "supervision_state": "failed_terminal",
            "last_error": "read qc failed",
            "active_interrupt": None,
        }
    )
    return state


def test_failed_occurrence_rejects_an_unsettled_prefix(tmp_path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    state = _failed_middle_occurrence_state(run_dir)

    with pytest.raises(
        TerminalFinalizationError,
        match="terminal edge of a fully settled workflow prefix",
    ):
        finalize_authoritative_terminal(
            _TerminalGraph(state),
            {"configurable": {"thread_id": "s1"}},
            state,
            "s1",
            run_dir,
            "memory",
        )

    assert not any(
        row["event_type"] == "session_terminal" for row in ledger.read_event_rows("s1")
    )


def test_failed_occurrence_rejects_a_terminal_after_the_failed_step(
    tmp_path,
) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_stage_completed(
        "s1", "inventory", workflow_step_id="step-000-inventory"
    )
    ledger.record_stage_completed(
        "s1", "genome_survey", workflow_step_id="step-002-genome_survey"
    )
    state = _failed_middle_occurrence_state(run_dir)

    with pytest.raises(
        TerminalFinalizationError,
        match="terminal edge of a fully settled workflow prefix",
    ):
        finalize_authoritative_terminal(
            _TerminalGraph(state),
            {"configurable": {"thread_id": "s1"}},
            state,
            "s1",
            run_dir,
            "memory",
        )

    assert not any(
        row["event_type"] == "session_terminal" for row in ledger.read_event_rows("s1")
    )


def test_completed_terminal_rejects_unsettled_occurrence(tmp_path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    Ledger(run_dir).create_session("s1", "control/manifest.yaml")
    state = _completed_state(run_dir)
    state["workflow_step_outcome"] = None
    graph = _TerminalGraph(state)

    with pytest.raises(
        TerminalFinalizationError,
        match=(
            "final explicit occurrence outcome|occurrence settlement|"
            "unsettled workflow occurrences"
        ),
    ):
        finalize_authoritative_terminal(
            graph,
            {"configurable": {"thread_id": "s1"}},
            state,
            "s1",
            run_dir,
            "memory",
        )

    assert not any(
        row["event_type"] == "session_terminal"
        for row in Ledger(run_dir).read_event_rows("s1")
    )


def test_terminal_checkpoint_mismatch_writes_no_ledger_terminal(tmp_path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    Ledger(run_dir).create_session("s1", "control/manifest.yaml")
    expected = _completed_state(run_dir)
    graph = _TerminalGraph(
        {
            **expected,
            "supervision_state": "failed_terminal",
            "last_error": "wrong terminal",
        }
    )

    with pytest.raises(TerminalFinalizationError, match="checkpoint"):
        finalize_authoritative_terminal(
            graph,
            {"configurable": {"thread_id": "s1"}},
            expected,
            "s1",
            run_dir,
            "memory",
        )

    assert not any(
        row["event_type"] == "session_terminal"
        for row in Ledger(run_dir).read_event_rows("s1")
    )


def test_existing_canonical_terminal_stage_must_match_checkpoint(tmp_path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    canonical = ledger.record_session_terminal(
        "s1",
        outcome="completed",
        stage="assembly",
    )
    state = _completed_state(run_dir)
    graph = _TerminalGraph(state)

    with pytest.raises(TerminalFinalizationError, match="stage conflicts"):
        finalize_authoritative_terminal(
            graph,
            {"configurable": {"thread_id": "s1"}},
            state,
            "s1",
            run_dir,
            "memory",
        )

    assert ledger.read_canonical_terminal("s1")["canonical"] == canonical
