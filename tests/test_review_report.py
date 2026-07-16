"""Tests for v2 derived current-status and session-summary reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError, inspect_state, resume_review_state
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import persist_review_request
from app.reports.review_report import write_final_review_report, write_operator_report
from app.storage.paths import SessionPaths, ensure_run_layout


def _run(tmp: str, session_id: str = "s1") -> SessionPaths:
    paths = SessionPaths(tmp, session_id)
    ensure_run_layout(paths)
    paths.manifest_path.write_text("session_id: s1\n", encoding="utf-8")
    Ledger(paths.run_dir).create_session(session_id, str(paths.manifest_path))
    return paths


def _request(
    paths: SessionPaths,
    *,
    stage: str,
    gate_id: str,
    decisions_allowed: list[str],
):
    state = {
        "session_id": paths.session_id,
        "data_dir": str(paths.run_dir),
    }
    payload = ReviewPayload(
        stage=stage,
        gate_id=gate_id,
        reason="needs_review",
        decisions_allowed=decisions_allowed,
        auto_decidable=False,
    )
    return persist_review_request(
        state,
        stage=stage,
        gate_id=gate_id,
        reason="needs_review",
        payload=payload,
    )


class ReviewReportTests(unittest.TestCase):
    def test_inspect_loads_decision_requirements_from_verified_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            persisted = _request(
                paths,
                stage="assembly_review",
                gate_id="assembly_review",
                decisions_allowed=["accept", "reject"],
            )
            interrupt = persisted.compact_interrupt(
                stage="assembly_review",
                gate_id="assembly_review",
                reason="needs_review",
            )
            self.assertNotIn("decisions_allowed", interrupt)

            summary = inspect_state(
                {
                    "session_id": "s1",
                    "data_dir": str(paths.run_dir),
                    "current_stage": "assembly_review",
                    "supervision_state": "blocked_human",
                    "active_interrupt": interrupt,
                    "stage_outputs": {},
                }
            )

            self.assertEqual(
                summary["decision_requirements"]["decisions_allowed"],
                ["accept", "reject"],
            )
            self.assertEqual(
                summary["decision_requirements"]["review_request_id"],
                persisted.review_request_id,
            )

    def test_resume_fails_closed_on_request_identity_or_integrity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            persisted = _request(
                paths,
                stage="assembly_review",
                gate_id="assembly_review",
                decisions_allowed=["accept"],
            )
            state = {
                "session_id": "s1",
                "data_dir": str(paths.run_dir),
                "current_stage": "assembly_review",
                "supervision_state": "blocked_human",
                "active_interrupt": persisted.compact_interrupt(
                    stage="assembly_review",
                    gate_id="assembly_review",
                    reason="needs_review",
                ),
                "stage_outputs": {},
            }
            wrong_hash = ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                gate_id="assembly_review",
                review_request_id=persisted.review_request_id,
                review_request_hash="0" * 64,
                decision="accept",
            )
            with self.assertRaisesRegex(ResumeValidationError, "review_request_hash"):
                resume_review_state(state, wrong_hash)

            request = ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                gate_id="assembly_review",
                review_request_id=persisted.review_request_id,
                review_request_hash=persisted.request_hash,
                decision="accept",
            )
            paths.resolve_ref(persisted.request_ref.path_ref).write_text(
                "{\"tampered\": true}",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ResumeValidationError, "integrity mismatch"):
                resume_review_state(state, request)

    def test_unified_summary_lists_pending_confirmations_and_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            persisted = _request(
                paths,
                stage="hic_scaffolding",
                gate_id="hic_scaffolding",
                decisions_allowed=["select_method", "skip"],
            )
            rationale = paths.artifact_path("reviews", "assembly_001", "rationale.json")
            rationale.parent.mkdir(parents=True, exist_ok=True)
            rationale.write_text("{}", encoding="utf-8")
            ledger.record_review_requested(
                "s1",
                "hic_scaffolding",
                reason="hic_scaffolding_human_decision_required",
                review_request_artifact_ref=persisted.request_ref,
                review_request_id=persisted.review_request_id,
                auto_decidable=False,
                policy_mode="auto",
                pending_human_confirmation=True,
            )
            ledger.record_review_decision(
                "s1",
                "assembly_review",
                decision="accept",
                candidate_id="flye",
                decided_by="llm_provisional",
                rationale_artifact_ref=str(rationale),
                review_request_id="assembly_001",
            )

            before_events = len(ledger.read_event_rows("s1"))
            before_index = paths.artifact_index_path.read_bytes()
            report_ref = write_final_review_report(paths.run_dir, "s1")
            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))

            self.assertEqual(Path(report_ref), paths.report_path("session_summary.json"))
            self.assertEqual(report["report_type"], "session_summary")
            self.assertEqual(
                report["pending_human_confirmations"][0]["review_request_id"],
                persisted.review_request_id,
            )
            self.assertEqual(report["decisions_by_origin"]["llm_provisional"], 1)
            self.assertEqual(report["decisions"][0]["review_request_id"], "assembly_001")
            self.assertEqual(len(ledger.read_event_rows("s1")), before_events)
            self.assertEqual(paths.artifact_index_path.read_bytes(), before_index)

    def test_failed_current_status_is_overwritten_outside_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_session_terminal(
                "s1",
                outcome="failed",
                stage="function_annotation",
                error="missing database_provenance for function_annotation",
            )
            before_events = len(ledger.read_event_rows("s1"))
            report_ref = write_operator_report(
                paths.run_dir,
                "s1",
                {
                    "current_stage": "function_annotation",
                    "supervision_state": "failed_terminal",
                    "last_error": "missing database_provenance for function_annotation",
                    "route_trace": ["gene_annotation_stage", "function_annotation_stage"],
                },
            )

            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))
            self.assertEqual(Path(report_ref), paths.report_path("current_status.json"))
            self.assertEqual(report["report_type"], "current_status")
            self.assertEqual(report["next_action"]["kind"], "provenance_required")
            self.assertEqual(
                report["route_trace_tail"],
                ["gene_annotation_stage", "function_annotation_stage"],
            )
            self.assertIn("database_source_ref", report["next_action"]["required_fields"])
            self.assertEqual(len(ledger.read_event_rows("s1")), before_events)
            self.assertFalse(paths.artifact_index_path.exists())

    def test_blocked_current_status_compacts_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            persisted = _request(
                paths,
                stage="assembly_review",
                gate_id="assembly_review",
                decisions_allowed=["accept"],
            )
            report_ref = write_operator_report(
                paths.run_dir,
                "s1",
                {
                    "current_stage": "assembly_review",
                    "supervision_state": "blocked_human",
                    "checkpoint_persistence": "sqlite",
                    "active_interrupt": persisted.compact_interrupt(
                        stage="assembly_review",
                        gate_id="assembly_review",
                        reason="needs_review",
                    ),
                    "route_trace": ["assembly_review_stage"],
                },
            )

            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))
            interrupt = report["active_interrupt"]
            self.assertEqual(interrupt["review_request_id"], persisted.review_request_id)
            self.assertNotIn("candidates", interrupt)
            self.assertEqual(
                interrupt["request_ref"],
                persisted.request_ref.path_ref,
            )
            self.assertEqual(report["next_action"]["kind"], "review_decision_required")

    def test_later_status_replaces_one_fixed_file_without_audit_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            assembly_request = _request(
                paths,
                stage="assembly_review",
                gate_id="assembly_review",
                decisions_allowed=["accept"],
            )
            hic_request = _request(
                paths,
                stage="hic_scaffolding",
                gate_id="hic_scaffolding",
                decisions_allowed=["select_method"],
            )
            before_events = len(ledger.read_event_rows("s1"))
            before_artifacts = paths.artifact_index_path.read_bytes()
            first_ref = write_operator_report(
                paths.run_dir,
                "s1",
                {
                    "current_stage": "assembly_review",
                    "supervision_state": "blocked_human",
                    "active_interrupt": assembly_request.compact_interrupt(
                        stage="assembly_review",
                        gate_id="assembly_review",
                        reason="needs_review",
                    ),
                },
            )
            second_ref = write_operator_report(
                paths.run_dir,
                "s1",
                {
                    "current_stage": "hic_scaffolding",
                    "supervision_state": "blocked_human",
                    "active_interrupt": hic_request.compact_interrupt(
                        stage="hic_scaffolding",
                        gate_id="hic_scaffolding",
                        reason="needs_review",
                    ),
                },
            )

            self.assertEqual(first_ref, second_ref)
            current = json.loads(Path(second_ref).read_text(encoding="utf-8"))
            self.assertEqual(current["current_stage"], "hic_scaffolding")
            self.assertEqual(len(list(paths.reports_dir.iterdir())), 1)
            self.assertEqual(len(ledger.read_event_rows("s1")), before_events)
            self.assertEqual(paths.artifact_index_path.read_bytes(), before_artifacts)


if __name__ == "__main__":
    unittest.main()
