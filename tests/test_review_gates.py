"""Tests for registry-backed review gates."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import (
    ResumeValidationError,
    build_resume_decision_template,
    inspect_session,
    resume_review_state,
)
from app.storage.ledger import Ledger
from app.entrypoints.review_interactive import prompt_generic_gate_decision
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.gates import ReviewGateDeclaration, get_review_gate, register_review_gate
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_request,
)
from app.review.recommender import recommend_review_decision
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


def _apply_unit_gate_decision(state: dict, request: ResumeReviewRequest) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs[request.stage] = {
        **dict(outputs.get(request.stage) or {}),
        "status": "completed",
        "decision": request.decision,
        "candidate_id": request.candidate_id,
        "decided_by": request.decided_by,
    }
    return {
        **state,
        "stage_outputs": outputs,
        "active_interrupt": None,
        "supervision_state": "running_stage",
        "current_stage": "done",
    }


try:
    register_review_gate(
        ReviewGateDeclaration(
            stage="unit_review_gate",
            gate_id="unit_review_gate",
            decisions_allowed=("accept", "defer_annotation", "stop_human_review"),
            decision_applier="tests.test_review_gates:_apply_unit_gate_decision",
            prompt_bundle="review.assembly_review_recommend",
            template_preferred_decision="accept",
            required_resume_fields=("candidate_id",),
        )
    )
except ValueError:
    pass

for _gate_id in ("gate_a", "gate_b"):
    try:
        register_review_gate(
            ReviewGateDeclaration(
                stage="unit_multi_stage",
                gate_id=_gate_id,
                decisions_allowed=("accept", "stop_human_review"),
                decision_applier="tests.test_review_gates:_apply_unit_gate_decision",
                prompt_bundle="review.assembly_review_recommend",
            )
        )
    except ValueError:
        pass


class ReviewGateRegistryTests(unittest.TestCase):
    def test_registered_gate_exposes_declaration(self):
        gate = get_review_gate("unit_review_gate")

        self.assertEqual(gate.gate_id, "unit_review_gate")
        self.assertEqual(gate.decisions_allowed, ("accept", "defer_annotation", "stop_human_review"))
        self.assertEqual(gate.prompt_bundle, "review.assembly_review_recommend")

    def test_registered_gate_dispatches_resume_applier(self):
        state = _blocked_unit_state()
        interrupt = state["active_interrupt"]
        assert interrupt is not None
        request = ResumeReviewRequest(
            session_id="s1",
            stage="unit_review_gate",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decision="accept",
            candidate_id="candidate-a",
        )

        update = resume_review_state(state, request)

        self.assertIsNone(update["active_interrupt"])
        self.assertEqual(update["current_stage"], "done")
        self.assertEqual(update["stage_outputs"]["unit_review_gate"]["candidate_id"], "candidate-a")

    def test_registered_gate_can_use_local_decision_verb(self):
        state = _blocked_unit_state(decisions_allowed=["defer_annotation", "stop_human_review"])
        interrupt = state["active_interrupt"]
        assert interrupt is not None
        request = ResumeReviewRequest(
            session_id="s1",
            stage="unit_review_gate",
            gate_id="unit_review_gate",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decision="defer_annotation",
            candidate_id="candidate-a",
        )

        update = resume_review_state(state, request)

        self.assertEqual(update["stage_outputs"]["unit_review_gate"]["decision"], "defer_annotation")

    def test_unknown_gate_is_rejected_by_registry_not_schema(self):
        state = _blocked_unit_state(stage="unregistered_gate", decisions_allowed=["accept"])
        interrupt = state["active_interrupt"]
        assert interrupt is not None
        request = ResumeReviewRequest(
            session_id="s1",
            stage="unregistered_gate",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decision="accept",
        )

        with self.assertRaisesRegex(ResumeValidationError, "Unsupported resume stage"):
            resume_review_state(state, request)

    def test_recommender_uses_registry_prompt_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(
                stage="unit_review_gate",
                reason="review",
                candidates=[{"candidate_id": "candidate-a"}],
                decisions_allowed=["accept", "stop_human_review"],
            )
            chat = _FakeChat(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "unit_review_gate",
                        "decision": "accept",
                        "candidate_id": "candidate-a",
                    }
                )
            )
            persisted = persist_review_request(
                {"session_id": "s1", "data_dir": str(run_dir)},
                stage="unit_review_gate",
                gate_id="unit_review_gate",
                reason="review",
                payload=request,
            )

            decision = recommend_review_decision(
                session_id="s1",
                stage="unit_review_gate",
                review_request_id=persisted.review_request_id,
                request_ref=persisted.request_ref,
                request_hash=persisted.request_hash,
                data_dir=run_dir,
                chat_model=chat,
            )

            self.assertEqual(decision.stage, "unit_review_gate")
            artifact_path = SessionPaths.from_run_dir(run_dir).resolve_ref(
                decision.rationale_artifact_ref or ""
            )
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["prompt_bundle"], "review.assembly_review_recommend")

    def test_recommender_uses_gate_id_when_stage_has_multiple_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(
                stage="unit_multi_stage",
                gate_id="gate_b",
                reason="review",
                decisions_allowed=["accept", "stop_human_review"],
            )
            chat = _FakeChat(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "unit_multi_stage",
                        "decision": "accept",
                    }
                )
            )
            persisted = persist_review_request(
                {"session_id": "s1", "data_dir": str(run_dir)},
                stage="unit_multi_stage",
                gate_id="gate_b",
                reason="review",
                payload=request,
            )

            decision = recommend_review_decision(
                session_id="s1",
                stage="unit_multi_stage",
                review_request_id=persisted.review_request_id,
                request_ref=persisted.request_ref,
                request_hash=persisted.request_hash,
                data_dir=run_dir,
                chat_model=chat,
            )

            self.assertEqual(decision.stage, "unit_multi_stage")
            self.assertEqual(decision.gate_id, "gate_b")
            artifact_path = SessionPaths.from_run_dir(run_dir).resolve_ref(
                decision.rationale_artifact_ref or ""
            )
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["review_request_id"], persisted.review_request_id)
            self.assertEqual(artifact["request_hash"], persisted.request_hash)
            self.assertNotIn("review_request", artifact)

    def test_review_request_envelope_is_the_only_owner_of_gate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(
                stage="unit_review_gate",
                gate_id="unit_review_gate",
                reason="unit review",
                candidates=[{"candidate_id": "candidate-a"}],
                decisions_allowed=["accept", "stop_human_review"],
            )
            persisted = persist_review_request(
                {"session_id": "s1", "data_dir": str(run_dir)},
                stage="unit_review_gate",
                gate_id="unit_review_gate",
                reason="unit review",
                payload=request,
            )

            record = load_review_request(
                run_dir,
                persisted.request_ref,
                session_id="s1",
            )

            self.assertEqual(record["stage"], "unit_review_gate")
            self.assertEqual(record["gate_id"], "unit_review_gate")
            self.assertEqual(record["reason"], "unit review")
            self.assertFalse({"stage", "gate_id", "reason"} & set(record["payload"]))

    def test_review_request_rejects_conflicting_envelope_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            state = {"session_id": "s1", "data_dir": str(run_dir)}
            baseline = {
                "stage": "unit_review_gate",
                "gate_id": "unit_review_gate",
                "reason": "unit review",
                "decisions_allowed": ["accept"],
            }
            mismatches = {
                "stage": "different_stage",
                "gate_id": "different_gate",
                "reason": "different reason",
            }

            for field, mismatch in mismatches.items():
                with self.subTest(field=field):
                    payload = {**baseline, field: mismatch}
                    with self.assertRaisesRegex(
                        ReviewPersistenceError,
                        f"payload {field} does not match canonical envelope",
                    ):
                        persist_review_request(
                            state,
                            stage="unit_review_gate",
                            gate_id="unit_review_gate",
                            reason="unit review",
                            payload=payload,
                        )

            with self.subTest(field="gate_id_explicit_none"):
                with self.assertRaisesRegex(
                    ReviewPersistenceError,
                    "payload gate_id does not match canonical envelope",
                ):
                    persist_review_request(
                        state,
                        stage="unit_review_gate",
                        gate_id="unit_review_gate",
                        reason="unit review",
                        payload={**baseline, "gate_id": None},
                    )

    def test_inspect_and_template_use_registered_gate_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _blocked_unit_session(tmp)

            summary = inspect_session(session_id, data_dir)
            template = build_resume_decision_template(summary)

            self.assertEqual(summary["decision_requirements"]["stage"], "unit_review_gate")
            self.assertEqual(summary["decision_requirements"]["gate_id"], "unit_review_gate")
            self.assertEqual(summary["decision_requirements"]["decisions_allowed"], ["accept", "defer_annotation", "stop_human_review"])
            self.assertEqual(template["stage"], "unit_review_gate")
            self.assertEqual(template["gate_id"], "unit_review_gate")
            self.assertEqual(template["decision"], "accept")
            self.assertEqual(template["candidate_id"], "candidate-a")
            self.assertIn("candidate_id", summary["next_action"]["required_fields"])

    def test_generic_prompt_collects_candidate_and_notes(self):
        requirements = {
            "stage": "unit_review_gate",
            "gate_id": "unit_review_gate",
            "decisions_allowed": ["accept", "stop_human_review"],
            "candidate_ids": ["candidate-a", "candidate-b"],
        }
        inputs = iter(["1", "2", "reviewed evidence"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            decision = prompt_generic_gate_decision(requirements)

        self.assertIsNotNone(decision)
        self.assertEqual(decision["stage"], "unit_review_gate")
        self.assertEqual(decision["gate_id"], "unit_review_gate")
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["candidate_id"], "candidate-b")
        self.assertEqual(decision["rejected_candidates"], ["candidate-a"])
        self.assertEqual(decision["notes"], ["reviewed evidence"])


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model_name = "fake-model"

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        return _FakeResponse(self.content)


def _blocked_unit_state(
    *,
    stage: str = "unit_review_gate",
    decisions_allowed: list[str] | None = None,
) -> dict:
    allowed = decisions_allowed or ["accept", "stop_human_review"]
    run_dir = v2_run_dir(tempfile.mkdtemp(), "s1")
    state = {
        "session_id": "s1",
        "thread_id": "s1",
        "data_dir": str(run_dir),
        "control_state": "running",
        "supervision_state": "blocked_human",
        "current_stage": stage,
        "requested_stages": None,
        "input_manifest_ref": "control/manifest.yaml",
        "asset_registry_ref": None,
        "stage_plan_ref": None,
        "active_interrupt": None,
        "last_error": None,
        "review_notes": [],
        "route_trace": [],
        "stage_outputs": {
            stage: {
                "status": "blocked_human",
                "candidates": [{"candidate_id": "candidate-a"}],
            }
        },
        "skip_reasons": {},
    }
    payload = ReviewPayload(
        stage=stage,
        gate_id=stage,
        reason="unit review",
        decisions_allowed=allowed,
        candidates=[{"candidate_id": "candidate-a"}],
    )
    persisted = persist_review_request(
        state,
        stage=stage,
        gate_id=stage,
        reason="unit review",
        payload=payload,
    )
    state["active_interrupt"] = persisted.compact_interrupt(
        stage=stage,
        gate_id=stage,
        reason="unit review",
    )
    return state


def _blocked_unit_session(tmp: str) -> tuple[str, str]:
    session_id = "s1"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    state = {
        "session_id": session_id,
        "data_dir": str(data_dir),
    }
    payload = ReviewPayload(
        stage="unit_review_gate",
        gate_id="unit_review_gate",
        reason="unit review",
        candidates=[{"candidate_id": "candidate-a"}],
        decisions_allowed=["accept", "defer_annotation", "stop_human_review"],
    )
    persisted = persist_review_request(
        state,
        stage="unit_review_gate",
        gate_id="unit_review_gate",
        reason="unit review",
        payload=payload,
        method_name="unit_review_gate.contract",
        artifact_type="unit_review_gate.request",
    )
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "control/manifest.yaml")
    ledger.record_review_requested(
        session_id,
        "unit_review_gate",
        reason="unit review",
        candidates=[{"candidate_id": "candidate-a"}],
        review_request_artifact_ref=persisted.request_ref,
        review_request_id=persisted.review_request_id,
    )
    return session_id, str(data_dir)


if __name__ == "__main__":
    unittest.main()
