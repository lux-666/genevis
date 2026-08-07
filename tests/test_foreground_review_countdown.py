"""Foreground-only review countdown and claim-window tests."""

from __future__ import annotations

import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.control.errors import ResumeValidationError
from app.control.resume_transaction import ResumeTransactionError
from app.entrypoints.review_interactive import wait_for_gate_claim
from app.entrypoints.run import _interactive_gate_loop
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate


def _summary(wait_seconds: int) -> dict:
    return {
        "session_id": "s1",
        "supervision_state": "blocked_human",
        "active_interrupt": {
            "stage": "inventory",
            "gate_id": "inventory_review",
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        },
        "decision_requirements": {
            "stage": "inventory",
            "gate_id": "inventory_review",
            "decisions_allowed": ["continue", "stop_human_review"],
            "review_request_id": "review_1",
            "request_hash": "a" * 64,
            "review_request_artifact_ref": (
                "artifacts/steps/step-000-inventory/inventory/reviews/"
                "review_1/request.json"
            ),
            "max_blocked_seconds": wait_seconds,
        },
        "latest_review_request_artifact": None,
        "next_action": {},
    }


def _route_summary(wait_seconds: int) -> dict:
    summary = _summary(wait_seconds)
    summary["active_interrupt"] = {
        "stage": "assembly",
        "gate_id": "route_selection",
        "blocked_at": datetime.now(timezone.utc).isoformat(),
    }
    summary["decision_requirements"] = {
        "stage": "assembly",
        "gate_id": "route_selection",
        "decisions_allowed": ["select_hifi", "stop_human_review"],
        "review_request_id": "review_1",
        "request_hash": "a" * 64,
        "review_request_artifact_ref": (
            "artifacts/steps/step-001-assembly/assembly/reviews/"
            "review_1/request.json"
        ),
        "max_blocked_seconds": wait_seconds,
        "recommended_route": "hifi",
        "recommended_assembly_selection": {
            "primary_method": "assembly.candidate.hifiasm"
        },
    }
    return summary


def _human_request(
    *, stage: str = "inventory", gate_id: str = "inventory_review"
) -> ResumeReviewRequest:
    return ResumeReviewRequest(
        session_id="s1",
        stage=stage,
        gate_id=gate_id,
        review_request_id="review_1",
        review_request_hash="a" * 64,
        decision="select_hifi" if stage == "assembly" else "continue",
    )


def test_parameter_approval_gates_share_the_foreground_timeout_resolver():
    resolver_paths = {
        get_review_gate(stage, "parameter_approval").timeout_applier
        for stage in ("assembly", "hic_scaffolding", "t2t")
    }

    assert resolver_paths == {"app.review.approval:apply_parameter_approval_timeout"}
    for stage in ("assembly", "hic_scaffolding", "t2t"):
        gate = get_review_gate(stage, "parameter_approval")
        assert gate.template_preferred_decision == "approve_parameters"
        assert (
            gate.load_timeout_applier().__name__ == "apply_parameter_approval_timeout"
        )


class GateClaimWaitTests(unittest.TestCase):
    def test_countdown_expires_without_input(self):
        current = [0.0]

        def clock() -> float:
            return current[0]

        def wait_for_input(_stream: io.StringIO, timeout: float) -> bool:
            current[0] += timeout
            return False

        output = io.StringIO()
        result = wait_for_gate_claim(
            stage="inventory",
            gate_id="inventory_review",
            automatic_resolver="continue",
            remaining_seconds=2.2,
            input_stream=io.StringIO(),
            output_stream=output,
            clock=clock,
            input_waiter=wait_for_input,
        )

        self.assertEqual(result, "expired")
        self.assertIn("stage=inventory", output.getvalue())
        self.assertIn("automatic=continue", output.getvalue())

    def test_enter_claims_gate(self):
        result = wait_for_gate_claim(
            stage="inventory",
            gate_id="inventory_review",
            automatic_resolver="continue",
            remaining_seconds=300,
            input_stream=io.StringIO("\n"),
            output_stream=io.StringIO(),
            input_waiter=lambda _stream, _timeout: True,
        )

        self.assertEqual(result, "claimed")

    def test_eof_cancels_without_accepting(self):
        result = wait_for_gate_claim(
            stage="inventory",
            gate_id="inventory_review",
            automatic_resolver="continue",
            remaining_seconds=300,
            input_stream=io.StringIO(""),
            output_stream=io.StringIO(),
            input_waiter=lambda _stream, _timeout: True,
        )

        self.assertEqual(result, "cancelled")

    def test_keyboard_interrupt_cancels_without_accepting(self):
        def interrupt(_stream: io.StringIO, _timeout: float) -> bool:
            raise KeyboardInterrupt

        result = wait_for_gate_claim(
            stage="inventory",
            gate_id="inventory_review",
            automatic_resolver="continue",
            remaining_seconds=300,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
            input_waiter=interrupt,
        )

        self.assertEqual(result, "cancelled")


class ForegroundGateLoopTests(unittest.TestCase):
    def _run_loop(
        self,
        wait_seconds: int,
        *,
        claim: str | None = None,
        collected: tuple[ResumeReviewRequest, dict] | None = None,
        summary: dict | None = None,
        summary_sequence: list[dict] | None = None,
        execute_side_effect: list[object] | None = None,
        verified_inputs: object | None = None,
    ) -> tuple[dict, Mock, Mock, Mock]:
        summary = summary or _summary(wait_seconds)
        completed_transaction = SimpleNamespace(
            state={"supervision_state": "completed", "current_stage": "done"},
            decision_artifact_path=None,
            annotation_intake_artifact_ref=None,
        )
        execute = Mock(
            return_value=completed_transaction,
            side_effect=execute_side_effect,
        )
        collect = Mock(return_value=collected)
        waiter = Mock(return_value=claim)
        result = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "current_stage": "inventory",
        }
        with (
            patch(
                "app.entrypoints.review_interactive.load_blocked_gate_summary",
                return_value=summary,
                side_effect=summary_sequence,
            ),
            patch(
                "app.entrypoints.review_interactive.collect_gate_decision",
                collect,
            ),
            patch("app.entrypoints.review_interactive.render_decision_brief"),
            patch(
                "app.control.resume_transaction.execute_resume_transaction",
                execute,
            ),
            patch("app.entrypoints.run.Ledger"),
        ):
            final = _interactive_gate_loop(
                Mock(),
                {"configurable": {"thread_id": "s1"}},
                result,
                "s1",
                "/tmp/run",
                claim_waiter=waiter,
                verified_inputs=verified_inputs,
            )
        return final, execute, collect, waiter

    def test_zero_keeps_gate_without_resolver_on_manual_path(self):
        final, execute, collect, waiter = self._run_loop(0)

        self.assertEqual(final["supervision_state"], "blocked_human")
        waiter.assert_not_called()
        collect.assert_called_once()
        execute.assert_not_called()

    def test_zero_immediately_runs_existing_route_resolver(self):
        verified_inputs = object()
        final, execute, collect, waiter = self._run_loop(
            0,
            summary=_route_summary(0),
            verified_inputs=verified_inputs,
        )

        self.assertEqual(final["supervision_state"], "completed")
        waiter.assert_not_called()
        collect.assert_not_called()
        call = execute.call_args.kwargs
        self.assertEqual(call["source"], "foreground_countdown")
        self.assertEqual(call["blocked_timeout_seconds"], 0.0)
        self.assertEqual(call["request"].decision, "select_hifi")
        self.assertEqual(call["request"].decided_by, "auto_rule")
        self.assertIs(call["verified_inputs"], verified_inputs)

    def test_zero_falls_back_to_manual_when_existing_resolver_fails(self):
        with patch(
            "app.control.lifecycle.build_foreground_auto_resume_request",
            side_effect=ResumeValidationError("missing binding"),
        ):
            final, execute, collect, waiter = self._run_loop(
                0,
                summary=_route_summary(0),
            )

        self.assertEqual(final["supervision_state"], "blocked_human")
        waiter.assert_not_called()
        collect.assert_called_once()
        execute.assert_not_called()

    def test_auto_builder_failure_does_not_prompt_after_integrity_drift(self):
        original = _route_summary(0)
        invalid = {
            **original,
            "review_request_verification_error": "request hash mismatch",
        }
        with patch(
            "app.control.lifecycle.build_foreground_auto_resume_request",
            side_effect=ResumeValidationError("missing binding"),
        ):
            final, execute, collect, waiter = self._run_loop(
                0,
                summary=original,
                summary_sequence=[original, invalid],
            )

        self.assertEqual(final["supervision_state"], "blocked_human")
        waiter.assert_not_called()
        collect.assert_not_called()
        execute.assert_not_called()

    def test_auto_transaction_failure_falls_back_to_manual_on_same_verified_gate(self):
        human = _human_request(stage="assembly", gate_id="route_selection")
        completed = SimpleNamespace(
            state={"supervision_state": "completed", "current_stage": "done"},
            decision_artifact_path=None,
        )
        final, execute, collect, waiter = self._run_loop(
            0,
            summary=_route_summary(0),
            collected=(human, human.model_dump(mode="json")),
            execute_side_effect=[
                ResumeTransactionError("decision_apply", "resolver failed"),
                completed,
            ],
        )

        self.assertEqual(final["supervision_state"], "completed")
        waiter.assert_not_called()
        collect.assert_called_once()
        self.assertFalse(collect.call_args.kwargs["render_brief"])
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(
            execute.call_args_list[0].kwargs["source"],
            "foreground_countdown",
        )
        self.assertEqual(
            execute.call_args_list[1].kwargs["source"],
            "interactive_gate",
        )
        self.assertIsNone(execute.call_args_list[1].kwargs["blocked_timeout_seconds"])

    def test_auto_transaction_failure_does_not_prompt_after_integrity_drift(self):
        original = _route_summary(0)
        invalid = {
            **original,
            "review_request_verification_error": "request hash mismatch",
        }
        final, execute, collect, waiter = self._run_loop(
            0,
            summary=original,
            summary_sequence=[original, invalid],
            execute_side_effect=[
                ResumeTransactionError("validation", "request changed")
            ],
        )

        self.assertEqual(final["supervision_state"], "blocked_human")
        waiter.assert_not_called()
        collect.assert_not_called()
        execute.assert_called_once()

    def test_enter_claims_timer_and_uses_human_prompt(self):
        human = _human_request(stage="assembly", gate_id="route_selection")
        final, execute, collect, waiter = self._run_loop(
            300,
            claim="claimed",
            collected=(human, human.model_dump(mode="json")),
            summary=_route_summary(300),
        )

        self.assertEqual(final["supervision_state"], "completed")
        waiter.assert_called_once()
        self.assertFalse(collect.call_args.kwargs["render_brief"])
        call = execute.call_args.kwargs
        self.assertEqual(call["source"], "interactive_gate")
        self.assertIsNone(call["blocked_timeout_seconds"])
        self.assertEqual(call["request"].decided_by, "human")

    def test_indefinite_wait_goes_directly_to_manual_prompt(self):
        human = _human_request()
        final, execute, collect, waiter = self._run_loop(
            -1,
            collected=(human, human.model_dump(mode="json")),
        )

        self.assertEqual(final["supervision_state"], "completed")
        waiter.assert_not_called()
        collect.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["source"], "interactive_gate")

    def test_cancelled_countdown_leaves_original_gate_blocked(self):
        final, execute, collect, waiter = self._run_loop(
            300,
            claim="cancelled",
            summary=_route_summary(300),
        )

        self.assertEqual(final["supervision_state"], "blocked_human")
        waiter.assert_called_once()
        collect.assert_not_called()
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
