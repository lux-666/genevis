"""Tests for the durable session run trace."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records, read_artifact_records
from app.reports.trace_writer import (
    STAGE_STATUS_BLOCKED_HUMAN,
    STAGE_STATUS_EXECUTED,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_NO_EVIDENCE,
    STAGE_STATUS_PLANNED_ONLY,
    TRACE_JSON_ARTIFACT_TYPE,
    TRACE_MARKDOWN_ARTIFACT_TYPE,
    build_session_trace,
    write_session_trace,
)
from app.schemas.artifacts import ArtifactMeta


def _attempt(ledger: Ledger, session_id: str, stage: str, method: str, outcome: str) -> None:
    ledger.record_event(session_id, "attempt_started", {"attempt_id": f"{session_id}:{stage}:{method}", "stage": stage, "method_name": method})
    if outcome == "succeeded":
        ledger.record_event(session_id, "attempt_succeeded", {"attempt_id": f"{session_id}:{stage}:{method}", "stage": stage, "method_name": method, "artifacts": []})
    elif outcome == "failed":
        ledger.record_event(session_id, "attempt_failed", {"attempt_id": f"{session_id}:{stage}:{method}", "stage": stage, "method_name": method, "return_code": 1, "error": "tool boom\nERROR conda.cli.main_run:execute(142): failed"})


def _register_artifact(tmp: str, session_id: str, *, stage: str, method: str, artifact_type: str, metadata: dict | None = None) -> Path:
    out_dir = Path(tmp) / "artifacts" / stage / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"{artifact_type.replace('.', '_')}.json"
    file_path.write_text("{}", encoding="utf-8")
    artifact = ArtifactMeta.from_file(file_path, stage=stage, method_name=method, artifact_type=artifact_type, metadata=metadata or {})
    append_artifact_records(tmp, session_id, [artifact])
    return file_path


def _stage(trace: dict, name: str) -> dict:
    return next(stage for stage in trace["stages"] if stage["stage"] == name)


class RunTraceBuildTests(unittest.TestCase):
    def test_executed_and_decision_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            _attempt(ledger, "s1", "read_qc", "read_qc.long_read_stats.seqkit_stats", "succeeded")
            _register_artifact(tmp, "s1", stage="read_qc", method="read_qc.long_read_stats.seqkit_stats", artifact_type="read_qc.long_read_stats")
            ledger.record_review_decision("s1", "assembly_review", decision="accept", candidate_id="flye", decided_by="human", rationale_artifact_ref="rationale.json")

            trace = build_session_trace(tmp, "s1")

            self.assertEqual(_stage(trace, "read_qc")["status"], STAGE_STATUS_EXECUTED)
            self.assertEqual(_stage(trace, "inventory")["status"], STAGE_STATUS_NO_EVIDENCE)
            self.assertEqual(trace["decisions"][0]["decision"], "accept")
            self.assertEqual(trace["decisions"][0]["decided_by"], "human")
            self.assertEqual(trace["decisions_by_origin"]["human"], 1)
            self.assertIn("read_qc", trace["stage_path_observed"])
            self.assertEqual(_stage(trace, "read_qc")["methods"][0]["outcome"], "succeeded")

    def test_planned_only_when_candidate_without_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            _register_artifact(
                tmp,
                "s1",
                stage="assembly",
                method="assembly.candidate.nextdenovo",
                artifact_type="draft_assembly.candidate",
                metadata={"candidate_id": "s1:assembly.candidate.nextdenovo", "execution_enabled": False},
            )

            trace = build_session_trace(tmp, "s1")

            assembly = _stage(trace, "assembly")
            self.assertEqual(assembly["status"], STAGE_STATUS_PLANNED_ONLY)
            self.assertEqual(assembly["candidate_methods"][0]["execution_enabled"], False)

    def test_failed_stage_surfaces_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            _attempt(ledger, "s1", "read_qc", "read_qc.long_read_stats.seqkit_stats", "succeeded")
            _attempt(ledger, "s1", "assembly", "assembly.candidate.flye", "failed")
            from app.reports.review_report import write_operator_report

            write_operator_report(tmp, "s1", {"current_stage": "assembly", "supervision_state": "failed_terminal", "last_error": "flye failed", "route_trace": ["assembly_stage"]})

            trace = build_session_trace(tmp, "s1")

            self.assertEqual(_stage(trace, "assembly")["status"], STAGE_STATUS_FAILED)
            self.assertEqual(trace["supervision_state"], "failed_terminal")
            self.assertEqual(trace["final_or_current_stage"]["stage"], "assembly")
            self.assertEqual(trace["failures"][0]["stage"], "assembly")
            self.assertEqual(trace["failures"][0]["error"], "tool boom")
            self.assertNotIn("conda.cli.main_run", trace["failures"][0]["error"])


class RunTraceBlockedTests(unittest.TestCase):
    def _seed_blocked_session(self, tmp: str) -> None:
        ledger = Ledger(tmp)
        ledger.create_session("s1", "manifest.yaml")
        _attempt(ledger, "s1", "assembly", "assembly.candidate.flye", "succeeded")
        request_dir = Path(tmp) / "artifacts" / "assembly_review" / "s1"
        request_dir.mkdir(parents=True, exist_ok=True)
        request_path = request_dir / "assembly_review_request.json"
        request_path.write_text(
            json.dumps({"stage": "assembly_review", "decisions_allowed": ["accept", "reject"], "candidates": [{"candidate_id": "flye"}]}),
            encoding="utf-8",
        )
        artifact = ArtifactMeta.from_file(
            request_path,
            stage="assembly_review",
            method_name="assembly_review.request",
            artifact_type="assembly_review.request",
            metadata={},
        )
        append_artifact_records(tmp, "s1", [artifact])
        ledger.record_review_requested(
            "s1",
            "assembly_review",
            reason="assembly_review_human_decision_required",
            review_request_artifact_ref=str(request_path),
            auto_decidable=False,
            pending_human_confirmation=True,
        )

    def test_blocked_session_names_pending_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_blocked_session(tmp)

            trace = build_session_trace(tmp, "s1")

            self.assertEqual(trace["supervision_state"], "blocked_human")
            self.assertEqual(_stage(trace, "assembly_review")["status"], STAGE_STATUS_BLOCKED_HUMAN)
            self.assertEqual(_stage(trace, "assembly")["status"], STAGE_STATUS_EXECUTED)
            self.assertIsNotNone(trace["blocked"])
            self.assertEqual(trace["blocked"]["stage"], "assembly_review")
            self.assertIn("accept", trace["blocked"]["decisions_allowed"])


class RunTraceGapTests(unittest.TestCase):
    def test_route_trace_and_skip_reason_are_flagged_checkpoint_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            Ledger(tmp).create_session("s1", "manifest.yaml")
            trace = build_session_trace(tmp, "s1")
            gap_fields = {gap["field"]: gap["kind"] for gap in trace["gaps"]}
            self.assertEqual(gap_fields["route_trace"], "checkpoint_only")
            self.assertEqual(gap_fields["skip_reasons"], "checkpoint_only")

    def test_missing_session_reports_error_and_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = build_session_trace(tmp, "missing")
            self.assertEqual(trace["error"], "session_not_found")
            self.assertFalse(trace["session_found"])
            self.assertIn("session", {gap["field"] for gap in trace["gaps"]})

    def test_multiple_requests_per_stage_flagged_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            ledger.record_review_requested("s1", "assembly_review", reason="first")
            ledger.record_review_requested("s1", "assembly_review", reason="second")
            trace = build_session_trace(tmp, "s1")
            kinds = {gap["field"]: gap["kind"] for gap in trace["gaps"]}
            self.assertEqual(kinds.get("review_requests"), "unsupported")


class RunTraceWriteTests(unittest.TestCase):
    def test_write_registers_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            _attempt(ledger, "s1", "read_qc", "read_qc.long_read_stats.seqkit_stats", "succeeded")

            result = write_session_trace(tmp, "s1")

            self.assertTrue(Path(result["json_ref"]).exists())
            self.assertTrue(Path(result["markdown_ref"]).exists())
            records = read_artifact_records(tmp, "s1")
            types = [record["artifact_type"] for record in records]
            self.assertIn(TRACE_JSON_ARTIFACT_TYPE, types)
            self.assertIn(TRACE_MARKDOWN_ARTIFACT_TYPE, types)
            self.assertIn("# Run Trace: s1", result["markdown"])

    def test_regeneration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            _attempt(ledger, "s1", "read_qc", "read_qc.long_read_stats.seqkit_stats", "succeeded")

            write_session_trace(tmp, "s1")
            write_session_trace(tmp, "s1")

            records = read_artifact_records(tmp, "s1")
            json_rows = [r for r in records if r["artifact_type"] == TRACE_JSON_ARTIFACT_TYPE]
            md_rows = [r for r in records if r["artifact_type"] == TRACE_MARKDOWN_ARTIFACT_TYPE]
            self.assertEqual(len(json_rows), 1)
            self.assertEqual(len(md_rows), 1)
            events = ledger.read_events("s1")
            trace_registrations = [
                e for e in events
                if e.get("event_type") == "artifact_registered"
                and (e.get("artifact") or {}).get("artifact_type", "").startswith("run_trace.")
            ]
            self.assertEqual(len(trace_registrations), 2)

    def test_artifact_count_excludes_trace_artifacts_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            _attempt(ledger, "s1", "read_qc", "read_qc.long_read_stats.seqkit_stats", "succeeded")
            _register_artifact(tmp, "s1", stage="read_qc", method="read_qc.long_read_stats.seqkit_stats", artifact_type="read_qc.long_read_stats")

            first = write_session_trace(tmp, "s1")["trace"]["artifact_count"]
            second = write_session_trace(tmp, "s1")["trace"]["artifact_count"]
            third = write_session_trace(tmp, "s1")["trace"]["artifact_count"]

            # The one workflow artifact is counted; the trace's own json+md never inflate it.
            self.assertEqual([first, second, third], [1, 1, 1])

    def test_missing_session_writes_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = write_session_trace(tmp, "missing")

            self.assertIsNone(result["json_ref"])
            self.assertIsNone(result["markdown_ref"])
            self.assertIn("# Run Trace: missing", result["markdown"])
            self.assertFalse((Path(tmp) / "artifacts" / "run_trace").exists())
            self.assertFalse((Path(tmp) / "ledger" / "missing").exists())


if __name__ == "__main__":
    unittest.main()
