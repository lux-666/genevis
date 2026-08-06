"""Tests for the v2 session-summary projection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.lifecycle import inspect_session
from app.reports.trace_writer import (
    SESSION_SUMMARY_JSON_FILENAME,
    SESSION_SUMMARY_MARKDOWN_FILENAME,
    STAGE_STATUS_BLOCKED_HUMAN,
    STAGE_STATUS_EXECUTED,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_PLANNED_ONLY,
    STAGE_STATUS_SKIPPED,
    _workflow_steps_for_trace,
    build_session_trace,
    write_session_trace,
)
from app.review.base import ReviewPayload
from app.review.persistence import persist_review_request
from app.schemas.artifacts import ArtifactMeta
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import initial_state
from app.storage.artifacts import read_artifact_records, register_artifact_records
from app.storage.frozen_inputs import freeze_session_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths, ensure_run_layout

_DEFAULT_REQUESTED_STAGES = (
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review",
)


def _run(
    tmp: str,
    session_id: str = "s1",
    *,
    create: bool = True,
    requested_stages: tuple[str, ...] = _DEFAULT_REQUESTED_STAGES,
) -> SessionPaths:
    paths = SessionPaths(tmp, session_id)
    ensure_run_layout(paths)
    if create:
        paths.manifest_path.write_text("session_id: s1\n", encoding="utf-8")
        paths.run_config_path.write_text(
            json.dumps(
                {
                    "requested_stages": [
                        {"stage": stage, "allow_local_execution": False}
                        for stage in requested_stages
                    ]
                }
            ),
            encoding="utf-8",
        )
        Ledger(paths.run_dir).create_session(session_id, str(paths.manifest_path))
    return paths


def _frozen_run_with_read_qc(tmp: str) -> SessionPaths:
    paths = SessionPaths(tmp, "s1")
    reads = Path(tmp) / "reads.fastq"
    reads.write_text("@read\nACGT\n+\n!!!!\n", encoding="utf-8")
    manifest_draft = Path(tmp) / "manifest.yaml"
    manifest_draft.write_text(
        "session_id: s1\n"
        "assets:\n"
        "  - asset_type: raw_long_reads\n"
        f"    paths: [{reads}]\n"
        "    platform: ont\n"
        "    format: fastq\n",
        encoding="utf-8",
    )
    run_config_draft = Path(tmp) / "run_config.json"
    run_config_draft.write_text(
        json.dumps(
            {"requested_stages": [{"stage": "read_qc", "allow_local_execution": False}]}
        ),
        encoding="utf-8",
    )
    freeze_session_inputs(
        paths,
        manifest_draft=manifest_draft,
        run_config_draft=run_config_draft,
        selected_methods={},
    )
    Ledger(paths.run_dir).create_session(session_id="s1", input_manifest_ref="")
    return paths


def _step_id(stage: str, *, occurrence: int = 0) -> str:
    stages = ("inventory", *_DEFAULT_REQUESTED_STAGES)
    index = [index for index, value in enumerate(stages) if value == stage][occurrence]
    return f"step-{index:03d}-{stage}"


def _attempt(
    ledger: Ledger,
    session_id: str,
    stage: str,
    method: str,
    outcome: str,
    workflow_step_id: str | None = None,
) -> None:
    workflow_step_id = workflow_step_id or _step_id(stage)
    attempt_id = f"{workflow_step_id}:{stage}:attempt_000"
    ledger.record_event(
        session_id,
        "attempt_started",
        {"attempt_id": attempt_id, "stage": stage, "method_name": method},
        workflow_step_id=workflow_step_id,
    )
    if outcome == "succeeded":
        ledger.record_event(
            session_id,
            "attempt_succeeded",
            {"attempt_id": attempt_id, "stage": stage, "method_name": method},
            workflow_step_id=workflow_step_id,
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
            workflow_step_id=workflow_step_id,
        )


def _register_artifact(
    paths: SessionPaths,
    *,
    stage: str,
    method: str,
    artifact_type: str,
    payload: dict | None = None,
    metadata: dict | None = None,
    workflow_step_id: str | None = None,
    artifact_name: str | None = None,
) -> Path:
    workflow_step_id = workflow_step_id or _step_id(stage)
    file_path = paths.workflow_step_artifact_path(
        workflow_step_id,
        stage,
        "evidence",
        artifact_name or f"{artifact_type.replace('.', '_')}.json",
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


def _workflow_state(paths: SessionPaths, *, workflow_step_index: int) -> dict:
    steps = [
        {
            "step_id": f"step-{index:03d}-{stage}",
            "stage": stage,
            "allow_local_execution": False,
        }
        for index, stage in enumerate(("inventory", *_DEFAULT_REQUESTED_STAGES))
    ]
    producer_step_id = steps[0]["step_id"]
    for step in steps:
        if step["stage"] == "assembly_review":
            step["review_target"] = {
                "resource_type": "assembly",
                "producer_step_id": producer_step_id,
            }
        if step["stage"] in {"inventory", "assembly", "assembly_review"}:
            producer_step_id = step["step_id"]
    state = initial_state(
        session_id=paths.session_id,
        input_manifest_ref=paths.to_ref(paths.manifest_path),
        data_dir=str(paths.run_dir),
        workflow_steps=steps,
        stage_plan_ref=paths.to_ref(paths.run_config_path),
    )
    state.update(
        {
            "workflow_step_index": workflow_step_index,
            "entered_workflow_step_index": workflow_step_index,
            "current_stage": steps[workflow_step_index]["stage"],
        }
    )
    return state


class SessionSummaryBuildTests(unittest.TestCase):
    def test_hash_invalid_run_config_uses_only_durable_occurrences(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _frozen_run_with_read_qc(tmp)
            Ledger(paths.run_dir).record_stage_entered(
                "s1",
                "read_qc",
                workflow_step_id="step-durable-read-qc",
            )
            paths.run_config_path.write_text(
                json.dumps(
                    {
                        "requested_stages": [
                            {"stage": "assembly", "allow_local_execution": False}
                        ]
                    }
                ),
                encoding="utf-8",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                [record["workflow_step_id"] for record in trace["stages"]],
                ["step-durable-read-qc"],
            )
            self.assertEqual(trace["stages"][0]["stage"], "read_qc")

    def test_durable_conflicts_are_removed_and_fallback_order_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp, create=False)

            steps = _workflow_steps_for_trace(
                paths,
                [
                    {"workflow_step_id": "step-010-z", "stage": "assembly"},
                    {"workflow_step_id": "step-conflict", "stage": "read_qc"},
                    {"workflow_step_id": "step-002-a", "stage": "inventory"},
                ],
                [
                    {"workflow_step_id": "step-conflict", "stage": "assembly"},
                    {"workflow_step_id": "step-002-a", "stage": "inventory"},
                ],
            )

            self.assertEqual(
                [step["step_id"] for step in steps],
                ["step-002-a", "step-010-z"],
            )

    def test_inspect_route_trace_keeps_only_latest_sixteen_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            for index in range(ROUTE_TRACE_LIMIT + 4):
                ledger.record_stage_entered(
                    "s1",
                    f"stage_{index}",
                    workflow_step_id=f"step-{index:03d}-stage_{index}",
                    route_node=f"route_{index}",
                    idempotency_key=f"route-test-{index}",
                )

            summary = inspect_session("s1", paths.run_dir)

            self.assertEqual(
                summary["route_trace"],
                [
                    f"step-{index:03d}-stage_{index}:route_{index}"
                    for index in range(4, ROUTE_TRACE_LIMIT + 4)
                ],
            )

    def test_attempt_and_request_bound_decision_are_reconstructed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_stage_entered(
                "s1", "read_qc", workflow_step_id=_step_id("read_qc")
            )
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
            ledger.record_event(
                "s1",
                "review_decision",
                {
                    "stage": "assembly_review",
                    "review_request_id": "review_001",
                    "decision": "accept",
                    "candidate_id": "flye",
                    "decided_by": "human",
                },
                workflow_step_id=_step_id("assembly_review"),
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(_stage(trace, "read_qc")["status"], STAGE_STATUS_EXECUTED)
            self.assertFalse(
                any(stage["stage"] == "inventory" for stage in trace["stages"])
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

    def test_repeated_stage_occurrences_have_distinct_trace_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp, requested_stages=("read_qc", "read_qc"))
            ledger = Ledger(paths.run_dir)
            first_step_id = "step-001-read_qc"
            second_step_id = "step-002-read_qc"
            for step_id in (first_step_id, second_step_id):
                ledger.record_stage_entered("s1", "read_qc", workflow_step_id=step_id)
                _attempt(
                    ledger,
                    "s1",
                    "read_qc",
                    "read_qc.long_read_stats.seqkit_stats",
                    "succeeded",
                    workflow_step_id=step_id,
                )
                ledger.record_stage_completed("s1", "read_qc", workflow_step_id=step_id)

            trace = build_session_trace(paths.run_dir, "s1")
            read_qc_steps = [
                stage for stage in trace["stages"] if stage["stage"] == "read_qc"
            ]

            self.assertEqual(
                [stage["workflow_step_id"] for stage in read_qc_steps],
                [first_step_id, second_step_id],
            )
            self.assertEqual(
                [stage["status"] for stage in read_qc_steps],
                [STAGE_STATUS_EXECUTED, STAGE_STATUS_EXECUTED],
            )
            self.assertEqual(
                [
                    event["workflow_step_id"]
                    for event in trace["route"]
                    if event["event_type"] == "stage_entered"
                ],
                [first_step_id, second_step_id],
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
                _workflow_state(paths, workflow_step_index=4),
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
            self.assertFalse(
                {"policy_mode", "pending_human_confirmation"}
                & _stage(trace, "assembly_review")["review"].keys()
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
            ledger.record_stage_entered(
                "s1",
                "read_qc",
                workflow_step_id=_step_id("read_qc"),
                route_node="route_controller",
            )
            ledger.record_stage_completed(
                "s1", "read_qc", workflow_step_id=_step_id("read_qc")
            )
            ledger.record_stage_skipped(
                "s1",
                "genome_survey",
                workflow_step_id=_step_id("genome_survey"),
                reason="not_requested",
            )

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
                request = _register_artifact(
                    paths,
                    stage="assembly_review",
                    method="assembly_review.request",
                    artifact_type="assembly_review.request",
                    metadata={"review_request_id": request_id},
                    artifact_name=f"{request_id}.json",
                )
                ledger.record_review_requested(
                    "s1",
                    "assembly_review",
                    reason="operator_required",
                    review_request_artifact_ref=str(request),
                    review_request_id=request_id,
                )
            ledger.record_event(
                "s1",
                "review_decision",
                {
                    "stage": "assembly_review",
                    "review_request_id": "review_001",
                    "decision": "accept",
                    "decided_by": "human",
                },
                workflow_step_id=_step_id("assembly_review"),
            )

            trace = build_session_trace(paths.run_dir, "s1")
            pending = {
                item["review_request_id"]: item["resolved"]
                for item in trace["pending_review_requests"]
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
            ledger.record_stage_entered(
                "s1", "read_qc", workflow_step_id=_step_id("read_qc")
            )
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
