"""Data Architecture v2 ledger contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.entrypoints.run import _record_automatic_review_failure
from app.schemas.artifacts import ArtifactMeta
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.storage.artifacts import register_artifact_records
from app.storage.ledger import (
    LEDGER_SCHEMA_VERSION,
    SUMMARY_MAX_BYTES,
    Ledger,
    LedgerConflictError,
)
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout


def _run(tmp: str, session_id: str = "s1") -> SessionPaths:
    paths = SessionPaths(tmp, session_id)
    ensure_run_layout(paths)
    paths.manifest_path.write_text("session_id: s1\n", encoding="utf-8")
    return paths


def _register_artifact(
    paths: SessionPaths,
    *,
    workflow_step_id: str,
    stage: str,
    parts: tuple[str, ...],
    artifact_type: str,
    metadata: dict[str, str] | None = None,
):
    path = paths.workflow_step_artifact_path(workflow_step_id, stage, *parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        path,
        stage=stage,
        method_name="ledger-test",
        artifact_type=artifact_type,
        metadata=metadata,
    )
    return register_artifact_records(paths.run_dir, paths.session_id, [artifact])[0]


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
                workflow_step_id="step-001-read_qc",
                route_node="route_controller",
                idempotency_key="route:read_qc:entered",
            )
            ledger.record_stage_entered(
                "s1",
                "read_qc",
                workflow_step_id="step-001-read_qc",
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
                stage="hic_scaffolding",
                workflow_step_id="step-001-hic_scaffolding",
                idempotency_key="skip:hic",
            )
            with self.assertRaises(LedgerConflictError):
                ledger.record_event(
                    "s1",
                    "stage_skipped",
                    {"stage": "hic_scaffolding", "reason": "missing_input"},
                    stage="hic_scaffolding",
                    workflow_step_id="step-001-hic_scaffolding",
                    idempotency_key="skip:hic",
                )

    def test_large_collections_are_counts_and_refs_not_embedded_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            request = _register_artifact(
                paths,
                workflow_step_id="step-001-assembly",
                stage="assembly",
                parts=("reviews", "r1", "request.json"),
                artifact_type="review.request",
                metadata={"review_request_id": "review_001"},
            )
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            ledger.record_review_requested(
                "s1",
                "assembly",
                reason="approval_required",
                signals=[{"large": "x" * 10_000}],
                candidates=[{"candidate_id": "flye", "large": "y" * 10_000}],
                review_request_artifact_ref=request,
                review_request_id="review_001",
            )

            row = ledger.read_event_rows("s1")[-1]
            serialized = json.dumps(row, ensure_ascii=False)
            self.assertNotIn('"signals"', serialized)
            self.assertNotIn('"candidates"', serialized)
            self.assertNotIn('"policy_mode"', serialized)
            self.assertNotIn('"pending_human_confirmation"', serialized)
            self.assertEqual(row["summary"]["signal_count"], 1)
            self.assertEqual(row["summary"]["candidate_count"], 1)
            self.assertEqual(
                row["refs"][0]["path_ref"], request.path_ref
            )

    def test_review_decisions_bind_to_request_id_not_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            for request_id in ("review_001", "review_002"):
                request = _register_artifact(
                    paths,
                    workflow_step_id="step-001-assembly_review",
                    stage="assembly_review",
                    parts=("reviews", request_id, "request.json"),
                    artifact_type="review.request",
                    metadata={"review_request_id": request_id},
                )
                ledger.record_review_requested(
                    "s1",
                    "assembly_review",
                    reason="operator_required",
                    review_request_artifact_ref=request,
                    review_request_id=request_id,
                )
            decision = _register_artifact(
                paths,
                workflow_step_id="step-001-assembly_review",
                stage="assembly_review",
                parts=("reviews", "review_001", "decision.json"),
                artifact_type="review.decision",
                metadata={"review_request_id": "review_001"},
            )
            ledger.record_review_decision(
                "s1",
                "assembly_review",
                decision="accept",
                review_request_id="review_001",
                artifact_ref=decision,
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
            evidence = _register_artifact(
                paths,
                workflow_step_id="step-001-read_qc",
                stage="read_qc",
                parts=("failure.json",),
                artifact_type="read_qc.failure",
            )
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))

            ledger.record_stage_failed(
                "s1",
                "read_qc",
                workflow_step_id="step-001-read_qc",
                error="tool failed",
                refs=[{"path_ref": evidence.path_ref, "role": "execution"}],
            )

            event = ledger.read_events("s1")[-1]
            self.assertEqual(event["event_type"], "stage_failed")
            self.assertEqual(event["error"], "tool failed")
            self.assertEqual(event["refs"][0]["path_ref"], evidence.path_ref)

    def test_nested_asset_ids_are_persisted_as_value_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))

            refs = ledger.references_from(
                {
                    "stage_outputs": {
                        "read_qc": {
                            "input_asset_ref": "s1:raw_long_reads:reads.fastq",
                            "input_asset_refs": [
                                "s1:raw_long_reads:reads.fastq",
                                "s1:raw_long_reads:reads-2.fastq",
                            ],
                            "summary_artifact_ref": (
                                "artifacts/steps/step-001-read_qc/read_qc/summary.json"
                            ),
                        }
                    }
                },
                role_prefix="terminal.completed",
            )
            ledger.record_session_terminal(
                "s1",
                outcome="completed",
                stage="done",
                refs=refs,
            )

            row_refs = ledger.read_event_rows("s1")[-1]["refs"]
            by_role = {ref["role"]: ref for ref in row_refs}
            self.assertEqual(
                by_role["terminal.completed.stage_outputs.read_qc.input_asset_ref"][
                    "value"
                ],
                "s1:raw_long_reads:reads.fastq",
            )
            self.assertNotIn(
                "path_ref",
                by_role["terminal.completed.stage_outputs.read_qc.input_asset_ref"],
            )
            self.assertEqual(
                by_role["terminal.completed.stage_outputs.read_qc.input_asset_refs.0"][
                    "value"
                ],
                "s1:raw_long_reads:reads.fastq",
            )
            self.assertEqual(
                by_role["terminal.completed.stage_outputs.read_qc.input_asset_refs.1"][
                    "value"
                ],
                "s1:raw_long_reads:reads-2.fastq",
            )
            self.assertEqual(
                by_role[
                    "terminal.completed.stage_outputs.read_qc.summary_artifact_ref"
                ]["path_ref"],
                "artifacts/steps/step-001-read_qc/read_qc/summary.json",
            )

    def test_stage_runtime_event_requires_an_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)

            with self.assertRaisesRegex(ValueError, "workflow_step_id"):
                ledger.record_event(
                    "s1",
                    "env_preflight_blocked",
                    {"method_name": "read_qc.long_read_stats.seqkit_stats"},
                    stage="read_qc",
                )

            event = ledger.record_occurrence_event(
                "s1",
                "env_preflight_blocked",
                {"method_name": "read_qc.long_read_stats.seqkit_stats"},
                stage="read_qc",
                workflow_step_id="step-001-read_qc",
                subject_id="read_qc.long_read_stats.seqkit_stats:seqkit",
            )
            self.assertEqual(event["workflow_step_id"], "step-001-read_qc")

    def test_raw_custom_stage_event_requires_an_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)

            with self.assertRaisesRegex(ValueError, "workflow_step_id"):
                ledger.record_event(
                    "s1",
                    "custom_stage_runtime_fact",
                    {"detail": "unbound"},
                    stage="assembly",
                )

            ledger.record_event(
                "s1",
                "custom_stage_runtime_fact",
                {"detail": "bound"},
                stage="assembly",
                workflow_step_id="step-001-assembly",
            )
            terminal = ledger.record_session_terminal(
                "s1",
                outcome="failed",
                stage="assembly",
            )
            self.assertIsNone(terminal)
            rows = ledger.read_event_rows("s1")
            self.assertEqual(rows[-2]["workflow_step_id"], "step-001-assembly")
            self.assertIsNone(rows[-1]["workflow_step_id"])

    def test_automatic_review_failure_replays_without_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            kwargs = {
                "data_dir": paths.run_dir,
                "session_id": "s1",
                "stage": "assembly_review",
                "workflow_step_id": "step-001-assembly_review",
                "gate_id": "assembly_review",
                "error": ValueError("review request is malformed"),
                "configured_seconds": 30,
                "elapsed_seconds": 30.0,
            }

            _record_automatic_review_failure(**kwargs)
            _record_automatic_review_failure(**kwargs)
            _record_automatic_review_failure(
                **{**kwargs, "elapsed_seconds": 31.0}
            )

            events = [
                event
                for event in Ledger(paths.run_dir).read_events("s1")
                if event["event_type"] == "automatic_review_failed"
            ]
            self.assertEqual(len(events), 2)
            self.assertEqual(
                {event["workflow_step_id"] for event in events},
                {"step-001-assembly_review"},
            )


if __name__ == "__main__":
    unittest.main()
