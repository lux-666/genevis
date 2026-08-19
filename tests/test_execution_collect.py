"""Tests for the shared execution collection failure boundary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.control.workflow_plan import bind_attempt_to_current_workflow_step
from app.execution.collect import (
    COLLECT_REASON_MAX_CHARS,
    collect_with_failure_boundary,
)
from app.execution.fake_runner import FakeRunner
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    run_method_attempts,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.execution import ExecutionResult
from app.schemas.state import initial_state
from app.storage.ledger import Ledger, LedgerConflictError, LedgerFormatError
from app.storage.paths import DataLayoutError, SessionPaths
from tests.data_v2 import v2_run_dir


def _state(run_dir: Path, session_id: str, *, status: str) -> dict:
    state = initial_state(
        session_id=session_id,
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-001-genome_survey",
                "stage": "genome_survey",
                "allow_local_execution": False,
            },
        ],
    )
    state["current_stage"] = "genome_survey"
    state["workflow_step_index"] = 1
    state["entered_workflow_step_index"] = 1
    state["stage_outputs"] = {"genome_survey": {"status": status}}
    return state


class CollectFailureBoundaryTests(unittest.TestCase):
    def test_expected_collect_errors_become_bounded_auditable_failures(self):
        errors = [
            OSError("required output is missing"),
            DataLayoutError("invalid artifact layout"),
            LedgerFormatError("malformed ledger row"),
            OSError("x" * (70 * 1024)),
        ]

        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__, index=index):
                with tempfile.TemporaryDirectory() as tmpdir:
                    session_id = f"s{index}"
                    run_dir = v2_run_dir(tmpdir, session_id)
                    state = _state(run_dir, session_id, status="jellyfish_ran")

                    def fail_collect(_state, *, raised=error):
                        raise raised

                    result = collect_with_failure_boundary(
                        state,
                        lifecycle=MethodExecutionLifecycle(
                            stage="genome_survey",
                            name="Jellyfish",
                            method_name="genome_survey.kmer_count.jellyfish",
                            frozen_status="jellyfish_frozen",
                            ran_status="jellyfish_ran",
                        ),
                        collect=fail_collect,
                    )

                    output = result["stage_outputs"]["genome_survey"]
                    self.assertEqual(output["status"], "failed")
                    self.assertEqual(output["failure_kind"], "collect")
                    self.assertLessEqual(
                        len(output["reason"]), COLLECT_REASON_MAX_CHARS
                    )
                    events = Ledger(run_dir).read_events(session_id)
                    self.assertFalse(
                        any(event["event_type"] == "stage_failed" for event in events)
                    )

    def test_collect_callback_value_error_remains_visible(self):
        """A callback contract bug must not become a normal collect failure."""

        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            state = _state(run_dir, "s1", status="jellyfish_ran")

            def broken_collect(_state):
                raise ValueError("collect callback contract bug")

            with self.assertRaisesRegex(ValueError, "collect callback contract bug"):
                collect_with_failure_boundary(
                    state,
                    lifecycle=MethodExecutionLifecycle(
                        stage="genome_survey",
                        name="Jellyfish",
                        method_name="genome_survey.kmer_count.jellyfish",
                        frozen_status="jellyfish_frozen",
                        ran_status="jellyfish_ran",
                    ),
                    collect=broken_collect,
                )

            self.assertEqual(Ledger(run_dir).read_events("s1"), [])

    def test_failure_records_attempt_event_and_leaves_stage_terminal_to_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            state = _state(run_dir, "s1", status="jellyfish_ran")
            lifecycle = MethodExecutionLifecycle(
                stage="genome_survey",
                name="Jellyfish",
                method_name="genome_survey.kmer_count.jellyfish",
                frozen_status="jellyfish_frozen",
                ran_status="jellyfish_ran",
                outcome_policy=lambda _state: None,
            )
            working_dir = SessionPaths.from_run_dir(run_dir).workflow_step_work_path(
                "step-001-genome_survey", "genome_survey", "jellyfish"
            )
            working_dir.mkdir(parents=True)
            script_path = working_dir / "run.sh"
            script_path.write_text("exit 0\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:genome_survey:jellyfish",
                session_id="s1",
                stage="genome_survey",
                method_name=lifecycle.method_name,
                script_path=script_path,
                working_dir=working_dir,
                expected_outputs=[working_dir / "required.tsv"],
                env=None,
                env_spec=None,
            )
            attempt = bind_attempt_to_current_workflow_step(state, attempt)
            state["stage_outputs"] = freeze_method_attempts(
                state, lifecycle, [attempt]
            )["stage_outputs"]
            state["stage_outputs"] = run_method_attempts(
                state, lifecycle, runner=FakeRunner()
            )["stage_outputs"]

            def fail_collect(_state):
                raise FileNotFoundError("required.tsv")

            collect_with_failure_boundary(
                state,
                lifecycle=lifecycle,
                collect=fail_collect,
            )

            event_types = [
                event["event_type"] for event in Ledger(run_dir).read_events("s1")
            ]
            self.assertIn("attempt_failed", event_types)
            self.assertNotIn("stage_failed", event_types)

    def test_shared_graph_applies_collect_failure_boundary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            state = _state(run_dir, "s1", status="planned")
            lifecycle = MethodExecutionLifecycle(
                stage="genome_survey",
                name="Jellyfish",
                method_name="genome_survey.kmer_count.jellyfish",
                frozen_status="jellyfish_frozen",
                ran_status="jellyfish_ran",
                outcome_policy=lambda _state: None,
            )

            def freeze(current):
                working_dir = SessionPaths.from_run_dir(
                    run_dir
                ).workflow_step_work_path(
                    "step-001-genome_survey", "genome_survey", "graph"
                )
                working_dir.mkdir(parents=True, exist_ok=True)
                script_path = working_dir / "run.sh"
                script_path.write_text("exit 0\n", encoding="utf-8")
                attempt = build_frozen_attempt(
                    attempt_id="s1:genome_survey:jellyfish",
                    session_id="s1",
                    stage="genome_survey",
                    method_name=lifecycle.method_name,
                    script_path=script_path,
                    working_dir=working_dir,
                    expected_outputs=[working_dir / "required.tsv"],
                    env=None,
                    env_spec=None,
                )
                attempt = bind_attempt_to_current_workflow_step(current, attempt)
                return freeze_method_attempts(current, lifecycle, [attempt])

            def broken_collect(_state):
                raise OSError("required output disappeared")

            result = build_method_graph(
                lifecycle,
                freeze,
                broken_collect,
                runner=FakeRunner(),
            ).invoke(state)

            output = result["stage_outputs"]["genome_survey"]
            self.assertEqual(output["status"], "failed")
            self.assertEqual(output["failure_kind"], "collect")
            self.assertIn("required output disappeared", output["reason"])

    def test_existing_attempt_failure_does_not_create_stage_terminal_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            state = _state(run_dir, "s1", status="jellyfish_ran")
            lifecycle = MethodExecutionLifecycle(
                stage="genome_survey",
                name="Jellyfish",
                method_name="genome_survey.kmer_count.jellyfish",
                frozen_status="jellyfish_frozen",
                ran_status="jellyfish_ran",
                process_failure="collect",
            )
            working_dir = SessionPaths.from_run_dir(run_dir).workflow_step_work_path(
                "step-001-genome_survey", "genome_survey", "failed"
            )
            working_dir.mkdir(parents=True)
            script_path = working_dir / "run.sh"
            script_path.write_text("exit 1\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:genome_survey:jellyfish",
                session_id="s1",
                stage="genome_survey",
                method_name=lifecycle.method_name,
                script_path=script_path,
                working_dir=working_dir,
                expected_outputs=[working_dir / "required.tsv"],
                env=None,
                env_spec=None,
            )
            attempt = bind_attempt_to_current_workflow_step(state, attempt)
            state["stage_outputs"] = freeze_method_attempts(
                state, lifecycle, [attempt]
            )["stage_outputs"]
            stdout = working_dir / "log"
            stderr = working_dir / "err"
            stdout.write_text("", encoding="utf-8")
            stderr.write_text("process failed", encoding="utf-8")
            runner = FakeRunner()
            runner.expect_command(
                "genome_survey",
                lifecycle.method_name,
                ExecutionResult(
                    attempt_id=attempt.attempt_id,
                    success=False,
                    return_code=1,
                    stdout_path=stdout,
                    stderr_path=stderr,
                    status="failed",
                ),
            )
            state["stage_outputs"] = run_method_attempts(
                state, lifecycle, runner=runner
            )["stage_outputs"]

            def fail_collect(_state):
                raise OSError("required.tsv is missing")

            result = collect_with_failure_boundary(
                state,
                lifecycle=lifecycle,
                collect=fail_collect,
            )

            self.assertEqual(
                result["stage_outputs"]["genome_survey"]["status"],
                "failed",
            )
            event_types = [
                event["event_type"] for event in Ledger(run_dir).read_events("s1")
            ]
            self.assertIn("attempt_failed", event_types)
            self.assertNotIn("stage_failed", event_types)

    def test_ledger_conflict_at_collect_boundary_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            state = _state(run_dir, "s1", status="jellyfish_ran")
            lifecycle = MethodExecutionLifecycle(
                stage="genome_survey",
                name="Jellyfish",
                method_name="genome_survey.kmer_count.jellyfish",
                frozen_status="jellyfish_frozen",
                ran_status="jellyfish_ran",
            )

            def conflict_collect(_state):
                raise LedgerConflictError(
                    "idempotency key owns a different ledger fact"
                )

            result = collect_with_failure_boundary(
                state,
                lifecycle=lifecycle,
                collect=conflict_collect,
            )

            output = result["stage_outputs"]["genome_survey"]
            self.assertEqual(output["status"], "failed")
            self.assertEqual(output["failure_kind"], "collect")
            self.assertIn("idempotency key", output["reason"])
            self.assertFalse(
                any(
                    event["event_type"] == "stage_failed"
                    for event in Ledger(run_dir).read_events("s1")
                )
            )

    def test_collect_failure_after_attempt_success_keeps_single_terminal_fact(self):
        # Adapters own attempt_succeeded; the boundary must not add a
        # contradicting attempt_failed when collect fails after the adapter
        # already recorded success.
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            state = _state(run_dir, "s1", status="jellyfish_ran")
            lifecycle = MethodExecutionLifecycle(
                stage="genome_survey",
                name="Jellyfish",
                method_name="genome_survey.kmer_count.jellyfish",
                frozen_status="jellyfish_frozen",
                ran_status="jellyfish_ran",
            )
            working_dir = SessionPaths.from_run_dir(run_dir).workflow_step_work_path(
                "step-001-genome_survey", "genome_survey", "jellyfish"
            )
            working_dir.mkdir(parents=True)
            script_path = working_dir / "run.sh"
            script_path.write_text("exit 0\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:genome_survey:jellyfish",
                session_id="s1",
                stage="genome_survey",
                method_name=lifecycle.method_name,
                script_path=script_path,
                working_dir=working_dir,
                expected_outputs=[working_dir / "required.tsv"],
                env=None,
                env_spec=None,
            )
            attempt = bind_attempt_to_current_workflow_step(state, attempt)
            state["stage_outputs"] = freeze_method_attempts(
                state, lifecycle, [attempt]
            )["stage_outputs"]
            state["stage_outputs"] = run_method_attempts(
                state, lifecycle, runner=FakeRunner()
            )["stage_outputs"]

            def succeed_then_fail_collect(_state):
                Ledger(run_dir).record_attempt_succeeded("s1", attempt)
                raise OSError("post-success storage failure")

            result = collect_with_failure_boundary(
                state,
                lifecycle=lifecycle,
                collect=succeed_then_fail_collect,
            )

            output = result["stage_outputs"]["genome_survey"]
            self.assertEqual(output["status"], "failed")
            self.assertEqual(output["failure_kind"], "collect")
            event_types = [
                event["event_type"] for event in Ledger(run_dir).read_events("s1")
            ]
            self.assertIn("attempt_succeeded", event_types)
            self.assertNotIn("attempt_failed", event_types)
            self.assertNotIn("stage_failed", event_types)


if __name__ == "__main__":
    unittest.main()
