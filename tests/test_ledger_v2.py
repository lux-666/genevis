"""Data Architecture v2 ledger contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.storage.ledger import (
    LEDGER_SCHEMA_VERSION,
    SUMMARY_MAX_BYTES,
    Ledger,
    LedgerConflictError,
)
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout


def _run(tmp: str, session_id: str = "s1") -> SessionPaths:
    paths = SessionPaths(tmp, session_id)
    ensure_run_layout(paths)
    paths.manifest_path.write_text("session_id: s1\n", encoding="utf-8")
    return paths


class LedgerV2Tests(unittest.TestCase):
    def test_session_and_events_are_one_idempotent_v2_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            ledger.create_session("s1", str(paths.manifest_path))
            ledger.record_stage_entered(
                "s1",
                "read_qc",
                route_node="route_controller",
                idempotency_key="route:read_qc:entered",
            )
            ledger.record_stage_entered(
                "s1",
                "read_qc",
                route_node="route_controller",
                idempotency_key="route:read_qc:entered",
            )

            rows = ledger.read_event_rows("s1")
            self.assertEqual([row["sequence"] for row in rows], [1, 2])
            self.assertTrue(
                all(row["schema_version"] == LEDGER_SCHEMA_VERSION for row in rows)
            )
            self.assertTrue(all(row["event_id"].startswith("evt_") for row in rows))
            self.assertTrue(all(row["idempotency_key"] for row in rows))
            self.assertFalse((paths.ledger_dir / "session.json").exists())
            self.assertEqual(
                ledger.read_session("s1")["input_manifest_ref"],
                "control/manifest.yaml",
            )

    def test_reusing_idempotency_key_for_different_fact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            ledger.record_event(
                "s1",
                "stage_skipped",
                {"stage": "hic_scaffolding", "reason": "not_requested"},
                idempotency_key="skip:hic",
            )
            with self.assertRaises(LedgerConflictError):
                ledger.record_event(
                    "s1",
                    "stage_skipped",
                    {"stage": "hic_scaffolding", "reason": "missing_input"},
                    idempotency_key="skip:hic",
                )

    def test_large_collections_are_counts_and_refs_not_embedded_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            request = paths.artifact_path("reviews", "r1", "request.json")
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text("{}", encoding="utf-8")
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            ledger.record_review_requested(
                "s1",
                "assembly",
                reason="approval_required",
                signals=[{"large": "x" * 10_000}],
                candidates=[{"candidate_id": "flye", "large": "y" * 10_000}],
                review_request_artifact_ref={
                    "schema_version": 2,
                    "artifact_id": "art_" + "1" * 32,
                    "path_ref": "artifacts/reviews/r1/request.json",
                    "sha256": "a" * 64,
                    "size_bytes": request.stat().st_size,
                    "artifact_type": "review.request",
                },
                review_request_id="review_001",
                pending_human_confirmation=True,
            )

            row = ledger.read_event_rows("s1")[-1]
            serialized = json.dumps(row, ensure_ascii=False)
            self.assertNotIn('"signals"', serialized)
            self.assertNotIn('"candidates"', serialized)
            self.assertEqual(row["summary"]["signal_count"], 1)
            self.assertEqual(row["summary"]["candidate_count"], 1)
            self.assertEqual(
                row["refs"][0]["path_ref"], "artifacts/reviews/r1/request.json"
            )

    def test_review_decisions_bind_to_request_id_not_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
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
                    pending_human_confirmation=True,
                )
            ledger.record_review_decision(
                "s1",
                "assembly_review",
                decision="accept",
                review_request_id="review_001",
            )

            decisions = [
                event
                for event in ledger.read_events("s1")
                if event["event_type"] == "review_decision"
            ]
            self.assertEqual(decisions[0]["subject_id"], "review_001")
            self.assertEqual(decisions[0]["review_request_id"], "review_001")

    def test_summary_is_bounded_and_external_absolute_ref_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            ledger.record_event("s1", "run_failed", {"error": "boom" * 100_000})
            row = ledger.read_event_rows("s1")[-1]
            size = len(json.dumps(row["summary"], ensure_ascii=False).encode("utf-8"))
            self.assertLessEqual(size, SUMMARY_MAX_BYTES)
            with self.assertRaises(DataLayoutError):
                ledger.record_event(
                    "s1",
                    "artifact_registered",
                    {"artifact_ref": str(Path(tmp).parent / "outside.json")},
                )

    def test_route_trace_summary_keeps_latest_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            route_trace = [f"route_{index}" for index in range(ROUTE_TRACE_LIMIT + 4)]

            ledger.record_event("s1", "route_snapshot", {"route_trace": route_trace})

            row = ledger.read_event_rows("s1")[-1]
            self.assertEqual(
                row["summary"]["route_trace"],
                route_trace[-ROUTE_TRACE_LIMIT:],
            )

    def test_stage_failure_event_carries_durable_evidence_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            evidence = paths.artifact_path("read_qc", "failure.json")
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("{}\n", encoding="utf-8")
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))

            ledger.record_stage_failed(
                "s1",
                "read_qc",
                error="tool failed",
                refs=[{"path_ref": paths.to_ref(evidence), "role": "execution"}],
            )

            event = ledger.read_events("s1")[-1]
            self.assertEqual(event["event_type"], "stage_failed")
            self.assertEqual(event["error"], "tool failed")
            self.assertEqual(event["refs"][0]["path_ref"], paths.to_ref(evidence))


if __name__ == "__main__":
    unittest.main()
