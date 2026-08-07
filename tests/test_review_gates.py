"""Tests for registry-backed review gates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import (
    ResumeValidationError,
    build_foreground_auto_resume_request,
    build_resume_decision_template,
    foreground_automatic_decision_label,
    inspect_session,
    resume_review_state,
)
from app.entrypoints.review_interactive import prompt_generic_gate_decision
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.gates import (
    ReviewGateDeclaration,
    get_review_gate,
    planned_review_gates,
    register_review_gate,
    review_prompt_bundle,
)
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_request,
)
from app.storage.frozen_inputs import freeze_session_inputs
from app.storage.ledger import Ledger
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
            stage="inventory",
            gate_id="unit_review_gate",
            decisions_allowed=("accept", "defer", "stop_human_review"),
            decision_applier="tests.test_review_gates:_apply_unit_gate_decision",
            template_preferred_decision="accept",
            required_resume_fields=("candidate_id",),
        )
    )
except ValueError:
    pass


class ReviewGateRegistryTests(unittest.TestCase):
    def test_registered_gate_exposes_declaration(self):
        gate = get_review_gate("inventory", "unit_review_gate")

        self.assertEqual(gate.gate_id, "unit_review_gate")
        self.assertEqual(
            gate.decisions_allowed, ("accept", "defer", "stop_human_review")
        )
        self.assertEqual(gate.template_preferred_decision, "accept")

    def test_planned_review_gates_preserves_repeated_stage_occurrences(self):
        planned = planned_review_gates(
            ["inventory", "assembly", "assembly_review", "assembly"]
        )

        self.assertEqual(
            [gate.stage for gate in planned],
            [
                "inventory",
                "inventory",
                "assembly",
                "assembly",
                "assembly_review",
                "assembly",
                "assembly",
            ],
        )
        self.assertEqual(
            [gate.gate_id for gate in planned[2:4]],
            [gate.gate_id for gate in planned[5:]],
        )

    def test_registered_gate_dispatches_resume_applier(self):
        state = _blocked_unit_state()
        interrupt = state["active_interrupt"]
        assert interrupt is not None
        request = ResumeReviewRequest(
            session_id="s1",
            stage="inventory",
            gate_id="unit_review_gate",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decision="accept",
            candidate_id="candidate-a",
        )

        update = resume_review_state(state, request)

        self.assertIsNone(update["active_interrupt"])
        self.assertEqual(update["current_stage"], "done")
        self.assertEqual(
            update["stage_outputs"]["inventory"]["candidate_id"], "candidate-a"
        )

    def test_registered_gate_can_use_local_decision_verb(self):
        state = _blocked_unit_state(decisions_allowed=["defer", "stop_human_review"])
        interrupt = state["active_interrupt"]
        assert interrupt is not None
        request = ResumeReviewRequest(
            session_id="s1",
            stage="inventory",
            gate_id="unit_review_gate",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decision="defer",
            candidate_id="candidate-a",
        )

        update = resume_review_state(state, request)

        self.assertEqual(update["stage_outputs"]["inventory"]["decision"], "defer")

    def test_unknown_gate_is_rejected_by_registry_not_schema(self):
        state = _blocked_unit_state(stage="genome_survey", decisions_allowed=["accept"])
        interrupt = state["active_interrupt"]
        assert interrupt is not None
        request = ResumeReviewRequest(
            session_id="s1",
            stage="genome_survey",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decision="accept",
        )

        with self.assertRaisesRegex(ResumeValidationError, "Unsupported resume stage"):
            resume_review_state(state, request)

    def test_review_request_envelope_is_the_only_owner_of_gate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(
                stage="inventory",
                gate_id="unit_review_gate",
                reason="unit review",
                candidates=[{"candidate_id": "candidate-a"}],
                decisions_allowed=["accept", "stop_human_review"],
            )
            persisted = persist_review_request(
                _review_state(run_dir, stage="inventory"),
                stage="inventory",
                gate_id="unit_review_gate",
                reason="unit review",
                payload=request,
            )

            record = load_review_request(
                run_dir,
                persisted.request_ref,
                session_id="s1",
            )

            self.assertEqual(record["stage"], "inventory")
            self.assertEqual(record["gate_id"], "unit_review_gate")
            self.assertEqual(record["reason"], "unit review")
            self.assertFalse({"stage", "gate_id", "reason"} & set(record["payload"]))

    def test_review_request_rejects_conflicting_envelope_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            state = _review_state(run_dir, stage="inventory")
            baseline = {
                "stage": "inventory",
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
                            stage="inventory",
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
                        stage="inventory",
                        gate_id="unit_review_gate",
                        reason="unit review",
                        payload={**baseline, "gate_id": None},
                    )

    def test_inspect_and_template_use_registered_gate_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _blocked_unit_session(tmp)

            summary = inspect_session(session_id, data_dir)
            template = build_resume_decision_template(summary)

            self.assertEqual(summary["decision_requirements"]["stage"], "inventory")
            self.assertEqual(
                summary["decision_requirements"]["gate_id"], "unit_review_gate"
            )
            self.assertEqual(
                summary["decision_requirements"]["decisions_allowed"],
                ["accept", "defer", "stop_human_review"],
            )
            self.assertEqual(template["stage"], "inventory")
            self.assertEqual(template["gate_id"], "unit_review_gate")
            self.assertEqual(template["decision"], "accept")
            self.assertEqual(template["candidate_id"], "candidate-a")
            self.assertIn("candidate_id", summary["next_action"]["required_fields"])

    def test_parameter_approval_gates_default_templates_to_approval(self):
        for stage in ("assembly", "hic_scaffolding", "t2t"):
            with self.subTest(stage=stage):
                gate = get_review_gate(stage, "parameter_approval")
                self.assertEqual(
                    gate.decisions_allowed,
                    ("approve_parameters", "reject_parameters"),
                )
                template = build_resume_decision_template(
                    {
                        "session_id": "s1",
                        "decision_requirements": {
                            "stage": stage,
                            "gate_id": "parameter_approval",
                            "decisions_allowed": list(gate.decisions_allowed),
                            "review_request_id": "request-1",
                            "request_hash": "hash-1",
                        },
                    }
                )

                self.assertEqual(template["decision"], "approve_parameters")

    def test_every_production_gate_freezes_the_effective_wait(self):
        production_gates = (
            ("assembly", "parameter_approval"),
            ("inventory", "inventory_review"),
            ("assembly_review", "assembly_review"),
            ("assembly", "route_selection"),
            ("hic_scaffolding", "parameter_approval"),
            ("hic_scaffolding", "hic_scaffolding"),
            ("t2t", "parameter_approval"),
            ("t2t", "t2t_review"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            for stage, gate_id in production_gates:
                with self.subTest(stage=stage, gate_id=gate_id):
                    gate = get_review_gate(stage, gate_id)
                    state = _review_state(run_dir, stage=stage)
                    state["stage_outputs"] = {
                        stage: {"review_policy": {"max_blocked_seconds": 17}}
                    }
                    persisted = persist_review_request(
                        state,
                        stage=stage,
                        gate_id=gate_id,
                        reason="registry timeout contract",
                        payload={
                            "decisions_allowed": list(gate.decisions_allowed),
                        },
                    )
                    record = load_review_request(
                        run_dir,
                        persisted.request_ref,
                        session_id="s1",
                    )

                    self.assertEqual(record["payload"]["max_blocked_seconds"], 17)
                    with self.assertRaisesRegex(
                        ReviewPersistenceError,
                        "does not match the effective frozen RunConfig policy",
                    ):
                        persist_review_request(
                            state,
                            stage=stage,
                            gate_id=gate_id,
                            reason="registry timeout mismatch",
                            payload={
                                "decisions_allowed": list(gate.decisions_allowed),
                                "max_blocked_seconds": 18,
                            },
                        )

    def test_foreground_auto_request_requires_a_registered_resolver(self):
        """The wait is only a trigger; template defaults are never an accept rule."""
        base = {
            "session_id": "s1",
            "decision_requirements": {
                "review_request_id": "request-1",
                "request_hash": "a" * 64,
                "max_blocked_seconds": 0,
            },
        }
        manual_gates = (
            ("inventory", "inventory_review"),
            ("hic_scaffolding", "hic_scaffolding"),
            ("t2t", "t2t_review"),
        )
        for stage, gate_id in manual_gates:
            with self.subTest(stage=stage, gate_id=gate_id):
                gate = get_review_gate(stage, gate_id)
                summary = {
                    **base,
                    "decision_requirements": {
                        **base["decision_requirements"],
                        "stage": stage,
                        "gate_id": gate_id,
                        "review_request_artifact_ref": (
                            f"artifacts/steps/step-001-{stage}/{stage}/reviews/"
                            "request-1/request.json"
                        ),
                        "decisions_allowed": list(gate.decisions_allowed),
                    },
                }
                with self.assertRaisesRegex(
                    ResumeValidationError, "no eligible automatic resolver"
                ):
                    build_foreground_auto_resume_request(
                        summary,
                        configured_seconds=0,
                        elapsed_seconds=0,
                        data_dir="/tmp/run",
                    )

    def test_route_selection_builds_foreground_auto_request_from_its_resolver(self):
        summary = {
            "session_id": "s1",
            "decision_requirements": {
                "stage": "assembly",
                "gate_id": "route_selection",
                "decisions_allowed": ["select_hifi", "stop_human_review"],
                "review_request_id": "request-1",
                "request_hash": "a" * 64,
                "review_request_artifact_ref": (
                    "artifacts/steps/step-001-assembly/assembly/reviews/"
                    "request-1/request.json"
                ),
                "request_ref": {
                    "path_ref": (
                        "artifacts/steps/step-001-assembly/assembly/reviews/"
                        "request-1/request.json"
                    ),
                    "sha256": "a" * 64,
                },
                "max_blocked_seconds": 0,
                "recommended_route": "hifi",
                "recommended_assembly_selection": {
                    "primary_method": "assembly.candidate.hifiasm"
                },
            },
        }

        request = build_foreground_auto_resume_request(
            summary,
            configured_seconds=0,
            elapsed_seconds=0,
            data_dir="/tmp/run",
        )

        self.assertEqual(request.decision, "select_hifi")
        self.assertEqual(request.method_name, "assembly.candidate.hifiasm")
        self.assertEqual(request.decided_by, "auto_rule")

    def test_assembly_review_expiry_uses_llm_provisional_recommendation(self):
        summary = {
            "session_id": "s1",
            "decision_requirements": {
                "stage": "assembly_review",
                "gate_id": "assembly_review",
                "decisions_allowed": ["accept", "stop_human_review"],
                "review_request_id": "request-1",
                "request_hash": "a" * 64,
                "review_request_artifact_ref": (
                    "artifacts/steps/step-002-assembly_review/assembly_review/reviews/"
                    "request-1/request.json"
                ),
                "request_ref": {
                    "path_ref": (
                        "artifacts/steps/step-002-assembly_review/assembly_review/"
                        "reviews/request-1/request.json"
                    ),
                    "sha256": "a" * 64,
                },
                "max_blocked_seconds": 0,
                "auto_decidable": True,
            },
        }
        recommendation = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            gate_id="assembly_review",
            decision="accept",
            candidate_id="candidate-a",
            review_request_id="request-1",
            review_request_hash="a" * 64,
            decided_by="llm_provisional",
            rationale_artifact_ref=(
                "artifacts/steps/step-002-assembly_review/assembly_review/reviews/"
                "request-1/recommendation.json"
            ),
        )

        with patch(
            "app.review.recommender.recommend_review_decision",
            return_value=recommendation,
        ) as recommend:
            request = build_foreground_auto_resume_request(
                summary,
                configured_seconds=0,
                elapsed_seconds=0,
                data_dir="/tmp/run",
            )

        recommend.assert_called_once_with(
            session_id="s1",
            stage="assembly_review",
            review_request_id="request-1",
            request_ref={
                "path_ref": (
                    "artifacts/steps/step-002-assembly_review/assembly_review/"
                    "reviews/request-1/request.json"
                ),
                "sha256": "a" * 64,
            },
            request_hash="a" * 64,
            data_dir="/tmp/run",
        )
        self.assertEqual(request.decided_by, "llm_provisional")
        self.assertEqual(request.candidate_id, "candidate-a")
        self.assertEqual(
            request.rationale_artifact_ref,
            (
                "artifacts/steps/step-002-assembly_review/assembly_review/reviews/"
                "request-1/recommendation.json"
            ),
        )
        self.assertEqual(request.metadata["source"], "foreground_countdown")

    def test_ineligible_assembly_review_has_no_foreground_automatic_label(self):
        summary = {
            "decision_requirements": {
                "stage": "assembly_review",
                "gate_id": "assembly_review",
                "auto_decidable": False,
            }
        }

        self.assertIsNone(foreground_automatic_decision_label(summary))

    def test_only_assembly_review_declares_an_llm_recommendation_bundle(self):
        self.assertEqual(
            review_prompt_bundle("assembly_review", "assembly_review"),
            "review.assembly_review_recommend",
        )
        for stage, gate_id in (
            ("inventory", "inventory_review"),
            ("assembly", "parameter_approval"),
            ("hic_scaffolding", "hic_scaffolding"),
            ("t2t", "t2t_review"),
        ):
            with self.subTest(stage=stage, gate_id=gate_id):
                self.assertIsNone(review_prompt_bundle(stage, gate_id))

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


def _blocked_unit_state(
    *,
    stage: str = "inventory",
    decisions_allowed: list[str] | None = None,
) -> dict:
    allowed = decisions_allowed or ["accept", "stop_human_review"]
    gate_id = "unit_review_gate" if stage == "inventory" else stage
    run_dir = v2_run_dir(tempfile.mkdtemp(), "s1")
    state = _review_state(run_dir, stage=stage)
    state["supervision_state"] = "blocked_human"
    state["stage_outputs"] = {
        stage: {
            "status": "blocked_human",
            "candidates": [{"candidate_id": "candidate-a"}],
        }
    }
    payload = ReviewPayload(
        stage=stage,
        gate_id=gate_id,
        reason="unit review",
        decisions_allowed=allowed,
        candidates=[{"candidate_id": "candidate-a"}],
    )
    persisted = persist_review_request(
        state,
        stage=stage,
        gate_id=gate_id,
        reason="unit review",
        payload=payload,
    )
    state["active_interrupt"] = persisted.compact_interrupt(
        stage=stage,
        gate_id=gate_id,
        reason="unit review",
    )
    return state


def _blocked_unit_session(tmp: str) -> tuple[str, str]:
    session_id = "s1"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    reads = data_dir / "fixture_reads.fastq"
    reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
    freeze_session_inputs(
        SessionPaths.from_run_dir(data_dir),
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
        run_config_draft={"requested_stages": []},
        selected_methods={},
        probe_environments=False,
    )
    state = _review_state(data_dir, stage="inventory")
    payload = ReviewPayload(
        stage="inventory",
        gate_id="unit_review_gate",
        reason="unit review",
        candidates=[{"candidate_id": "candidate-a"}],
        decisions_allowed=["accept", "defer", "stop_human_review"],
    )
    persisted = persist_review_request(
        state,
        stage="inventory",
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
        "inventory",
        reason="unit review",
        candidates=[{"candidate_id": "candidate-a"}],
        review_request_artifact_ref=persisted.request_ref,
        review_request_id=persisted.review_request_id,
    )
    return session_id, str(data_dir)


def _review_state(run_dir: Path, *, stage: str) -> dict:
    """Minimal frozen occurrence state for review persistence tests."""

    stages = ["inventory"] if stage == "inventory" else ["inventory", stage]
    if stage in {"assembly", "t2t"}:
        stages.append("assembly_review")
    step_index = stages.index(stage)
    return {
        "session_id": "s1",
        "thread_id": "s1",
        "data_dir": str(run_dir),
        "control_state": "running",
        "supervision_state": "running_stage",
        "current_stage": stage,
        "workflow_steps": [
            {
                "step_id": f"step-{index:03d}-{item}",
                "stage": item,
                "allow_local_execution": False,
                **(
                    {
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": (
                                f"step-{index - 1:03d}-{stages[index - 1]}"
                            ),
                        }
                    }
                    if item == "assembly_review"
                    else {}
                ),
            }
            for index, item in enumerate(stages)
        ],
        "workflow_step_index": step_index,
        "entered_workflow_step_index": step_index,
        "input_manifest_ref": "control/manifest.yaml",
        "asset_registry_ref": None,
        "stage_plan_ref": "control/run_config.json",
        "stage_seeds": {},
        "active_interrupt": None,
        "last_error": None,
        "review_notes": [],
        "route_trace": [],
        "stage_outputs": {},
        "resource_refs": {},
        "skip_reasons": {},
    }


if __name__ == "__main__":
    unittest.main()
