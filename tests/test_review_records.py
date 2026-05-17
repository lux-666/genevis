"""Tests for shared review decision record helpers."""

from __future__ import annotations

import unittest

from app.review.records import KNOWN_ORIGINS, counts_by_origin, decision_records


class ReviewRecordsTests(unittest.TestCase):
    def test_decision_records_filters_and_defaults_decided_by_to_human(self):
        events = [
            {"event_type": "review_requested", "stage": "assembly_review"},
            {
                "event_type": "review_decision",
                "timestamp": "2026-05-17T00:00:00Z",
                "stage": "assembly_review",
                "decision": "accept",
                "candidate_id": "flye_01",
                "method_name": "assembly.candidate.flye",
                "decided_by": "llm_provisional",
                "rationale_artifact_ref": "/tmp/r.json",
                "artifact_ref": "/tmp/a.json",
            },
            {
                "event_type": "review_decision",
                "stage": "hic_scaffolding",
                "decision": "skip",
            },
        ]
        records = decision_records(events)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["decided_by"], "llm_provisional")
        self.assertEqual(records[0]["candidate_id"], "flye_01")
        self.assertEqual(records[1]["decided_by"], "human")

    def test_counts_by_origin_initializes_known_origins(self):
        records = [
            {"decided_by": "auto_rule"},
            {"decided_by": "auto_rule"},
            {"decided_by": "llm_provisional"},
            {},
        ]
        counts = counts_by_origin(records)
        self.assertEqual(counts["auto_rule"], 2)
        self.assertEqual(counts["llm_provisional"], 1)
        self.assertEqual(counts["human"], 1)
        for origin in KNOWN_ORIGINS:
            self.assertIn(origin, counts)


if __name__ == "__main__":
    unittest.main()
