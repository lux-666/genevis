"""Tests for final review report artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.reports.review_report import write_final_review_report, write_operator_report


class ReviewReportTests(unittest.TestCase):
    def test_final_report_lists_pending_confirmations_first_and_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(tmp)
            ledger.create_session("s1", "manifest.yaml")
            ledger.record_review_requested(
                "s1",
                "hic_scaffolding",
                reason="hic_scaffolding_human_decision_required",
                review_request_artifact_ref="hic_request.json",
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
                rationale_artifact_ref="assembly_review_recommendation.json",
            )

            report_ref = write_final_review_report(tmp, "s1")

            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))
            self.assertEqual(report["pending_human_confirmations"][0]["stage"], "hic_scaffolding")
            self.assertEqual(report["decisions_by_origin"]["llm_provisional"], 1)
            self.assertEqual(report["decisions"][0]["rationale_artifact_ref"], "assembly_review_recommendation.json")
            artifact_index = Path(tmp) / "artifacts" / "s1" / "artifacts.jsonl"
            self.assertIn("final_report.review_decisions", artifact_index.read_text(encoding="utf-8"))

    def test_failed_operator_report_is_registered_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            Ledger(tmp).create_session("s1", "manifest.yaml")
            report_ref = write_operator_report(
                tmp,
                "s1",
                {
                    "current_stage": "function_annotation",
                    "supervision_state": "failed_terminal",
                    "last_error": "missing database_provenance for function_annotation",
                    "route_trace": ["gene_annotation_stage", "function_annotation_stage"],
                },
            )

            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))
            self.assertEqual(report["next_action"]["kind"], "provenance_required")
            self.assertEqual(report["route_trace_tail"], ["gene_annotation_stage", "function_annotation_stage"])
            artifact_index = Path(tmp) / "artifacts" / "s1" / "artifacts.jsonl"
            self.assertIn("operator_report.failed_terminal", artifact_index.read_text(encoding="utf-8"))
            self.assertIn("database_source_ref", report["next_action"]["required_fields"])

    def test_failed_operator_report_points_to_materialized_proteins_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            Ledger(tmp).create_session("s1", "manifest.yaml")
            report_ref = write_operator_report(
                tmp,
                "s1",
                {
                    "current_stage": "function_annotation",
                    "supervision_state": "failed_terminal",
                    "last_error": "protein artifact not found for function_annotation: /tmp/missing.fa",
                    "route_trace": ["gene_annotation_stage", "function_annotation_stage"],
                },
            )

            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))
            next_action = report["next_action"]
            self.assertEqual(next_action["kind"], "materialized_proteins_required")
            self.assertEqual(next_action["manifest_asset_type"], "predicted_proteins")
            self.assertIn("manifest.assets[predicted_proteins].paths[0]", next_action["required_fields"])
            self.assertIn("function_annotation.database_provenance[*].input_proteins_ref", next_action["required_fields"])
            self.assertEqual(
                next_action["run_config_path"],
                "function_annotation.database_provenance[*].input_proteins_ref",
            )

    def test_blocked_operator_report_compacts_active_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            Ledger(tmp).create_session("s1", "manifest.yaml")
            report_ref = write_operator_report(
                tmp,
                "s1",
                {
                    "current_stage": "assembly_review",
                    "supervision_state": "blocked_human",
                    "checkpoint_persistence": "sqlite",
                    "active_interrupt": {
                        "stage": "assembly_review",
                        "reason": "needs_review",
                        "decisions_allowed": ["accept"],
                        "candidates": [{"candidate_id": "flye", "large_unused": "x"}],
                        "report_refs": {"assembly_review_report": "report.md"},
                        "candidate_comparison_context": {"candidate_ids": ["flye"]},
                    },
                    "route_trace": ["assembly_review_stage"],
                },
            )

            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))
            self.assertEqual(report["active_interrupt"]["candidate_ids"], ["flye"])
            self.assertEqual(report["active_interrupt"]["report_refs"]["assembly_review_report"], "report.md")
            self.assertEqual(report["active_interrupt"]["candidate_comparison_context"]["candidate_ids"], ["flye"])
            self.assertEqual(report["next_action"]["kind"], "review_decision_required")
            artifact_index = Path(tmp) / "artifacts" / "s1" / "artifacts.jsonl"
            self.assertIn("operator_report.blocked_human", artifact_index.read_text(encoding="utf-8"))

    def test_blocked_operator_report_records_later_gate_without_overwriting_prior_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            Ledger(tmp).create_session("s1", "manifest.yaml")
            first_ref = write_operator_report(
                tmp,
                "s1",
                {
                    "current_stage": "assembly_review",
                    "supervision_state": "blocked_human",
                    "checkpoint_persistence": "sqlite",
                    "active_interrupt": {
                        "stage": "assembly_review",
                        "reason": "needs_review",
                        "decisions_allowed": ["accept"],
                        "candidates": [{"candidate_id": "flye"}],
                    },
                },
            )
            second_ref = write_operator_report(
                tmp,
                "s1",
                {
                    "current_stage": "hic_scaffolding",
                    "supervision_state": "blocked_human",
                    "checkpoint_persistence": "sqlite",
                    "active_interrupt": {
                        "stage": "hic_scaffolding",
                        "reason": "needs_review",
                        "decisions_allowed": ["select_method"],
                        "candidate_methods": ["hic_scaffolding.candidate.yahs"],
                    },
                },
            )
            repeated_ref = write_operator_report(
                tmp,
                "s1",
                {
                    "current_stage": "hic_scaffolding",
                    "supervision_state": "blocked_human",
                    "checkpoint_persistence": "sqlite",
                    "active_interrupt": {
                        "stage": "hic_scaffolding",
                        "reason": "needs_review",
                        "decisions_allowed": ["select_method"],
                        "candidate_methods": ["hic_scaffolding.candidate.yahs"],
                    },
                },
            )

            first = json.loads(Path(first_ref).read_text(encoding="utf-8"))
            second = json.loads(Path(second_ref).read_text(encoding="utf-8"))
            artifact_index = Path(tmp) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
            self.assertNotEqual(first_ref, second_ref)
            self.assertEqual(first["current_stage"], "assembly_review")
            self.assertEqual(second["current_stage"], "hic_scaffolding")
            self.assertEqual(second_ref, repeated_ref)
            self.assertEqual(artifact_types.count("operator_report.blocked_human"), 2)


if __name__ == "__main__":
    unittest.main()
