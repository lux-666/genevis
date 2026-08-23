"""Tests for the v2 session-summary projection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.lifecycle import inspect_session, normalize_resume_review_request
from app.reports.trace_writer import (
    SESSION_SUMMARY_JSON_FILENAME,
    SESSION_SUMMARY_MARKDOWN_FILENAME,
    STAGE_STATUS_BLOCKED_HUMAN,
    STAGE_STATUS_COMPLETED,
    STAGE_STATUS_EXECUTED,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_NO_EVIDENCE,
    STAGE_STATUS_PLANNED_ONLY,
    STAGE_STATUS_SKIPPED,
    _workflow_steps_for_trace,
    build_session_trace,
    write_session_trace,
)
from app.review.application import (
    ReviewApplicationIdentity,
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
)
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.gates import get_review_gate
from app.review.persistence import persist_review_request
from app.schemas.artifacts import ArtifactMeta
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import initial_state
from app.storage.artifacts import (
    artifact_ref_for_path,
    read_artifact_records,
    record_registered_artifacts,
    register_artifact_records,
)
from app.storage.frozen_inputs import freeze_session_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths, ensure_run_layout
from tests.busco_fixture import (
    assembly_review_selector_config,
    ensure_busco_lineage,
    ensure_test_host_envs,
)

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
        reads = paths.run_dir / "fixture_reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        run_config: dict[str, object] = {
            "requested_stages": [
                {"stage": stage, "allow_local_execution": False}
                for stage in requested_stages
            ]
        }
        if "assembly_review" in requested_stages:
            lineage = ensure_busco_lineage(paths.run_dir)
            run_config["assembly_review"] = assembly_review_selector_config()
            config_path = ensure_test_host_envs(paths.run_dir, lineage_path=lineage)
        else:
            config_path = ensure_test_host_envs(paths.run_dir)
        freeze_session_inputs(
            paths,
            manifest_draft={
                "session_id": session_id,
                "assets": [
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(reads.resolve())],
                        "platform": "ont",
                        "format": "fastq",
                    }
                ],
            },
            run_config_draft=run_config,
            config_path=config_path,
            selected_methods={},
            probe_environments=False,
        )
        Ledger(paths.run_dir).create_session(
            session_id,
            paths.to_ref(paths.input_manifest_path),
        )
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
        config_path=ensure_test_host_envs(Path(tmp)),
        selected_methods={},
    )
    Ledger(paths.run_dir).create_session(session_id="s1", input_manifest_ref="")
    return paths


def _step_id(stage: str, *, occurrence: int = 0) -> str:
    stages = ("inventory", *_DEFAULT_REQUESTED_STAGES)
    index = [index for index, value in enumerate(stages) if value == stage][occurrence]
    return f"step-{index:03d}-{stage}"


def _append_legacy_stage_terminal(
    ledger: Ledger,
    *,
    event_type: str,
    stage: str,
    workflow_step_id: str,
    summary: dict[str, object],
) -> None:
    """Inject immutable pre-invariant terminal history for trace coverage."""

    rows = ledger.read_event_rows("s1")
    sequence = int(rows[-1]["sequence"]) + 1
    row = {
        "schema_version": 2,
        "sequence": sequence,
        "event_id": f"evt_legacy_{sequence:08d}",
        "idempotency_key": f"legacy:{sequence}",
        "session_id": "s1",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": event_type,
        "stage": stage,
        "workflow_step_id": workflow_step_id,
        "subject_id": workflow_step_id,
        "refs": [],
        "summary": summary,
    }
    with ledger.events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def _append_legacy_attempt_terminal(
    ledger: Ledger,
    *,
    event_type: str,
    attempt_id: str,
    workflow_step_id: str,
    stage: str,
    method_name: str,
) -> None:
    rows = ledger.read_event_rows("s1")
    sequence = int(rows[-1]["sequence"]) + 1
    row = {
        "schema_version": 2,
        "sequence": sequence,
        "event_id": f"evt_legacy_{sequence:08d}",
        "idempotency_key": f"legacy:{sequence}",
        "session_id": "s1",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": event_type,
        "stage": stage,
        "workflow_step_id": workflow_step_id,
        "subject_id": attempt_id,
        "refs": [],
        "summary": {
            "method_name": method_name,
            "return_code": 1,
            "error": "legacy failed",
        },
    }
    with ledger.events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


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
    record_registration: bool = True,
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
    refs = register_artifact_records(paths.run_dir, paths.session_id, [artifact])
    if record_registration:
        record_registered_artifacts(paths.run_dir, paths.session_id, refs)
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
        input_manifest_ref=paths.to_ref(paths.input_manifest_path),
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


def _review_occurrence(
    paths: SessionPaths,
    *,
    reason: str,
    decision: str | None = None,
    applied: bool = False,
    rationale_artifact_ref=None,
    record_decision: bool = True,
):
    """Persist one exact Assembly Review occurrence through the v4 lifecycle."""

    state = _workflow_state(paths, workflow_step_index=4)
    state.update(
        {
            "supervision_state": "blocked_human",
            "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
        }
    )
    persisted = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason=reason,
        payload=ReviewPayload(
            stage="assembly_review",
            gate_id="assembly_review",
            reason=reason,
            decisions_allowed=["stop_human_review"],
        ),
    )
    state["active_interrupt"] = persisted.compact_interrupt(
        stage="assembly_review",
        gate_id="assembly_review",
        reason=reason,
    )
    ledger = Ledger(paths.run_dir)
    ledger.record_review_requested(
        paths.session_id,
        "assembly_review",
        reason=reason,
        review_request_artifact_ref=persisted.request_ref,
        review_request_id=persisted.review_request_id,
    )
    if decision is None:
        return persisted, None

    request = ResumeReviewRequest(
        session_id=paths.session_id,
        stage="assembly_review",
        gate_id="assembly_review",
        review_request_id=persisted.review_request_id,
        review_request_hash=persisted.request_hash,
        decision=decision,
        rationale_artifact_ref=rationale_artifact_ref,
    )
    canonical = normalize_resume_review_request(state, request)
    intent = persist_review_intent(state, canonical, source="test")
    gate = get_review_gate("assembly_review", "assembly_review")
    prepared = gate.load_prepare_application()(state, intent.intent)
    application_ref = persist_review_application(
        state,
        intent=intent,
        prepared=prepared,
    )
    application = load_verified_review_application(paths.run_dir, application_ref)
    identity = ReviewApplicationIdentity.from_intent(intent.intent)
    if record_decision:
        ledger.record_review_decision(
            paths.session_id,
            application_ref=application.ref,
            expected_identity=identity,
        )
    if applied:
        checkpoint_state = {
            **state,
            **dict(gate.load_apply_application()(state, application)),
            "review_application_ref": application.ref.model_dump(mode="json"),
        }
        ledger.record_review_applied(
            paths.session_id,
            application_ref=application.ref,
            expected_identity=identity,
            checkpoint_state=checkpoint_state,
        )
    return persisted, application


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
            persisted, _application = _review_occurrence(
                paths,
                reason="operator_required",
                decision="stop_human_review",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(_stage(trace, "read_qc")["status"], STAGE_STATUS_EXECUTED)
            self.assertEqual(
                _stage(trace, "inventory")["status"],
                STAGE_STATUS_NO_EVIDENCE,
            )
            self.assertEqual(
                trace["decisions"][0]["review_request_id"],
                persisted.review_request_id,
            )
            self.assertEqual(trace["decisions_by_origin"]["human"], 1)
            reviews = _stage(trace, "assembly_review")["reviews"]
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0]["status"], "application_prepared")
            self.assertFalse(reviews[0]["resolved"])
            self.assertEqual(reviews[0]["resolution_basis"], "none")
            self.assertEqual(
                _stage(trace, "read_qc")["methods"][0]["outcome"], "succeeded"
            )

    def test_attempt_conflict_uses_failed_canonical_method_and_failure_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            step_id = _step_id("read_qc")
            method = "read_qc.long_read_stats.seqkit_stats"
            _attempt(ledger, "s1", "read_qc", method, "succeeded")
            _append_legacy_attempt_terminal(
                ledger,
                event_type="attempt_failed",
                attempt_id=f"{step_id}:read_qc:attempt_000",
                workflow_step_id=step_id,
                stage="read_qc",
                method_name=method,
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                _stage(trace, "read_qc")["methods"][0]["outcome"], "failed"
            )
            self.assertEqual(len(trace["failures"]), 1)
            self.assertEqual(trace["failures"][0]["method_name"], method)
            diagnostic = trace["attempt_terminal_integrity"][0]
            self.assertEqual(diagnostic["kind"], "conflicting_attempt_terminal")
            self.assertEqual(diagnostic["selected_event_type"], "attempt_failed")

    def test_method_disposition_is_orthogonal_to_completed_pass_through_stage(self):
        """A reused candidate is a completed delivery, not a fake attempt."""

        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp, requested_stages=("assembly_dedup",))
            ledger = Ledger(paths.run_dir)
            step_id = "step-001-assembly_dedup"
            ledger.record_method_disposition(
                "s1",
                "assembly_dedup",
                step_id,
                "assembly_dedup.purge_dups",
                "skipped",
                reason="local_execution_not_enabled",
            )
            ledger.record_stage_completed(
                "s1", "assembly_dedup", workflow_step_id=step_id
            )

            trace = build_session_trace(paths.run_dir, "s1")

            dedup = _stage(trace, "assembly_dedup")
            self.assertEqual(dedup["status"], STAGE_STATUS_COMPLETED)
            self.assertEqual(len(dedup["methods"]), 1)
            method = dedup["methods"][0]
            self.assertEqual(method["outcome"], "not_started")
            self.assertEqual(method["disposition"], "skipped")
            self.assertEqual(
                method["disposition_reason"], "local_execution_not_enabled"
            )
            self.assertIsNone(method["attempt_id"])

    def test_post_terminal_attempt_conflict_is_not_method_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            step_id = _step_id("read_qc")
            ledger.record_session_terminal("s1", outcome="completed", stage="done")
            _attempt(
                ledger,
                "s1",
                "read_qc",
                "read_qc.long_read_stats.seqkit_stats",
                "succeeded",
            )
            _append_legacy_attempt_terminal(
                ledger,
                event_type="attempt_failed",
                attempt_id=f"{step_id}:read_qc:attempt_000",
                workflow_step_id=step_id,
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["attempt_terminal_integrity"], [])
            self.assertEqual(trace["failures"], [])
            self.assertEqual(
                _stage(trace, "read_qc")["status"], STAGE_STATUS_NO_EVIDENCE
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

    def test_post_terminal_stage_and_route_events_are_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            read_qc_step = _step_id("read_qc")
            assembly_step = _step_id("assembly")
            ledger.record_stage_entered("s1", "read_qc", workflow_step_id=read_qc_step)
            ledger.record_session_terminal("s1", outcome="completed", stage="read_qc")
            ledger.record_stage_entered(
                "s1", "assembly", workflow_step_id=assembly_step
            )
            _attempt(
                ledger,
                "s1",
                "assembly",
                "assembly.candidate.flye",
                "failed",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual([event["stage"] for event in trace["route"]], ["read_qc"])
            self.assertNotEqual(trace["final_or_current_stage"]["stage"], "assembly")
            self.assertEqual(
                _stage(trace, "assembly")["status"], STAGE_STATUS_NO_EVIDENCE
            )
            self.assertEqual(trace["failures"], [])
            terminal = trace["terminal_integrity"]
            self.assertEqual(terminal["post_terminal_event_count"], 3)
            self.assertEqual(
                [event["event_type"] for event in terminal["post_terminal_events"]],
                ["stage_entered", "attempt_started", "attempt_failed"],
            )

    def test_post_terminal_compaction_maintenance_is_not_integrity_pollution(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_session_terminal("s1", outcome="completed", stage="done")
            ledger.record_event(
                "s1",
                "terminal_compaction_completed",
                {"checkpoint_count": 1},
                idempotency_key="terminal-compaction:completed",
            )
            ledger.record_event(
                "s1",
                "terminal_compaction_skipped",
                {"error": "later retry diagnostic"},
                idempotency_key="terminal-compaction:skipped",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertIsNone(trace["terminal_integrity"])
            self.assertEqual(trace["authoritative_ledger_event_count"], 2)

    def test_spoofed_compaction_event_remains_terminal_integrity_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_session_terminal("s1", outcome="completed", stage="done")
            ledger.record_event(
                "s1",
                "terminal_compaction_completed",
                {"checkpoint_count": 2},
                idempotency_key="terminal-compaction:completed",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                trace["terminal_integrity"]["post_terminal_event_count"], 1
            )
            self.assertEqual(
                trace["terminal_integrity"]["post_terminal_events"][0]["event_type"],
                "terminal_compaction_completed",
            )

    def test_post_terminal_artifact_registration_cannot_change_trace_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            read_qc_step = _step_id("read_qc")
            ledger.record_stage_entered("s1", "read_qc", workflow_step_id=read_qc_step)
            _register_artifact(
                paths,
                stage="read_qc",
                method="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )
            ledger.record_session_terminal("s1", outcome="completed", stage="read_qc")
            _register_artifact(
                paths,
                stage="assembly",
                method="assembly.candidate.nextdenovo",
                artifact_type="draft_assembly.candidate",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                _stage(trace, "assembly")["status"], STAGE_STATUS_NO_EVIDENCE
            )
            self.assertNotIn("candidate_methods", _stage(trace, "assembly"))
            self.assertEqual(trace["artifact_count"], 1)
            self.assertEqual(trace["open_first"], [])
            self.assertEqual(
                trace["terminal_integrity"]["post_terminal_events"][0]["event_type"],
                "artifact_registered",
            )

    def test_attempt_success_ref_proves_terminal_trace_artifact_without_registration(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            artifact_path = _register_artifact(
                paths,
                stage="read_qc",
                method="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
                record_registration=False,
            )
            artifact_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                artifact_path,
                expected_type="read_qc.long_read_stats",
            )
            step_id = _step_id("read_qc")
            ledger.record_event(
                "s1",
                "attempt_succeeded",
                {"method_name": "read_qc.long_read_stats.seqkit_stats"},
                stage="read_qc",
                workflow_step_id=step_id,
                subject_id="attempt-success-ref",
                refs=[artifact_ref.model_dump(mode="json")],
            )
            ledger.record_session_terminal("s1", outcome="completed", stage="done")

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["artifact_count"], 1)
            self.assertEqual(
                _stage(trace, "read_qc")["artifact_refs"][0]["artifact_type"],
                "read_qc.long_read_stats",
            )

    def test_registered_assembly_candidate_bound_proves_terminal_trace_artifacts(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            bundle_path = _register_artifact(
                paths,
                stage="inventory",
                method="inventory.registered_assembly",
                artifact_type="assembly.candidate_bundle",
                record_registration=False,
            )
            primary_path = _register_artifact(
                paths,
                stage="inventory",
                method="inventory.registered_assembly",
                artifact_type="draft_assembly.candidate",
                record_registration=False,
            )
            bundle_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                bundle_path,
                expected_type="assembly.candidate_bundle",
            )
            primary_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                primary_path,
                expected_type="draft_assembly.candidate",
            )
            ledger.record_occurrence_event(
                "s1",
                "registered_assembly_candidate_bound",
                {
                    "asset_id": "asset_registered_assembly",
                    "candidate_bundle_ref": bundle_ref.model_dump(mode="json"),
                    "primary_output_ref": primary_ref.model_dump(mode="json"),
                },
                stage="inventory",
                workflow_step_id=_step_id("inventory"),
                subject_id="asset_registered_assembly",
            )
            ledger.record_session_terminal("s1", outcome="completed", stage="done")

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["artifact_count"], 2)
            self.assertEqual(
                {
                    ref["artifact_type"]
                    for ref in _stage(trace, "inventory")["artifact_refs"]
                },
                {"assembly.candidate_bundle", "draft_assembly.candidate"},
            )

    def test_review_request_path_ref_proves_terminal_trace_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            request_path = _register_artifact(
                paths,
                stage="assembly_review",
                method="assembly_review.request",
                artifact_type="assembly_review.request",
                metadata={"review_request_id": "review_path_ref"},
                artifact_name="review_path_ref.json",
                record_registration=False,
            )
            ledger = Ledger(paths.run_dir)
            ledger.record_review_requested(
                "s1",
                "assembly_review",
                reason="operator_required",
                review_request_artifact_ref=str(request_path),
                review_request_id="review_path_ref",
            )
            ledger.record_session_terminal("s1", outcome="completed", stage="done")

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["artifact_count"], 1)
            self.assertEqual(
                _stage(trace, "assembly_review")["artifact_refs"][0]["artifact_type"],
                "assembly_review.request",
            )

    def test_review_decision_proves_verified_application_required_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            evidence_path = _register_artifact(
                paths,
                stage="assembly_review",
                method="assembly_review.evidence",
                artifact_type="assembly_review.evidence",
                record_registration=False,
            )
            evidence_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                evidence_path,
                expected_type="assembly_review.evidence",
            )
            _persisted, application = _review_occurrence(
                paths,
                reason="operator_required",
                decision="stop_human_review",
                rationale_artifact_ref=evidence_ref,
            )
            self.assertIn(evidence_ref, application.application.required_refs)
            Ledger(paths.run_dir).record_session_terminal(
                "s1", outcome="completed", stage="done"
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertIn(
                "assembly_review.evidence",
                {
                    ref["artifact_type"]
                    for ref in _stage(trace, "assembly_review")["artifact_refs"]
                },
            )

    def test_terminal_ref_does_not_prove_terminal_trace_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            artifact_path = _register_artifact(
                paths,
                stage="read_qc",
                method="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
                record_registration=False,
            )
            artifact_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                artifact_path,
                expected_type="read_qc.long_read_stats",
            )
            ledger.record_session_terminal(
                "s1",
                outcome="completed",
                stage="done",
                refs=[artifact_ref.model_dump(mode="json")],
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["artifact_count"], 0)
            self.assertEqual(_stage(trace, "read_qc")["artifact_refs"], [])

    def test_corrupt_success_ref_does_not_prove_terminal_trace_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            artifact_path = _register_artifact(
                paths,
                stage="read_qc",
                method="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
                record_registration=False,
            )
            artifact_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                artifact_path,
                expected_type="read_qc.long_read_stats",
            ).model_dump(mode="json")
            artifact_ref["sha256"] = "0" * 64
            ledger.record_event(
                "s1",
                "attempt_succeeded",
                {"method_name": "read_qc.long_read_stats.seqkit_stats"},
                stage="read_qc",
                workflow_step_id=_step_id("read_qc"),
                subject_id="attempt-corrupt-ref",
                refs=[artifact_ref],
            )
            ledger.record_session_terminal("s1", outcome="completed", stage="done")

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["artifact_count"], 0)
            self.assertEqual(_stage(trace, "read_qc")["artifact_refs"], [])

    def test_attempt_failure_ref_does_not_promote_terminal_trace_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            artifact_path = _register_artifact(
                paths,
                stage="assembly",
                method="assembly.candidate.nextdenovo",
                artifact_type="draft_assembly.candidate",
                record_registration=False,
            )
            artifact_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                artifact_path,
                expected_type="draft_assembly.candidate",
            )
            step_id = _step_id("assembly")
            ledger.record_event(
                "s1",
                "attempt_failed",
                {"method_name": "assembly.candidate.nextdenovo", "error": "failed"},
                stage="assembly",
                workflow_step_id=step_id,
                subject_id="attempt-failure-ref",
                refs=[artifact_ref.model_dump(mode="json")],
            )
            ledger.record_session_terminal("s1", outcome="failed", stage="assembly")

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["artifact_count"], 0)
            self.assertEqual(_stage(trace, "assembly")["artifact_refs"], [])

    def test_resume_failure_ref_does_not_promote_terminal_trace_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            artifact_path = _register_artifact(
                paths,
                stage="assembly",
                method="assembly.candidate.nextdenovo",
                artifact_type="draft_assembly.candidate",
                record_registration=False,
            )
            artifact_ref = artifact_ref_for_path(
                paths.run_dir,
                "s1",
                artifact_path,
                expected_type="draft_assembly.candidate",
            )
            ledger.record_event(
                "s1",
                "resume_failed",
                {"phase": "checkpoint_apply", "error": "checkpoint failed"},
                stage="assembly",
                workflow_step_id=_step_id("assembly"),
                subject_id="review_assembly",
                refs=[artifact_ref.model_dump(mode="json")],
            )
            ledger.record_session_terminal("s1", outcome="failed", stage="assembly")

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(trace["artifact_count"], 0)
            self.assertEqual(_stage(trace, "assembly")["artifact_refs"], [])

    def test_post_terminal_artifact_cannot_create_fallback_trace_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp, create=False)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", "control/manifest.yaml")
            ledger.record_stage_entered(
                "s1", "read_qc", workflow_step_id="step-000-read-qc"
            )
            ledger.record_session_terminal("s1", outcome="completed", stage="read_qc")
            _register_artifact(
                paths,
                stage="assembly",
                method="assembly.candidate.nextdenovo",
                artifact_type="draft_assembly.candidate",
                workflow_step_id="step-001-assembly",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                [
                    (stage["workflow_step_id"], stage["stage"])
                    for stage in trace["stages"]
                ],
                [("step-000-read-qc", "read_qc")],
            )
            self.assertEqual(trace["artifact_count"], 0)

    def test_review_hash_mismatch_does_not_bind_to_request_occurrence(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            persisted, _application = _review_occurrence(
                paths,
                reason="operator_required",
            )
            ledger = Ledger(paths.run_dir)
            ledger._record_event(
                "s1",
                "review_decision",
                {
                    "review_request_id": persisted.review_request_id,
                    "request_hash": "0" * 64,
                    "decision": "stop_human_review",
                    "decided_by": "human",
                },
                stage="assembly_review",
                workflow_step_id=_step_id("assembly_review"),
                subject_id=persisted.review_request_id,
                idempotency_key="test:wrong-review-hash",
            )
            ledger._record_event(
                "s1",
                "review_applied",
                {
                    "review_request_id": persisted.review_request_id,
                    "request_hash": "0" * 64,
                    "application_id": "wrong-application",
                },
                stage="assembly_review",
                workflow_step_id=_step_id("assembly_review"),
                subject_id=persisted.review_request_id,
                idempotency_key="test:wrong-review-hash-applied",
            )

            trace = build_session_trace(paths.run_dir, "s1")

            review = _stage(trace, "assembly_review")["reviews"][0]
            self.assertEqual(review["request_hash"], persisted.request_hash)
            self.assertNotIn("decision", review)
            self.assertNotIn("applied_at", review)
            self.assertEqual(review["status"], "awaiting_decision")
            self.assertIn("review_bindings", {gap["field"] for gap in trace["gaps"]})

    def test_raw_decision_mismatch_is_excluded_from_trace_and_lifecycle(self):
        def wrong_application_id(payload, _refs, _application):
            payload["application_id"] = "wrong-application"

        def wrong_application_ref(_payload, refs, application):
            refs[2] = {
                **application.application.decision_ref.model_dump(mode="json"),
                "role": "application_ref",
            }

        def wrong_gate_id(payload, _refs, _application):
            payload["gate_id"] = "wrong_gate"

        def wrong_decision(payload, _refs, _application):
            payload["decision"] = "continue"

        for name, mutate in (
            ("application_id", wrong_application_id),
            ("application_ref", wrong_application_ref),
            ("gate_id", wrong_gate_id),
            ("decision", wrong_decision),
        ):
            with self.subTest(field=name), tempfile.TemporaryDirectory() as tmp:
                paths = _run(tmp)
                persisted, application = _review_occurrence(
                    paths,
                    reason="operator_required",
                    decision="stop_human_review",
                    record_decision=False,
                )
                assert application is not None
                envelope = application.application
                payload = {
                    "review_request_id": persisted.review_request_id,
                    "request_hash": persisted.request_hash,
                    "gate_id": envelope.gate_id,
                    "decision": "stop_human_review",
                    "decided_by": "human",
                    "application_id": envelope.application_id,
                    "effect_kind": envelope.effect_kind,
                    "source": "test",
                    "rejected_candidates": [],
                    "rejected_candidate_count": 0,
                    "note_count": 0,
                }
                refs = [
                    {
                        **envelope.decision_ref.model_dump(mode="json"),
                        "role": "artifact_ref",
                    },
                    {
                        **envelope.intent_ref.model_dump(mode="json"),
                        "role": "intent_ref",
                    },
                    {
                        **application.ref.model_dump(mode="json"),
                        "role": "application_ref",
                    },
                ]
                mutate(payload, refs, application)
                Ledger(paths.run_dir)._record_event(
                    paths.session_id,
                    "review_decision",
                    payload,
                    stage="assembly_review",
                    workflow_step_id=_step_id("assembly_review"),
                    subject_id=persisted.review_request_id,
                    refs=refs,
                    idempotency_key=f"test:raw-decision-mismatch:{name}",
                )

                summary = inspect_session(paths.session_id, paths.run_dir)
                occurrence = summary["review_occurrences"][0]
                self.assertIsNotNone(occurrence["integrity_error"])
                self.assertIsNone(summary["latest_review_decision"])
                self.assertEqual(summary["review_decisions"], [])

                trace = build_session_trace(paths.run_dir, paths.session_id)
                self.assertEqual(trace["decisions"], [])
                review = _stage(trace, "assembly_review")["reviews"][0]
                self.assertNotIn("decision", review)
                self.assertIn(
                    "review_bindings",
                    {gap["field"] for gap in trace["gaps"]},
                )

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
            for stage in ("inventory", "read_qc", "genome_survey", "assembly"):
                ledger.record_stage_completed(
                    "s1",
                    stage,
                    workflow_step_id=_step_id(stage),
                )
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
            reviews = _stage(trace, "assembly_review")["reviews"]
            self.assertEqual(len(reviews), 1)
            self.assertEqual(reviews[0]["status"], "awaiting_decision")
            self.assertFalse(reviews[0]["resolved"])
            self.assertEqual(reviews[0]["resolution_basis"], "none")
            self.assertFalse(
                {"policy_mode", "pending_human_confirmation"} & reviews[0].keys()
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
            _append_legacy_stage_terminal(
                ledger,
                event_type="stage_completed",
                stage="genome_survey",
                workflow_step_id=_step_id("genome_survey"),
                summary={},
            )

            trace = build_session_trace(paths.run_dir, "s1")

            self.assertEqual(
                [event["event_type"] for event in trace["route"]],
                [
                    "stage_entered",
                    "stage_completed",
                    "stage_skipped",
                    "stage_completed",
                ],
            )
            self.assertEqual(
                _stage(trace, "genome_survey")["status"], STAGE_STATUS_SKIPPED
            )
            self.assertEqual(
                _stage(trace, "genome_survey")["lifecycle"]["skip_reason"],
                "not_requested",
            )
            self.assertEqual(
                trace["stage_terminal_integrity"][0]["selected_event_type"],
                "stage_skipped",
            )
            markdown = write_session_trace(paths.run_dir, "s1")["markdown"]
            self.assertIn("## Stage Terminal Integrity Warning", markdown)
            self.assertIn(
                "Progress is a degraded projection, not authoritative",
                markdown,
            )
            self.assertNotIn("route", {gap["field"] for gap in trace["gaps"]})

    def test_multiple_requests_in_one_stage_resolve_by_request_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            applied_request, _application = _review_occurrence(
                paths,
                reason="operator_required_first",
                decision="stop_human_review",
                applied=True,
            )
            pending_request, _application = _review_occurrence(
                paths,
                reason="operator_required_second",
            )

            trace = build_session_trace(paths.run_dir, "s1")
            occurrences = {
                item["review_request_id"]: item
                for item in trace["pending_review_requests"]
            }

            self.assertEqual(
                occurrences[applied_request.review_request_id]["status"], "applied"
            )
            self.assertTrue(occurrences[applied_request.review_request_id]["resolved"])
            self.assertEqual(
                occurrences[applied_request.review_request_id]["resolution_basis"],
                "review_applied",
            )
            self.assertEqual(
                occurrences[pending_request.review_request_id]["status"],
                "awaiting_decision",
            )
            self.assertFalse(occurrences[pending_request.review_request_id]["resolved"])
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
