"""Control-layer resume transaction contract tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.control.lifecycle import ResumeValidationError
from app.control.resume_transaction import (
    ResumeTransactionError,
    execute_resume_transaction,
)
from app.review.base import ResumeReviewRequest
from app.storage.ledger import Ledger
from tests.data_v2 import v2_run_dir


class _Graph:
    def __init__(self, state: dict, *, update_result=None) -> None:
        self.state = state
        self.update_result = update_result
        self.updates: list[dict] = []
        self.invocations = 0

    def get_state(self, _config):
        return SimpleNamespace(values=self.state)

    def update_state(self, _config, update):
        self.updates.append(update)
        return self.update_result

    def invoke(self, _state, *, config):
        self.invocations += 1
        return {
            **self.state,
            "current_stage": "gene_annotation",
            "supervision_state": "running_stage",
            "route_trace": ["assembly_review_stage"],
        }


class ResumeTransactionTests(unittest.TestCase):
    def _setup(self, tmpdir: str):
        run_dir = v2_run_dir(Path(tmpdir) / "data", "s1")
        Ledger(run_dir).create_session("s1", "control/manifest.yaml")
        state = {
            "session_id": "s1",
            "data_dir": str(run_dir),
            "control_state": "running",
            "current_stage": "assembly_review",
            "supervision_state": "blocked_human",
            "input_manifest_ref": "control/manifest.yaml",
            "review_notes": [],
            "route_trace": [],
            "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
            "skip_reasons": {},
        }
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            gate_id="assembly_review",
            decision="accept",
            candidate_id="flye",
        )
        return run_dir, state, request

    @staticmethod
    def _auto_request(request: ResumeReviewRequest) -> ResumeReviewRequest:
        return request.model_copy(
            update={
                "decided_by": "auto_rule",
                "rationale_artifact_ref": "artifacts/reviews/request.json",
                "metadata": {
                    "source": "foreground_countdown",
                    "configured_seconds": 300,
                    "elapsed_seconds": 300,
                },
            }
        )

    def test_success_owns_update_invoke_and_standard_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request = self._setup(tmpdir)
            graph = _Graph(state)
            with (
                patch("app.control.resume_transaction.validate_resume_review_state"),
                patch(
                    "app.control.resume_transaction.resume_review_state",
                    return_value={"supervision_state": "running_stage"},
                ),
            ):
                result = execute_resume_transaction(
                    graph=graph,
                    config={"configurable": {"thread_id": "s1"}},
                    session_id="s1",
                    data_dir=run_dir,
                    request=request,
                    source="test",
                    checkpointer="sqlite",
                )

            self.assertEqual(result.state["current_stage"], "gene_annotation")
            self.assertEqual(len(graph.updates), 1)
            self.assertEqual(graph.invocations, 1)
            events = Ledger(run_dir).read_events("s1")
            started = [
                event for event in events if event["event_type"] == "resume_started"
            ]
            completed = [
                event for event in events if event["event_type"] == "resume_completed"
            ]
            self.assertEqual(len(started), 1)
            self.assertEqual(len(completed), 1)
            self.assertEqual(started[0]["source"], "test")
            self.assertEqual(started[0]["checkpointer"], "sqlite")
            self.assertEqual(started[0]["decision"]["stage"], "assembly_review")
            self.assertEqual(completed[0]["current_stage"], "gene_annotation")

    def test_validation_failure_records_one_failed_phase_without_checkpoint_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request = self._setup(tmpdir)
            graph = _Graph(state)
            with (
                patch(
                    "app.control.resume_transaction.validate_resume_review_state",
                    side_effect=ResumeValidationError("stale request"),
                ),
                self.assertRaisesRegex(ResumeTransactionError, "stale request"),
            ):
                execute_resume_transaction(
                    graph=graph,
                    config={"configurable": {"thread_id": "s1"}},
                    session_id="s1",
                    data_dir=run_dir,
                    request=request,
                    source="test",
                    checkpointer="sqlite",
                )

            self.assertEqual(graph.updates, [])
            self.assertEqual(graph.invocations, 0)
            failed = [
                event
                for event in Ledger(run_dir).read_events("s1")
                if event["event_type"] == "resume_failed"
            ]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["phase"], "validation")

    def test_auto_rule_requires_foreground_countdown_transaction_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, human_request = self._setup(tmpdir)
            request = self._auto_request(human_request)
            graph = _Graph(state)
            with (
                patch(
                    "app.control.resume_transaction.validate_resume_review_state"
                ) as validate,
                patch("app.control.resume_transaction.resume_review_state") as resume,
                self.assertRaisesRegex(
                    ResumeTransactionError,
                    "auto_rule.*foreground_countdown",
                ),
            ):
                execute_resume_transaction(
                    graph=graph,
                    config={"configurable": {"thread_id": "s1"}},
                    session_id="s1",
                    data_dir=run_dir,
                    request=request,
                    source="review",
                    checkpointer="sqlite",
                    blocked_timeout_seconds=300,
                )

            validate.assert_not_called()
            resume.assert_not_called()
            self.assertEqual(graph.updates, [])
            self.assertEqual(graph.invocations, 0)
            failed = [
                event
                for event in Ledger(run_dir).read_events("s1")
                if event["event_type"] == "resume_failed"
            ]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["phase"], "validation")

    def test_auto_rule_accepts_foreground_countdown_transaction_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, human_request = self._setup(tmpdir)
            request = self._auto_request(human_request)
            graph = _Graph(state)
            with (
                patch(
                    "app.control.resume_transaction.validate_resume_review_state"
                ) as validate,
                patch(
                    "app.control.resume_transaction.resume_review_state",
                    return_value={"supervision_state": "running_stage"},
                ) as resume,
            ):
                execute_resume_transaction(
                    graph=graph,
                    config={"configurable": {"thread_id": "s1"}},
                    session_id="s1",
                    data_dir=run_dir,
                    request=request,
                    source="foreground_countdown",
                    checkpointer="sqlite",
                    blocked_timeout_seconds=300,
                )

            validate.assert_called_once_with(state, request)
            resume.assert_called_once_with(
                state,
                request,
                blocked_timeout_seconds=300,
                decision_source="foreground_countdown",
            )
            self.assertEqual(len(graph.updates), 1)
            self.assertEqual(graph.invocations, 1)

    def test_false_update_result_is_audited_and_never_invoked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request = self._setup(tmpdir)
            graph = _Graph(state, update_result=False)
            with (
                patch("app.control.resume_transaction.validate_resume_review_state"),
                patch(
                    "app.control.resume_transaction.resume_review_state",
                    return_value={"supervision_state": "running_stage"},
                ),
                self.assertRaisesRegex(ResumeTransactionError, "returned False"),
            ):
                execute_resume_transaction(
                    graph=graph,
                    config={"configurable": {"thread_id": "s1"}},
                    session_id="s1",
                    data_dir=run_dir,
                    request=request,
                    source="test",
                    checkpointer="sqlite",
                )

            self.assertEqual(graph.invocations, 0)
            failed = [
                event
                for event in Ledger(run_dir).read_events("s1")
                if event["event_type"] == "resume_failed"
            ]
            self.assertEqual(failed[0]["phase"], "update_state")


if __name__ == "__main__":
    unittest.main()
