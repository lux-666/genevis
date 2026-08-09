from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.control.lifecycle import (
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
    terminals = [
        row
        for row in Ledger(run_dir).read_event_rows("s1")
        if row["event_type"] == "session_terminal"
    ]
    assert terminals == [result.terminal_event]


def test_failure_update_that_raises_after_commit_is_reconciled(tmp_path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    Ledger(run_dir).create_session("s1", "control/manifest.yaml")
    running = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
    )
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
