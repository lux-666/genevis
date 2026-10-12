"""Data Architecture v2 ledger contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.entrypoints.run import _record_automatic_review_failure
from app.review.application import (
    PreparedReviewApplication,
    ReviewApplicationIdentity,
    persist_review_application,
    persist_review_intent,
)
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.ledger import record_review_decision
from app.review.persistence import persist_review_decision, persist_review_request
from app.schemas.artifacts import ArtifactMeta
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import initial_state
from app.stages.inventory.nodes import InventoryReviewApplicationEffectV1
from app.storage.artifacts import register_artifact_records
from app.storage.ledger import (
    LEDGER_SCHEMA_VERSION,
    SUMMARY_MAX_BYTES,
    Ledger,
    LedgerConflictError,
    project_attempt_terminal_events,
    project_stage_terminal_events,
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


def _append_legacy_stage_terminal(
    ledger: Ledger,
    *,
    event_type: str,
    stage: str,
    workflow_step_id: str,
    summary: dict[str, object],
) -> None:
    """Inject pre-invariant terminal history for read-model compatibility tests."""

    rows = ledger.read_event_rows("s1")
    sequence = int(rows[-1]["sequence"]) + 1
    row = {
        "schema_version": LEDGER_SCHEMA_VERSION,
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
        "schema_version": LEDGER_SCHEMA_VERSION,
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
        "summary": {"method_name": method_name, "error": "legacy failure"},
    }
    with ledger.events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def _append_legacy_idempotency_collision(
    ledger: Ledger,
    *,
    idempotency_key: str,
) -> None:
    """Inject immutable bad history to exercise append-lock fail-closed paths."""

    rows = ledger.read_event_rows("s1")
    sequence = int(rows[-1]["sequence"]) + 1
    row = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "sequence": sequence,
        "event_id": f"evt_legacy_{sequence:08d}",
        "idempotency_key": idempotency_key,
        "session_id": "s1",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": "legacy_other_domain_fact",
        "stage": None,
        "workflow_step_id": None,
        "subject_id": None,
        "refs": [],
        "summary": {"detail": "occupied by another domain"},
    }
    with ledger.events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")


class LedgerV2Tests(unittest.TestCase):
    def test_attempt_terminal_writes_are_singleton_and_exact_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            attempt_id = "step-001-read_qc:attempt_000"
            first = ledger.record_event(
                "s1",
                "attempt_succeeded",
                {"method_name": "seqkit_stats"},
                stage="read_qc",
                workflow_step_id="step-001-read_qc",
                subject_id=attempt_id,
                idempotency_key="attempt:success",
            )
            self.assertEqual(
                ledger.record_event(
                    "s1",
                    "attempt_succeeded",
                    {"method_name": "seqkit_stats"},
                    stage="read_qc",
                    workflow_step_id="step-001-read_qc",
                    subject_id=attempt_id,
                    idempotency_key="attempt:success:replay",
                ),
                first,
            )
            with self.assertRaisesRegex(
                LedgerConflictError, "requested=attempt_failed"
            ):
                ledger.record_event(
                    "s1",
                    "attempt_failed",
                    {"method_name": "seqkit_stats", "error": "late failure"},
                    stage="read_qc",
                    workflow_step_id="step-001-read_qc",
                    subject_id=attempt_id,
                    idempotency_key="attempt:failure",
                )
            with self.assertRaisesRegex(
                LedgerConflictError, "same terminal outcome has different semantic body"
            ):
                ledger.record_event(
                    "s1",
                    "attempt_succeeded",
                    {"method_name": "different_method"},
                    stage="read_qc",
                    workflow_step_id="step-001-read_qc",
                    subject_id=attempt_id,
                    idempotency_key="attempt:success:drift",
                )

    def test_method_disposition_is_typed_unique_and_requires_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            first = ledger.record_method_disposition(
                "s1",
                "read_qc",
                "step-001-read_qc",
                "seqkit_stats",
                "skipped",
                reason="not selected",
            )
            self.assertEqual(
                ledger.record_method_disposition(
                    "s1",
                    "read_qc",
                    "step-001-read_qc",
                    "seqkit_stats",
                    "skipped",
                    reason="not selected",
                ),
                first,
            )
            self.assertEqual(
                ledger.record_method_disposition_recorded(
                    "s1",
                    "read_qc",
                    "step-001-read_qc",
                    "seqkit_stats",
                    "skipped",
                    reason="not selected",
                ),
                first,
            )
            with self.assertRaisesRegex(LedgerConflictError, "method disposition"):
                ledger.record_method_disposition(
                    "s1",
                    "read_qc",
                    "step-001-read_qc",
                    "seqkit_stats",
                    "failed",
                    reason="tool failed",
                )
            with self.assertRaisesRegex(ValueError, "requires a reason"):
                ledger.record_method_disposition(
                    "s1",
                    "read_qc",
                    "step-001-read_qc",
                    "seqkit_length_filter",
                    "skipped",
                )
            with self.assertRaisesRegex(ValueError, "must be recorded"):
                ledger.record_event(
                    "s1",
                    "method_disposition_recorded",
                    {"method_name": "seqkit_stats", "disposition": "completed"},
                    stage="read_qc",
                    workflow_step_id="step-001-read_qc",
                )

    def test_attempt_terminal_projection_fails_closed_for_legacy_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            attempt_id = "step-001-read_qc:attempt_000"
            ledger.record_event(
                "s1",
                "attempt_succeeded",
                {"method_name": "seqkit_stats"},
                stage="read_qc",
                workflow_step_id="step-001-read_qc",
                subject_id=attempt_id,
            )
            _append_legacy_attempt_terminal(
                ledger,
                event_type="attempt_failed",
                attempt_id=attempt_id,
                workflow_step_id="step-001-read_qc",
                stage="read_qc",
                method_name="seqkit_stats",
            )
            terminals, diagnostics = project_attempt_terminal_events(
                ledger.read_events("s1")
            )
            self.assertEqual(terminals[attempt_id]["event_type"], "attempt_failed")
            self.assertEqual(diagnostics[0]["kind"], "conflicting_attempt_terminal")
            self.assertEqual(diagnostics[0]["selected_sequence"], 2)

    def test_stage_terminal_writes_reject_conflicting_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            step_id = "step-001-genome_survey"
            first = ledger.record_event(
                "s1",
                "stage_completed",
                {},
                stage="genome_survey",
                workflow_step_id=step_id,
                subject_id=step_id,
                idempotency_key="terminal:first",
            )

            # A second idempotency key with the identical terminal fact is a
            # replay, not a second row.
            replay = ledger.record_event(
                "s1",
                "stage_completed",
                {},
                stage="genome_survey",
                workflow_step_id=step_id,
                subject_id=step_id,
                idempotency_key="terminal:alternate-key",
            )
            self.assertEqual(replay, first)
            with self.assertRaisesRegex(
                LedgerConflictError,
                "stage terminal conflicts with existing terminal outcome",
            ):
                ledger.record_stage_skipped(
                    "s1",
                    "genome_survey",
                    workflow_step_id=step_id,
                    reason="no_usable_survey_reads",
                )
            with self.assertRaisesRegex(LedgerConflictError, "requested=stage_failed"):
                ledger.record_stage_failed(
                    "s1",
                    "genome_survey",
                    workflow_step_id=step_id,
                    error="late failure",
                )
            self.assertEqual(
                [row["event_type"] for row in ledger.read_event_rows("s1")],
                ["session_created", "stage_completed"],
            )

    def test_stage_terminal_writes_reject_same_type_payload_or_ref_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            step_id = "step-001-genome_survey"
            ledger.record_stage_skipped(
                "s1",
                "genome_survey",
                workflow_step_id=step_id,
                reason="no_usable_survey_reads",
                idempotency_key="terminal:skip:first",
            )

            with self.assertRaisesRegex(
                LedgerConflictError,
                "same terminal outcome has different semantic body",
            ):
                ledger.record_stage_skipped(
                    "s1",
                    "genome_survey",
                    workflow_step_id=step_id,
                    reason="operator_declined",
                    idempotency_key="terminal:skip:payload-drift",
                )
            with self.assertRaisesRegex(
                LedgerConflictError,
                "same terminal outcome has different semantic body",
            ):
                ledger.record_stage_skipped(
                    "s1",
                    "genome_survey",
                    workflow_step_id=step_id,
                    reason="no_usable_survey_reads",
                    refs=[{"role": "diagnostic", "value": "different"}],
                    idempotency_key="terminal:skip:ref-drift",
                )
            self.assertEqual(
                [row["event_type"] for row in ledger.read_event_rows("s1")],
                ["session_created", "stage_skipped"],
            )

    def test_stage_terminal_writes_reject_cross_stage_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            step_id = "step-001-assembly"
            ledger.record_event(
                "s1",
                "stage_completed",
                {},
                stage="assembly",
                workflow_step_id=step_id,
                subject_id=step_id,
                idempotency_key="terminal:assembly:first",
            )

            with self.assertRaisesRegex(
                LedgerConflictError,
                "existing_stage=assembly; requested_stage=assembly_review",
            ):
                ledger.record_event(
                    "s1",
                    "stage_completed",
                    {},
                    stage="assembly_review",
                    workflow_step_id=step_id,
                    subject_id=step_id,
                    idempotency_key="terminal:assembly:cross-stage",
                )

    def test_stage_terminal_read_model_reports_legacy_cross_stage_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            step_id = "step-001-assembly"
            ledger.record_stage_completed("s1", "assembly", workflow_step_id=step_id)
            _append_legacy_stage_terminal(
                ledger,
                event_type="stage_skipped",
                stage="assembly_review",
                workflow_step_id=step_id,
                summary={"reason": "legacy_stage_binding_drift"},
            )

            terminals, diagnostics = project_stage_terminal_events(
                ledger.read_events("s1")
            )

            self.assertEqual(
                terminals[(step_id, "assembly")]["event_type"], "stage_completed"
            )
            self.assertEqual(
                terminals[(step_id, "assembly_review")]["event_type"],
                "stage_skipped",
            )
            self.assertEqual(len(diagnostics), 1)
            diagnostic = diagnostics[0]
            self.assertEqual(
                diagnostic["kind"], "conflicting_stage_terminal_stage_binding"
            )
            self.assertEqual(diagnostic["workflow_step_id"], step_id)
            self.assertEqual(diagnostic["stages"], ["assembly", "assembly_review"])

    def test_stage_terminal_replay_rejects_legacy_multiple_terminal_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            step_id = "step-001-genome_survey"
            ledger.record_stage_completed(
                "s1", "genome_survey", workflow_step_id=step_id
            )
            _append_legacy_stage_terminal(
                ledger,
                event_type="stage_skipped",
                stage="genome_survey",
                workflow_step_id=step_id,
                summary={"reason": "legacy_conflict"},
            )

            # This reuses the first row's default idempotency key. The terminal
            # invariant must run before generic idempotency replay.
            with self.assertRaisesRegex(
                LedgerConflictError,
                "multiple stage terminal events",
            ):
                ledger.record_stage_completed(
                    "s1", "genome_survey", workflow_step_id=step_id
                )

    def test_stage_terminal_read_model_reports_legacy_conflicts_and_uses_precedence(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            step_id = "step-001-genome_survey"
            ledger.record_stage_completed(
                "s1", "genome_survey", workflow_step_id=step_id
            )
            _append_legacy_stage_terminal(
                ledger,
                event_type="stage_skipped",
                stage="genome_survey",
                workflow_step_id=step_id,
                summary={"reason": "no_usable_survey_reads"},
            )
            _append_legacy_stage_terminal(
                ledger,
                event_type="stage_failed",
                stage="genome_survey",
                workflow_step_id=step_id,
                summary={"error": "late failure"},
            )

            terminals, diagnostics = project_stage_terminal_events(
                ledger.read_events("s1")
            )

            self.assertEqual(
                terminals[(step_id, "genome_survey")]["event_type"],
                "stage_failed",
            )
            self.assertEqual(len(diagnostics), 1)
            diagnostic = diagnostics[0]
            self.assertEqual(diagnostic["kind"], "conflicting_stage_terminal")
            self.assertEqual(diagnostic["workflow_step_id"], step_id)
            self.assertEqual(diagnostic["stage"], "genome_survey")
            self.assertEqual(diagnostic["terminal_count"], 3)
            self.assertEqual(diagnostic["selected_event_type"], "stage_failed")
            self.assertEqual(diagnostic["selected_sequence"], 4)
            self.assertEqual(
                [event["event_type"] for event in diagnostic["terminal_events"]],
                ["stage_completed", "stage_skipped", "stage_failed"],
            )
            self.assertEqual(
                [event["sequence"] for event in diagnostic["terminal_events"]],
                [2, 3, 4],
            )

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

    def test_cross_domain_key_occupancy_cannot_mask_natural_key_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))

            ledger.record_stage_completed(
                "s1",
                "read_qc",
                workflow_step_id="step-001-read_qc",
                idempotency_key="collision:stage",
            )
            stage = ledger.read_event_rows("s1")[-1]
            attempt = ledger.record_event(
                "s1",
                "attempt_succeeded",
                {"method_name": "seqkit_stats"},
                stage="read_qc",
                workflow_step_id="step-001-read_qc",
                subject_id="step-001-read_qc:attempt_000",
                idempotency_key="collision:attempt",
            )
            method = ledger.record_method_disposition(
                "s1",
                "read_qc",
                "step-001-read_qc",
                "seqkit_stats",
                "completed",
            )
            for row in (stage, attempt, method):
                _append_legacy_idempotency_collision(
                    ledger,
                    idempotency_key=row["idempotency_key"],
                )

            with self.assertRaisesRegex(LedgerConflictError, "idempotency key"):
                ledger.record_stage_completed(
                    "s1",
                    "read_qc",
                    workflow_step_id="step-001-read_qc",
                    idempotency_key="collision:stage",
                )
            with self.assertRaisesRegex(LedgerConflictError, "idempotency key"):
                ledger.record_event(
                    "s1",
                    "attempt_succeeded",
                    {"method_name": "seqkit_stats"},
                    stage="read_qc",
                    workflow_step_id="step-001-read_qc",
                    subject_id="step-001-read_qc:attempt_000",
                    idempotency_key="collision:attempt",
                )
            with self.assertRaisesRegex(LedgerConflictError, "idempotency key"):
                ledger.record_method_disposition(
                    "s1",
                    "read_qc",
                    "step-001-read_qc",
                    "seqkit_stats",
                    "completed",
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
                review_request_artifact_ref=paths.resolve_ref(request.path_ref),
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
                row["refs"][0],
                {
                    **request.model_dump(mode="json", exclude={"schema_version"}),
                    "role": "review_request_artifact_ref",
                },
            )

    def test_review_decisions_bind_to_full_request_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", str(paths.manifest_path))
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(paths.run_dir),
            )
            state.update(
                {
                    "entered_workflow_step_index": 0,
                    "supervision_state": "blocked_human",
                    "stage_outputs": {"inventory": {"status": "blocked_human"}},
                }
            )
            persisted_requests = []
            for suffix in ("first", "second"):
                request = persist_review_request(
                    state,
                    stage="inventory",
                    gate_id="inventory_review",
                    reason=f"operator_required_{suffix}",
                    payload=ReviewPayload(
                        stage="inventory",
                        gate_id="inventory_review",
                        reason=f"operator_required_{suffix}",
                        decisions_allowed=["continue", "stop_human_review"],
                    ),
                )
                persisted_requests.append(request)
                ledger.record_review_requested(
                    "s1",
                    "inventory",
                    reason="operator_required",
                    review_request_artifact_ref=request.request_ref,
                    review_request_id=request.review_request_id,
                )
            selected = persisted_requests[0]
            state["active_interrupt"] = selected.compact_interrupt(
                stage="inventory",
                gate_id="inventory_review",
                reason="operator_required_first",
            )
            request = ResumeReviewRequest(
                session_id="s1",
                stage="inventory",
                gate_id="inventory_review",
                review_request_id=selected.review_request_id,
                review_request_hash=selected.request_hash,
                decision="stop_human_review",
            )
            intent = persist_review_intent(state, request, source="review")
            required_ref = _register_artifact(
                paths,
                workflow_step_id="step-000-inventory",
                stage="inventory",
                parts=("reviews", selected.review_request_id, "required.json"),
                artifact_type="inventory.review.required",
            )
            decision_ref = persist_review_decision(
                paths.run_dir,
                session_id="s1",
                review_request_id=selected.review_request_id,
                stage="inventory",
                gate_id="inventory_review",
                decision=request,
                request_hash=selected.request_hash,
                request_ref=selected.request_ref,
            )
            application_ref = persist_review_application(
                state,
                intent=intent,
                prepared=PreparedReviewApplication(
                    effect_kind="inventory.review_application.v1",
                    effect=InventoryReviewApplicationEffectV1(
                        decision="stop_human_review"
                    ),
                    decision_ref=decision_ref,
                    required_refs=(required_ref,),
                ),
            )
            identity = ReviewApplicationIdentity.from_intent(intent.intent)
            record_review_decision(
                ledger,
                "s1",
                application_ref=application_ref,
                expected_identity=identity,
            )

            decisions = [
                event
                for event in ledger.read_events("s1")
                if event["event_type"] == "review_decision"
            ]
            self.assertEqual(decisions[0]["subject_id"], selected.review_request_id)
            self.assertEqual(
                decisions[0]["review_request_id"], selected.review_request_id
            )
            self.assertEqual(decisions[0]["request_hash"], selected.request_hash)
            required_event_ref = next(
                ref
                for ref in decisions[0]["refs"]
                if ref.get("role") == "application_required_ref"
            )
            self.assertEqual(
                required_event_ref,
                {
                    **required_ref.model_dump(mode="json", exclude={"schema_version"}),
                    "role": "application_required_ref",
                },
            )

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
            self.assertEqual(terminal["summary"]["outcome"], "failed")
            rows = ledger.read_event_rows("s1")
            self.assertEqual(rows[-2]["workflow_step_id"], "step-001-assembly")
            self.assertIsNone(rows[-1]["workflow_step_id"])

    def test_session_terminal_replay_requires_the_complete_semantic_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)

            first = ledger.record_session_terminal(
                "s1",
                outcome="completed",
                stage="done",
                error=None,
            )
            self.assertEqual(
                ledger.record_session_terminal(
                    "s1", outcome="completed", stage="done", error=None
                ),
                first,
            )
            with self.assertRaisesRegex(LedgerConflictError, "different semantic body"):
                ledger.record_session_terminal(
                    "s1",
                    outcome="completed",
                    stage="assembly_review",
                    error="later diagnostics must not replace the first fact",
                )
            terminal_rows = [
                row
                for row in ledger.read_event_rows("s1")
                if row["event_type"] == "session_terminal"
            ]
            self.assertEqual(terminal_rows, [first])
            self.assertEqual(first["idempotency_key"], "session:s1:terminal")

    def test_session_terminal_replay_rejects_cross_domain_idempotency_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            first = ledger.record_session_terminal(
                "s1", outcome="completed", stage="done"
            )
            collision = dict(first)
            collision.update(
                {
                    "sequence": 2,
                    "event_id": "evt_legacy_collision",
                    "event_type": "legacy_cross_domain_fact",
                    "stage": None,
                    "summary": {"detail": "must not replay as terminal"},
                }
            )
            with ledger.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(collision, separators=(",", ":")) + "\n")

            with self.assertRaisesRegex(LedgerConflictError, "idempotency"):
                ledger.record_session_terminal("s1", outcome="completed", stage="done")

    def test_stage_terminal_requires_exact_roster_coverage_when_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            step_id = "step-001-read_qc"
            roster = (
                "read_qc.long_read_stats.seqkit_stats",
                "read_qc.length_filter.skip",
            )
            with self.assertRaisesRegex(LedgerConflictError, "missing="):
                ledger.record_stage_completed(
                    "s1",
                    "read_qc",
                    workflow_step_id=step_id,
                    selected_method_names=roster,
                )
            ledger.record_method_disposition(
                "s1",
                "read_qc",
                step_id,
                roster[0],
                "completed",
            )
            ledger.record_method_disposition(
                "s1",
                "read_qc",
                step_id,
                roster[1],
                "completed",
            )
            ledger.record_stage_completed(
                "s1",
                "read_qc",
                workflow_step_id=step_id,
                selected_method_names=roster,
            )
            empty_step_id = "step-002-read_qc"
            ledger.record_method_disposition(
                "s1",
                "read_qc",
                empty_step_id,
                "read_qc.long_read_stats.seqkit_stats",
                "completed",
            )
            with self.assertRaisesRegex(LedgerConflictError, "extra="):
                ledger.record_stage_completed(
                    "s1",
                    "read_qc",
                    workflow_step_id=empty_step_id,
                    selected_method_names=(),
                )

    def test_failed_session_terminal_persists_scope_and_completed_rejects_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            failed = ledger.record_session_terminal(
                "s1",
                outcome="failed",
                stage="read_qc",
                failure_scope="controller_boundary",
            )
            self.assertEqual(failed["summary"]["failure_scope"], "controller_boundary")
            with self.assertRaisesRegex(ValueError, "must not include failure_scope"):
                Ledger(_run(tmp, "s2").run_dir).record_session_terminal(
                    "s2",
                    outcome="completed",
                    stage="done",
                    failure_scope="occurrence",
                )

    def test_session_terminal_rejects_a_conflicting_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_session_terminal("s1", outcome="completed", stage="done")

            with self.assertRaisesRegex(LedgerConflictError, "terminal"):
                ledger.record_session_terminal(
                    "s1",
                    outcome="failed",
                    stage="assembly",
                    error="renderer failed",
                )

            terminal_rows = [
                row
                for row in ledger.read_event_rows("s1")
                if row["event_type"] == "session_terminal"
            ]
            self.assertEqual(len(terminal_rows), 1)
            self.assertEqual(terminal_rows[0]["summary"]["outcome"], "completed")

    def test_generic_session_terminal_write_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)

            with self.assertRaisesRegex(ValueError, "record_session_terminal"):
                ledger.record_event(
                    "s1",
                    "session_terminal",
                    {"outcome": "completed"},
                    subject_id="s1",
                )

    def test_generic_review_lifecycle_writes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)

            for event_type, writer in (
                ("review_requested", "record_review_requested"),
                ("review_decision", "record_review_decision"),
                ("review_applied", "record_review_applied"),
            ):
                with self.subTest(event_type=event_type):
                    with self.assertRaisesRegex(ValueError, writer):
                        ledger.record_event("s1", event_type, {})

    def test_session_terminal_rejects_unsupported_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)

            with self.assertRaisesRegex(ValueError, "completed.*failed"):
                ledger.record_session_terminal("s1", outcome="cancelled")

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
            _record_automatic_review_failure(**{**kwargs, "elapsed_seconds": 31.0})

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
