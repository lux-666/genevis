"""Tests for v2 derived current-status and session-summary reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.lifecycle import (
    ResumeValidationError,
    inspect_state,
    normalize_resume_review_request,
)
from app.reports.review_report import write_final_review_report, write_operator_report
from app.review.application import (
    ReviewApplicationIdentity,
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
)
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.gates import get_review_gate
from app.review.persistence import persist_review_request
from app.schemas.state import initial_state
from app.storage.ledger import Ledger
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
        "current_stage": stage,
        "workflow_steps": [
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": f"step-001-{stage}",
                "stage": stage,
                "allow_local_execution": False,
                **(
                    {
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-000-inventory",
                        }
                    }
                    if stage == "assembly_review"
                    else {}
                ),
            },
        ],
        "workflow_step_index": 1,
    }
    payload = ReviewPayload(
        stage=stage,
        gate_id=gate_id,
        reason="needs_review",
        decisions_allowed=decisions_allowed,
    )
    return persist_review_request(
        state,
        stage=stage,
        gate_id=gate_id,
        reason="needs_review",
        payload=payload,
    )


def _review_state(paths: SessionPaths, stage: str, **updates: object) -> dict:
    workflow_steps = [
        {
            "step_id": "step-000-inventory",
            "stage": "inventory",
            "allow_local_execution": False,
        },
        {
            "step_id": f"step-001-{stage}",
            "stage": stage,
            "allow_local_execution": False,
            **(
                {
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-000-inventory",
                    }
                }
                if stage == "assembly_review"
                else {}
            ),
        },
    ]
    state = initial_state(
        session_id=paths.session_id,
        input_manifest_ref=paths.to_ref(paths.manifest_path),
        data_dir=str(paths.run_dir),
        workflow_steps=workflow_steps,
        stage_plan_ref="control/run_config.json",
    )
    state.update(
        {
            "current_stage": stage,
            "workflow_step_index": 1,
            "entered_workflow_step_index": 1,
            "supervision_state": "blocked_human",
            "stage_outputs": {},
            **updates,
        }
    )
    return state


def _review_occurrence(
    paths: SessionPaths,
    *,
    stage: str,
    gate_id: str,
    reason: str,
    decisions_allowed: list[str],
    decision: str | None = None,
    applied: bool = False,
):
    """Persist one request and, optionally, its intent/application lifecycle."""

    state = _review_state(paths, stage)
    persisted = persist_review_request(
        state,
        stage=stage,
        gate_id=gate_id,
        reason=reason,
        payload=ReviewPayload(
            stage=stage,
            gate_id=gate_id,
            reason=reason,
            decisions_allowed=decisions_allowed,
        ),
    )
    state.update(
        {
            "supervision_state": "blocked_human",
            "active_interrupt": persisted.compact_interrupt(
                stage=stage,
                gate_id=gate_id,
                reason=reason,
            ),
        }
    )
    ledger = Ledger(paths.run_dir)
    ledger.record_review_requested(
        paths.session_id,
        stage,
        reason=reason,
        review_request_artifact_ref=persisted.request_ref,
        review_request_id=persisted.review_request_id,
    )
    if decision is None:
        return persisted, None

    request = ResumeReviewRequest(
        session_id=paths.session_id,
        stage=stage,
        gate_id=gate_id,
        review_request_id=persisted.review_request_id,
        review_request_hash=persisted.request_hash,
        decision=decision,
    )
    canonical = normalize_resume_review_request(state, request)
    intent = persist_review_intent(state, canonical, source="test")
    gate = get_review_gate(stage, gate_id)
    prepared = gate.load_prepare_application()(state, intent.intent)
    application_ref = persist_review_application(
        state,
        intent=intent,
        prepared=prepared,
    )
    application = load_verified_review_application(paths.run_dir, application_ref)
    identity = ReviewApplicationIdentity.from_intent(intent.intent)
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
                _review_state(paths, "assembly_review", active_interrupt=interrupt)
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
            state = _review_state(
                paths,
                "assembly_review",
                active_interrupt=persisted.compact_interrupt(
                    stage="assembly_review",
                    gate_id="assembly_review",
                    reason="needs_review",
                ),
            )
            wrong_hash = ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                gate_id="assembly_review",
                review_request_id=persisted.review_request_id,
                review_request_hash="0" * 64,
                decision="accept",
            )
            with self.assertRaisesRegex(ResumeValidationError, "review_request_hash"):
                normalize_resume_review_request(state, wrong_hash)

            request = ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                gate_id="assembly_review",
                review_request_id=persisted.review_request_id,
                review_request_hash=persisted.request_hash,
                decision="accept",
            )
            paths.resolve_ref(persisted.request_ref.path_ref).write_text(
                '{"tampered": true}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ResumeValidationError, "integrity mismatch"):
                normalize_resume_review_request(state, request)

    def test_unified_summary_lists_pending_review_requests_and_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            persisted, _application = _review_occurrence(
                paths,
                stage="hic_scaffolding",
                gate_id="hic_scaffolding",
                reason="hic_scaffolding_human_decision_required",
                decisions_allowed=["select_method", "skip"],
            )
            prepared_request, prepared_application = _review_occurrence(
                paths,
                stage="assembly_review",
                gate_id="assembly_review",
                reason="assembly_review_prepared",
                decisions_allowed=["stop_human_review"],
                decision="stop_human_review",
            )
            applied_request, applied_application = _review_occurrence(
                paths,
                stage="assembly_review",
                gate_id="assembly_review",
                reason="assembly_review_applied",
                decisions_allowed=["stop_human_review"],
                decision="stop_human_review",
                applied=True,
            )
            self.assertIsNotNone(prepared_application)
            self.assertIsNotNone(applied_application)

            before_events = len(ledger.read_event_rows("s1"))
            before_index = paths.artifact_index_path.read_bytes()
            report_ref = write_final_review_report(paths.run_dir, "s1")
            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))

            self.assertEqual(
                Path(report_ref), paths.report_path("session_summary.json")
            )
            self.assertEqual(report["report_type"], "session_summary")
            occurrences = {
                item["review_request_id"]: item
                for item in report["pending_review_requests"]
            }
            self.assertEqual(
                occurrences[persisted.review_request_id]["status"],
                "awaiting_decision",
            )
            self.assertFalse(occurrences[persisted.review_request_id]["resolved"])
            self.assertEqual(
                occurrences[prepared_request.review_request_id]["status"],
                "application_prepared",
            )
            self.assertFalse(
                occurrences[prepared_request.review_request_id]["resolved"]
            )
            self.assertEqual(
                occurrences[applied_request.review_request_id]["status"], "applied"
            )
            self.assertTrue(occurrences[applied_request.review_request_id]["resolved"])
            self.assertEqual(
                occurrences[applied_request.review_request_id]["resolution_basis"],
                "review_applied",
            )
            self.assertEqual(report["decisions_by_origin"]["human"], 2)
            self.assertEqual(
                {item["review_request_id"] for item in report["decisions"]},
                {prepared_request.review_request_id, applied_request.review_request_id},
            )
            self.assertEqual(len(ledger.read_event_rows("s1")), before_events)
            self.assertEqual(paths.artifact_index_path.read_bytes(), before_index)

    def test_failed_current_status_is_overwritten_outside_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _run(tmp)
            ledger = Ledger(paths.run_dir)
            ledger.record_session_terminal(
                "s1",
                outcome="failed",
                stage="base_polish",
                error=(
                    "base_polish is registered in the parent graph but has no "
                    "implemented method"
                ),
            )
            before_events = len(ledger.read_event_rows("s1"))
            report_ref = write_operator_report(
                paths.run_dir,
                "s1",
                {
                    "current_stage": "base_polish",
                    "supervision_state": "failed_terminal",
                    "last_error": (
                        "base_polish is registered in the parent graph but has no "
                        "implemented method"
                    ),
                    "route_trace": [
                        "t2t_stage",
                        "base_polish_stage",
                    ],
                },
            )

            report = json.loads(Path(report_ref).read_text(encoding="utf-8"))
            self.assertEqual(Path(report_ref), paths.report_path("current_status.json"))
            self.assertEqual(report["report_type"], "current_status")
            self.assertEqual(report["next_action"]["kind"], "failed_terminal")
            self.assertEqual(
                report["route_trace_tail"],
                ["t2t_stage", "base_polish_stage"],
            )
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
                    **_review_state(paths, "assembly_review"),
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
            self.assertEqual(
                interrupt["review_request_id"], persisted.review_request_id
            )
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
