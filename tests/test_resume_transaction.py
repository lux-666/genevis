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
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import persist_review_request
from app.schemas.state import initial_state
from app.storage.ledger import Ledger
from tests.data_v2 import v2_run_dir

_ASSEMBLY_REVIEW_WORKFLOW_STEPS = [
    {
        "step_id": "step-000-inventory",
        "stage": "inventory",
        "allow_local_execution": False,
    },
    {
        "step_id": "step-001-assembly",
        "stage": "assembly",
        "allow_local_execution": True,
    },
    {
        "step_id": "step-002-assembly_review",
        "stage": "assembly_review",
        "allow_local_execution": True,
    },
    {
        "step_id": "step-003-hic_scaffolding",
        "stage": "hic_scaffolding",
        "allow_local_execution": True,
    },
]


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
            "workflow_step_index": 3,
            "entered_workflow_step_index": 2,
            "current_stage": "hic_scaffolding",
            "supervision_state": "running_stage",
            "route_trace": ["assembly_review_stage"],
        }


class ResumeTransactionTests(unittest.TestCase):
    def _setup(self, tmpdir: str):
        run_dir = v2_run_dir(Path(tmpdir) / "data", "s1")
        Ledger(run_dir).create_session("s1", "control/manifest.yaml")
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            workflow_steps=_ASSEMBLY_REVIEW_WORKFLOW_STEPS,
            stage_plan_ref="control/run_config.json",
            stage_seeds={"assembly_review": {"status": "pending"}},
        )
        state.update(
            {
                "workflow_step_index": 2,
                "entered_workflow_step_index": 2,
                "current_stage": "assembly_review",
                "supervision_state": "blocked_human",
                "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
            }
        )
        persisted = persist_review_request(
            state,
            stage="assembly_review",
            gate_id="assembly_review",
            reason="assembly_review_human_decision_required",
            payload=ReviewPayload(
                stage="assembly_review",
                gate_id="assembly_review",
                reason="assembly_review_human_decision_required",
                candidates=[{"candidate_id": "flye"}],
                decisions_allowed=["accept", "stop_human_review"],
            ),
        )
        state["active_interrupt"] = persisted.compact_interrupt(
            stage="assembly_review",
            gate_id="assembly_review",
            reason="assembly_review_human_decision_required",
        )
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            gate_id="assembly_review",
            review_request_id=persisted.review_request_id,
            review_request_hash=persisted.request_hash,
            decision="accept",
            candidate_id="flye",
            metadata={"review_request_ref": persisted.request_ref.path_ref},
        )
        return run_dir, state, request

    @staticmethod
    def _auto_request(request: ResumeReviewRequest) -> ResumeReviewRequest:
        return request.model_copy(
            update={
                "decided_by": "auto_rule",
                "rationale_artifact_ref": request.metadata["review_request_ref"],
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
            verified_inputs = object()
            with (
                patch("app.control.resume_transaction.validate_resume_review_state"),
                patch(
                    "app.control.resume_transaction.resume_review_state",
                    return_value={"supervision_state": "running_stage"},
                ) as resume,
            ):
                result = execute_resume_transaction(
                    graph=graph,
                    config={"configurable": {"thread_id": "s1"}},
                    session_id="s1",
                    data_dir=run_dir,
                    request=request,
                    source="test",
                    checkpointer="sqlite",
                    verified_inputs=verified_inputs,
                )

            self.assertEqual(result.state["current_stage"], "hic_scaffolding")
            self.assertIs(
                resume.call_args.kwargs["verified_inputs"],
                verified_inputs,
            )
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
            self.assertEqual(
                started[0]["workflow_step_id"], "step-002-assembly_review"
            )
            self.assertEqual(
                completed[0]["workflow_step_id"], "step-002-assembly_review"
            )
            self.assertEqual(completed[0]["current_stage"], "hic_scaffolding")

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
