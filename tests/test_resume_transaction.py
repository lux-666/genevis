"""Control-layer durable review-application transaction contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.control.resume_transaction import (
    ResumeTransactionError,
    execute_resume_transaction,
)
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import persist_review_request
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import initial_state
from app.storage.frozen_inputs import FrozenExecutionLease
from app.storage.ledger import Ledger
from tests.data_v2 import v2_run_dir

_INVENTORY_WORKFLOW_STEPS = [
    {
        "selected_method_names": [],
        "step_id": "step-000-inventory",
        "stage": "inventory",
        "allow_local_execution": False,
    }
]


class _Graph:
    """Checkpoint double that makes persistence, rather than return values, decisive."""

    def __init__(
        self,
        state: dict,
        *,
        update_result: object = None,
        persist_update: bool = True,
    ) -> None:
        self.state = state
        self.update_result = update_result
        self.persist_update = persist_update
        self.updates: list[dict] = []
        self.update_nodes: list[str | None] = []
        self.invocations = 0

    def get_state(self, _config):
        return SimpleNamespace(values=self.state)

    def update_state(self, _config, update, *, as_node=None):
        self.updates.append(update)
        self.update_nodes.append(as_node)
        if self.persist_update:
            self.state = {**self.state, **update}
        return self.update_result

    def invoke(self, _state, *, config):
        del config
        self.invocations += 1
        return self.state


class ResumeTransactionTests(unittest.TestCase):
    def _setup(self, tmpdir: str):
        run_dir = v2_run_dir(Path(tmpdir) / "data", "s1")
        Ledger(run_dir).create_session("s1", "control/manifest.yaml")
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            workflow_steps=_INVENTORY_WORKFLOW_STEPS,
            stage_plan_ref="control/run_config.json",
            stage_seeds={"inventory": {"status": "pending"}},
        )
        state.update(
            {
                "workflow_step_index": 0,
                "entered_workflow_step_index": 0,
                "current_stage": "inventory",
                "supervision_state": "blocked_human",
                "stage_outputs": {
                    "inventory": {
                        "status": "blocked_human",
                        "review_policy": {"max_blocked_seconds": 300},
                    }
                },
            }
        )
        persisted = persist_review_request(
            state,
            stage="inventory",
            gate_id="inventory_review",
            reason="inventory_human_decision_required",
            payload=ReviewPayload(
                stage="inventory",
                gate_id="inventory_review",
                reason="inventory_human_decision_required",
                decisions_allowed=["continue", "stop_human_review"],
            ),
        )
        state["active_interrupt"] = persisted.compact_interrupt(
            stage="inventory",
            gate_id="inventory_review",
            reason="inventory_human_decision_required",
        )
        request = ResumeReviewRequest(
            session_id="s1",
            stage="inventory",
            gate_id="inventory_review",
            review_request_id=persisted.review_request_id,
            review_request_hash=persisted.request_hash,
            decision="stop_human_review",
        )
        lease = FrozenExecutionLease(session_id="s1", run_dir=run_dir, active=True)
        return run_dir, state, request, lease

    @staticmethod
    def _auto_request(
        request: ResumeReviewRequest,
        rationale_artifact_ref: ArtifactRef,
    ) -> ResumeReviewRequest:
        return request.model_copy(
            update={
                "decided_by": "auto_rule",
                "rationale_artifact_ref": rationale_artifact_ref,
                "metadata": {
                    "source": "foreground_countdown",
                    "configured_seconds": 300,
                    "elapsed_seconds": 300,
                },
            }
        )

    @staticmethod
    def _events(run_dir: Path, event_type: str) -> list[dict]:
        return [
            event
            for event in Ledger(run_dir).read_events("s1")
            if event["event_type"] == event_type
        ]

    def _execute(self, *, graph, run_dir, request, lease, source="test", **kwargs):
        return execute_resume_transaction(
            graph=graph,
            config={"configurable": {"thread_id": "s1"}},
            session_id="s1",
            data_dir=run_dir,
            request=request,
            source=source,
            checkpointer="sqlite",
            lease=lease,
            **kwargs,
        )

    def test_success_persists_intent_application_checkpoint_proof_and_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request, lease = self._setup(tmpdir)
            graph = _Graph(state, update_result={"checkpoint_id": "cp-2"})

            result = self._execute(
                graph=graph,
                run_dir=run_dir,
                request=request,
                lease=lease,
            )

            self.assertEqual(result.state["supervision_state"], "failed_terminal")
            self.assertEqual(result.state["current_stage"], "inventory")
            self.assertEqual(result.review_status, "applied")
            self.assertEqual(len(graph.updates), 1)
            self.assertEqual(graph.update_nodes, ["inventory_stage"])
            self.assertEqual(graph.invocations, 1)
            self.assertIsNotNone(result.intent_artifact_path)
            self.assertIsNotNone(result.application_artifact_path)
            self.assertIsNotNone(result.decision_artifact_path)
            assert result.intent_artifact_path is not None
            assert result.application_artifact_path is not None
            assert result.decision_artifact_path is not None
            self.assertEqual(result.intent_artifact_path.name, "intent.json")
            self.assertEqual(result.application_artifact_path.name, "application.json")
            self.assertEqual(result.decision_artifact_path.name, "decision.json")
            self.assertTrue(result.application_artifact_path.is_file())

            events = Ledger(run_dir).read_events("s1")
            ordered = [
                event["event_type"]
                for event in events
                if event["event_type"]
                in {
                    "resume_started",
                    "review_decision",
                    "review_applied",
                    "resume_completed",
                }
            ]
            self.assertEqual(
                ordered,
                [
                    "resume_started",
                    "review_decision",
                    "review_applied",
                    "resume_completed",
                ],
            )
            decision = self._events(run_dir, "review_decision")
            applied = self._events(run_dir, "review_applied")
            completed = self._events(run_dir, "resume_completed")
            self.assertEqual(decision[0]["decision"], "stop_human_review")
            self.assertEqual(decision[0]["source"], "test")
            self.assertEqual(
                applied[0]["application_id"], decision[0]["application_id"]
            )
            self.assertEqual(completed[0]["review_status"], "applied")

    def test_validation_failure_records_failed_phase_before_any_application_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request, lease = self._setup(tmpdir)
            graph = _Graph(state)
            stale_request = request.model_copy(update={"review_request_hash": "0" * 64})

            with self.assertRaisesRegex(ResumeTransactionError, "request_hash") as ctx:
                self._execute(
                    graph=graph,
                    run_dir=run_dir,
                    request=stale_request,
                    lease=lease,
                )

            self.assertEqual(ctx.exception.phase, "validation")
            self.assertEqual(graph.updates, [])
            self.assertEqual(graph.invocations, 0)
            self.assertEqual(len(self._events(run_dir, "review_decision")), 0)
            self.assertEqual(len(self._events(run_dir, "review_applied")), 0)
            failed = self._events(run_dir, "resume_failed")
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["phase"], "validation")

    def test_applied_notification_and_report_precede_interrupted_invoke(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request, lease = self._setup(tmpdir)
            graph = _Graph(state)
            notified = Mock()

            def interrupt_invoke(_state, *, config):
                notified.assert_called_once()
                applied_request, supervision = notified.call_args.args
                self.assertEqual(applied_request.decision, request.decision)
                self.assertEqual(supervision, graph.state["supervision_state"])
                self.assertEqual(len(self._events(run_dir, "review_applied")), 1)
                report = json.loads(
                    (run_dir / "reports/current_status.json").read_text()
                )
                self.assertEqual(report["supervision_state"], supervision)
                self.assertIsNone(report["active_interrupt"])
                raise KeyboardInterrupt

            graph.invoke = interrupt_invoke
            with self.assertRaises(KeyboardInterrupt):
                self._execute(
                    graph=graph,
                    run_dir=run_dir,
                    request=request,
                    lease=lease,
                    on_review_applied=notified,
                )

    def test_applied_notification_failure_does_not_prevent_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request, lease = self._setup(tmpdir)
            graph = _Graph(state)
            result = self._execute(
                graph=graph,
                run_dir=run_dir,
                request=request,
                lease=lease,
                on_review_applied=Mock(side_effect=BrokenPipeError("closed")),
            )
            self.assertEqual(graph.invocations, 1)
            self.assertEqual(result.review_status, "applied")
            self.assertTrue(
                any(w.name == "review_applied" for w in result.projection_warnings)
            )

    def test_auto_rule_requires_foreground_countdown_transaction_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, human_request, lease = self._setup(tmpdir)
            graph = _Graph(state)
            interrupt = state["active_interrupt"]
            assert interrupt is not None
            rationale_ref = ArtifactRef.model_validate(interrupt["request_ref"])

            with self.assertRaisesRegex(
                ResumeTransactionError,
                "auto_rule decisions are only valid in the foreground countdown path",
            ) as ctx:
                self._execute(
                    graph=graph,
                    run_dir=run_dir,
                    request=self._auto_request(human_request, rationale_ref),
                    lease=lease,
                    source="review",
                )

            self.assertEqual(ctx.exception.phase, "validation")
            self.assertEqual(graph.updates, [])
            self.assertEqual(graph.invocations, 0)
            self.assertEqual(len(self._events(run_dir, "review_decision")), 0)
            self.assertEqual(
                self._events(run_dir, "resume_failed")[0]["phase"],
                "validation",
            )

    def test_false_update_result_without_checkpoint_proof_is_not_invoked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request, lease = self._setup(tmpdir)
            graph = _Graph(state, update_result=False, persist_update=False)
            notified = Mock()

            with self.assertRaisesRegex(
                ResumeTransactionError, "returned False"
            ) as ctx:
                self._execute(
                    graph=graph,
                    run_dir=run_dir,
                    request=request,
                    lease=lease,
                    on_review_applied=notified,
                )

            notified.assert_not_called()
            self.assertEqual(ctx.exception.phase, "checkpoint_apply")
            self.assertEqual(graph.invocations, 0)
            self.assertEqual(len(self._events(run_dir, "review_decision")), 1)
            self.assertEqual(len(self._events(run_dir, "review_applied")), 0)
            self.assertEqual(
                self._events(run_dir, "resume_failed")[0]["phase"],
                "checkpoint_apply",
            )

    def test_ambiguous_update_result_without_checkpoint_proof_is_not_invoked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir, state, request, lease = self._setup(tmpdir)
            graph = _Graph(state, update_result=None, persist_update=False)

            with self.assertRaisesRegex(
                ResumeTransactionError,
                "checkpoint does not carry the prepared application ref",
            ) as ctx:
                self._execute(
                    graph=graph,
                    run_dir=run_dir,
                    request=request,
                    lease=lease,
                )

            self.assertEqual(ctx.exception.phase, "checkpoint_apply")
            self.assertEqual(graph.invocations, 0)
            self.assertEqual(len(self._events(run_dir, "review_decision")), 1)
            self.assertEqual(len(self._events(run_dir, "review_applied")), 0)
            self.assertEqual(
                self._events(run_dir, "resume_failed")[0]["phase"],
                "checkpoint_apply",
            )

    def test_terminal_preflight_rejects_canonical_stage_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(Path(tmpdir) / "data", "s1")
            ledger = Ledger(run_dir)
            ledger.create_session("s1", "control/manifest.yaml")
            ledger.record_session_terminal(
                "s1",
                outcome="completed",
                stage="inventory",
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(run_dir),
                workflow_steps=_INVENTORY_WORKFLOW_STEPS,
                stage_plan_ref="control/run_config.json",
            )
            state.update(
                {
                    "workflow_step_index": 1,
                    "entered_workflow_step_index": 0,
                    "current_stage": "done",
                    "supervision_state": "completed",
                    "active_interrupt": None,
                }
            )
            graph = _Graph(state)
            lease = FrozenExecutionLease(
                session_id="s1",
                run_dir=run_dir,
                active=True,
            )

            with self.assertRaisesRegex(
                ResumeTransactionError,
                "stage conflicts with the checkpoint",
            ) as ctx:
                self._execute(
                    graph=graph,
                    run_dir=run_dir,
                    request=None,
                    lease=lease,
                )

            self.assertEqual(ctx.exception.phase, "terminal_preflight")
            self.assertEqual(graph.invocations, 0)


if __name__ == "__main__":
    unittest.main()
