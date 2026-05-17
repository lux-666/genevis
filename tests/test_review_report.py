"""Tests for final review report artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.reports.review_report import write_final_review_report


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


if __name__ == "__main__":
    unittest.main()
