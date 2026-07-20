"""Tests for the v2 session-summary projection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.storage.ledger import Ledger
from app.control.lifecycle import inspect_session
from app.storage.artifacts import read_artifact_records, register_artifact_records
from app.reports.trace_writer import (
    SESSION_SUMMARY_JSON_FILENAME,
    SESSION_SUMMARY_MARKDOWN_FILENAME,
    STAGE_STATUS_BLOCKED_HUMAN,
    STAGE_STATUS_EXECUTED,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_NO_EVIDENCE,
    STAGE_STATUS_PLANNED_ONLY,
    STAGE_STATUS_SKIPPED,
    build_session_trace,
    write_session_trace,
)
from app.review.base import ReviewPayload
from app.review.persistence import persist_review_request
from app.schemas.artifacts import ArtifactMeta
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.storage.paths import SessionPaths, ensure_run_layout


def _run(tmp: str, session_id: str = "s1", *, create: bool = True) -> SessionPaths:
    paths = SessionPaths(tmp, session_id)
    ensure_run_layout(paths)
    if create:
        paths.manifest_path.write_text("session_id: s1\n", encoding="utf-8")
        Ledger(paths.run_dir).create_session(session_id, str(paths.manifest_path))
    return paths


def _attempt(
    ledger: Ledger,
    session_id: str,
    stage: str,
    method: str,
    outcome: str,
) -> None:
    attempt_id = f"{stage}:attempt_000"
    ledger.record_event(
        session_id,
        "attempt_started",
        {"attempt_id": attempt_id, "stage": stage, "method_name": method},
    )
    if outcome == "succeeded":
        ledger.record_event(
            session_id,
            "attempt_succeeded",
            {"attempt_id": attempt_id, "stage": stage, "method_name": method},
        )
    elif outcome == "failed":
        ledger.record_event(
            session_id,
            "attempt_failed",
            {
                "attempt_id": attempt_id,
                "stage": stage,
                "method_name": method,
                "return_code": 1,
                "error": "tool boom\nERROR conda.cli.main_run:execute(142): failed",
            },
        )


def _register_artifact(
    paths: SessionPaths,
    *,
    stage: str,
    method: str,
    artifact_type: str,
    payload: dict | None = None,
    metadata: dict | None = None,
) -> Path:
    file_path = paths.artifact_path(
        stage,
        "evidence",
        f"{artifact_type.replace('.', '_')}.json",
    )
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload or {}), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        file_path,
        stage=stage,
        method_name=method,
        artifact_type=artifact_type,
        metadata=metadata or {},
    )
    register_artifact_records(paths.run_dir, paths.session_id, [artifact])
    return file_path


def _stage(trace: dict, name: str) -> dict:
    return next(stage for stage in trace["stages"] if stage["stage"] == name)


class SessionSummaryBuildTests(unittest.TestCase):
    def test_inspect_route_trace_keeps_only_latest_sixteen_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            for index in range(ROUTE_TRACE_LIMIT + 4):
                ledger.record_stage_entered(
                    "s1",
                    f"stage_{index}",
                    route_node=f"route_{index}",
                    idempotency_key=f"route-test-{index}",
                )

            summary = inspect_session("s1", paths.run_dir)

            self.assertEqual(
                summary["route_trace"],
                [f"route_{index}" for index in range(4, ROUTE_TRACE_LIMIT + 4)],
            )

    def test_attempt_and_request_bound_decision_are_reconstructed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_stage_entered("s1", "read_qc")
            _attempt(
                ledger,
                "s1",
                "read_qc",
                "read_qc.long_read_stats.seqkit_stats",
                "succeeded",
            )
            _register_artifact(
                paths,
                stage="read_qc",
                method="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )
            request = _register_artifact(
                paths,
                stage="assembly_review",
                method="assembly_review.request",
                artifact_type="assembly_review.request",
                payload={"stage": "assembly_review", "decisions_allowed": ["accept"]},
                metadata={"review_request_id": "review_001"},
            )
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="operator_required",
                review_request_artifact_ref=str(request),
                review_request_id="review_001",
            )
            ledger.record_review_decision(
                "s1",
                "assembly_review",
                decision="accept",
                candidate_id="flye",
                decided_by="human",
                review_request_id="review_001",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(_stage(trace, "read_qc")["status"], STAGE_STATUS_EXECUTED)
            self.assertEqual(
                _stage(trace, "inventory")["status"], STAGE_STATUS_NO_EVIDENCE
            )
            self.assertEqual(trace["decisions"][0]["review_request_id"], "review_001")
            self.assertEqual(trace["decisions_by_origin"]["human"], 1)
            self.assertEqual(
                _stage(trace, "read_qc")["methods"][0]["outcome"], "succeeded"
            )

    def test_candidate_evidence_without_attempt_is_planned_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            _register_artifact(
                paths,
                stage="assembly",
                method="assembly.candidate.nextdenovo",
                artifact_type="draft_assembly.candidate",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            assembly = _stage(trace, "assembly")
            self.assertEqual(assembly["status"], STAGE_STATUS_PLANNED_ONLY)
            self.assertEqual(
                assembly["candidate_methods"][0]["method_name"],
                "assembly.candidate.nextdenovo",
            )

    def test_terminal_failure_is_reconstructed_without_current_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            _attempt(ledger, "s1", "assembly", "assembly.candidate.flye", "failed")
            ledger.record_session_terminal(
                "s1",
                outcome="failed",
                stage="assembly",
                error="flye failed",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(_stage(trace, "assembly")["status"], STAGE_STATUS_FAILED)
            self.assertEqual(trace["supervision_state"], "failed_terminal")
            self.assertEqual(
                trace["final_or_current_stage"],
                {"stage": "assembly", "source": "ledger"},
            )
            self.assertEqual(trace["failures"][0]["error"], "tool boom")
            self.assertNotIn("conda.cli.main_run", trace["failures"][0]["error"])
            self.assertFalse(paths.report_path("current_status.json").exists())

    def test_blocked_request_is_loaded_from_immutable_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            persisted = persist_review_request(
                {"session_id": "s1", "data_dir": str(paths.run_dir)},
                stage="assembly_review",
                gate_id="assembly_review",
                reason="assembly_review_human_decision_required",
                payload=ReviewPayload(
                    stage="assembly_review",
                    gate_id="assembly_review",
                    reason="assembly_review_human_decision_required",
                    decisions_allowed=["accept", "reject"],
                    candidates=[{"candidate_id": "flye"}],
                ),
            )
            ledger = Ledger(paths.run_dir)
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="assembly_review_human_decision_required",
                review_request_artifact_ref=persisted.request_ref,
                review_request_id=persisted.review_request_id,
                auto_decidable=False,
                pending_human_confirmation=True,
            )

            trace = build_session_trace(paths.run_dir, "s1")
            summary = inspect_session("s1", paths.run_dir)

            self.assertEqual(trace["supervision_state"], "blocked_human")
            self.assertEqual(
                _stage(trace, "assembly_review")["status"], STAGE_STATUS_BLOCKED_HUMAN
            )
            self.assertEqual(
                trace["blocked"]["decisions_allowed"], ["accept", "reject"]
            )
            self.assertEqual(
                summary["active_interrupt"]["review_request_id"],
                persisted.review_request_id,
            )
            self.assertIsNone(summary["latest_operator_report"])

    def test_explicit_route_and_skip_reason_have_no_checkpoint_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_stage_entered("s1", "read_qc", route_node="route_controller")
            ledger.record_stage_completed("s1", "read_qc")
            ledger.record_stage_skipped("s1", "genome_survey", reason="not_requested")

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                [event["event_type"] for event in trace["route"]],
                ["stage_entered", "stage_completed", "stage_skipped"],
            )
            self.assertEqual(
                _stage(trace, "genome_survey")["status"], STAGE_STATUS_SKIPPED
            )
            self.assertEqual(
                _stage(trace, "genome_survey")["lifecycle"]["skip_reason"],
                "not_requested",
            )
            self.assertNotIn("route", {gap["field"] for gap in trace["gaps"]})

    def test_multiple_requests_in_one_stage_resolve_by_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            for request_id in ("review_001", "review_002"):
                request = paths.artifact_path("reviews", request_id, "request.json")
                request.parent.mkdir(parents=True, exist_ok=True)
                request.write_text("{}", encoding="utf-8")
                ledger.record_review_requested(
                    "s1",
                    "assembly_review",
                    reason="operator_required",
                    review_request_artifact_ref=str(request),
                    review_request_id=request_id,
                    auto_decidable=False,
                    pending_human_confirmation=True,
                )
            ledger.record_review_decision(
                "s1",
                "assembly_review",
                decision="accept",
                review_request_id="review_001",
            )

            trace = build_session_trace(paths.run_dir, "s1")
            pending = {
                item["review_request_id"]: item["resolved"]
                for item in trace["pending_human_confirmations"]
            }

            self.assertEqual(pending, {"review_001": True, "review_002": False})
            self.assertNotIn("review_requests", {gap["field"] for gap in trace["gaps"]})


class SessionSummaryWriteTests(unittest.TestCase):
    def test_write_uses_reports_only_and_has_no_audit_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            _attempt(
                ledger,
                "s1",
                "read_qc",
                "read_qc.long_read_stats.seqkit_stats",
                "succeeded",
            )
            before_events = len(ledger.read_event_rows("s1"))
            before_artifacts = read_artifact_records(paths.run_dir, "s1")

            result = write_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                Path(result["json_ref"]),
                paths.report_path(SESSION_SUMMARY_JSON_FILENAME),
            )
            self.assertEqual(
                Path(result["markdown_ref"]),
                paths.report_path(SESSION_SUMMARY_MARKDOWN_FILENAME),
            )
            self.assertIn("# Session Summary: s1", result["markdown"])
            self.assertEqual(len(ledger.read_event_rows("s1")), before_events)
            self.assertEqual(
                read_artifact_records(paths.run_dir, "s1"), before_artifacts
            )

    def test_regeneration_overwrites_fixed_views_and_updates_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            first = write_session_trace(paths.run_dir, "s1")
            first_payload = json.loads(
                Path(first["json_ref"]).read_text(encoding="utf-8")
            )
            ledger.record_stage_entered("s1", "read_qc")
            second = write_session_trace(paths.run_dir, "s1")
            second_payload = json.loads(
                Path(second["json_ref"]).read_text(encoding="utf-8")
            )

            self.assertEqual(first["json_ref"], second["json_ref"])
            self.assertEqual(len(list(paths.reports_dir.iterdir())), 2)
            self.assertGreater(
                second_payload["source_watermark"]["last_ledger_sequence"],
                first_payload["source_watermark"]["last_ledger_sequence"],
            )

    def test_artifact_count_does_not_inflate_across_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            _register_artifact(
                paths,
                stage="read_qc",
                method="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )

            counts = [
                write_session_trace(paths.run_dir, "s1")["trace"]["artifact_count"]
                for _ in range(3)
            ]

            self.assertEqual(counts, [1, 1, 1])
            self.assertEqual(len(read_artifact_records(paths.run_dir, "s1")), 1)

    def test_missing_session_writes_no_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp, "missing", create=False)

            result = write_session_trace(paths.run_dir, "missing")

            self.assertIsNone(result["json_ref"])
            self.assertIsNone(result["markdown_ref"])
            self.assertIn("# Session Summary: missing", result["markdown"])
            self.assertEqual(list(paths.reports_dir.iterdir()), [])
            self.assertFalse(paths.ledger_events_path.exists())


if __name__ == "__main__":
    unittest.main()
