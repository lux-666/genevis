"""Foreground-only review countdown and claim-window tests."""

from __future__ import annotations

import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.control.errors import ResumeValidationError
from app.entrypoints.review_interactive import wait_for_gate_claim
from app.entrypoints.run import _interactive_gate_loop
from app.review.base import ResumeReviewRequest


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
            "review_request_artifact_ref": ("artifacts/reviews/review_1/request.json"),
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
        "review_request_artifact_ref": "artifacts/reviews/review_1/request.json",
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
    ) -> tuple[dict, Mock, Mock, Mock]:
        summary = summary or _summary(wait_seconds)
        execute = Mock(
            return_value=SimpleNamespace(
                state={"supervision_state": "completed", "current_stage": "done"},
                decision_artifact_path=None,
                annotation_intake_artifact_ref=None,
            )
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
            )
        return final, execute, collect, waiter

    def test_zero_keeps_gate_without_resolver_on_manual_path(self):
        final, execute, collect, waiter = self._run_loop(0)

        self.assertEqual(final["supervision_state"], "blocked_human")
        waiter.assert_not_called()
        collect.assert_called_once()
        execute.assert_not_called()

    def test_zero_immediately_runs_existing_route_resolver(self):
        final, execute, collect, waiter = self._run_loop(
            0,
            summary=_route_summary(0),
        )

        self.assertEqual(final["supervision_state"], "completed")
        waiter.assert_not_called()
        collect.assert_not_called()
        call = execute.call_args.kwargs
        self.assertEqual(call["source"], "foreground_countdown")
        self.assertEqual(call["blocked_timeout_seconds"], 0.0)
        self.assertEqual(call["request"].decision, "select_hifi")
        self.assertEqual(call["request"].decided_by, "auto_rule")

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
