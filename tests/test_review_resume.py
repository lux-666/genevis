"""Tests for review inspect and in-process resume helpers."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.control.lifecycle import (
    ResumeValidationError,
    _decision_requirements_from_artifact,
    build_foreground_auto_resume_request,
    build_resume_decision_template,
    inspect_session,
    inspect_state,
    resume_review_state,
    review_max_blocked_seconds,
)
from app.reports.review_report import write_operator_report
from app.review.base import ResumeReviewRequest, load_resume_review_request
from app.review.persistence import persist_review_decision, persist_review_request
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactMeta,
    AssemblyCandidateBundle,
)
from app.schemas.resource_ref import ResourceRef
from app.storage.artifacts import publish_artifact_record
from app.storage.frozen_inputs import freeze_session_inputs, frozen_execution_lock
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class ReviewResumeTests(unittest.TestCase):
    def test_non_human_resume_requires_rationale_artifact_ref(self):
        with self.assertRaises(ValidationError) as ctx:
            ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
                decided_by="llm_provisional",
            )

        self.assertIn("rationale_artifact_ref", str(ctx.exception))

    def test_llm_provisional_resume_request_is_valid_with_rationale(self):
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            decided_by="llm_provisional",
            rationale_artifact_ref=(
                "artifacts/steps/step-002-assembly_review/assembly_review/"
                "recommendation.json"
            ),
        )

        self.assertEqual(request.decided_by, "llm_provisional")

    def test_inspect_blocked_assembly_state_is_non_mutating(self):
        state = _assembly_blocked_state()
        before = copy.deepcopy(state)

        summary = inspect_state(state)

        self.assertEqual(state, before)
        self.assertEqual(summary["supervision_state"], "blocked_human")
        self.assertEqual(summary["decision_requirements"]["stage"], "assembly_review")
        self.assertIn("accept", summary["decision_requirements"]["decisions_allowed"])
        self.assertIn(
            "review_request_artifact_ref",
            summary["latest_artifact_refs"]["assembly_review"],
        )
        self.assertEqual(
            summary["decision_requirements"]["workflow_step_id"],
            "step-002-assembly_review",
        )

    def test_resume_rejects_interrupt_bound_to_another_stage_occurrence(self):
        state = _assembly_blocked_state()
        state.update(
            {
                "workflow_steps": [
                    {
                        "step_id": "step-000-inventory",
                        "stage": "inventory",
                        "allow_local_execution": False,
                    },
                    {
                        "step_id": "step-001-assembly_review",
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-000-inventory",
                        },
                    },
                    {
                        "step_id": "step-002-hic_scaffolding",
                        "stage": "hic_scaffolding",
                        "allow_local_execution": False,
                    },
                    {
                        "step_id": "step-003-assembly_review",
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-002-hic_scaffolding",
                        },
                    },
                ],
                "workflow_step_index": 3,
                "entered_workflow_step_index": 3,
            }
        )
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(
            ResumeValidationError,
            "does not match the current frozen workflow step",
        ):
            resume_review_state(state, request)

    def test_resume_assembly_review_accept_advances_to_hic(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            rejected_candidates=["canu"],
            notes=["human accepted flye"],
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        self.assertIsNone(update["active_interrupt"])
        self.assertEqual(update["current_stage"], "hic_scaffolding")
        self.assertEqual(update["supervision_state"], "running_stage")
        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertEqual(ar_out["status"], "completed")
        self.assertEqual(ar_out["selected_candidate_id"], "flye")
        self.assertEqual(ar_out["action_type"], "pass")
        self.assertEqual(
            ar_out["pre_action_assembly_ref"],
            str(_candidate_output_path(state, "flye")),
        )
        self.assertEqual(
            update["resource_refs"]["assembly"]["paths"],
            [str(_candidate_output_path(state, "flye"))],
        )
        self.assertTrue(
            _artifact_path(state, ar_out["review_decision_artifact_ref"]).exists()
        )
        self.assertTrue(
            _artifact_path(
                state, ar_out["assembly_review_action_artifact_ref"]
            ).exists()
        )
        events = _read_events(state)
        self.assertEqual(events[-1]["event_type"], "review_decision")
        self.assertEqual(events[-1]["candidate_id"], "flye")
        decision_record = json.loads(
            _artifact_path(state, ar_out["review_decision_artifact_ref"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(decision_record["decision"]["metadata"]["action_type"], "pass")

    def test_resume_assembly_review_pass_advances_to_hic(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="pass",
            candidate_id="flye",
            notes=["human passed flye through unchanged"],
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        self.assertEqual(update["current_stage"], "hic_scaffolding")
        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertEqual(ar_out["decision"], "pass")
        self.assertEqual(ar_out["action_type"], "pass")
        self.assertEqual(ar_out["action_execution_mode"], "none")
        self.assertEqual(
            update["resource_refs"]["assembly"]["paths"],
            [str(_candidate_output_path(state, "flye"))],
        )

    def test_resume_assembly_purge_requires_materialized_output(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="purge",
            candidate_id="flye",
            method_name="external_purge_dups",
            metadata={
                "human_approved_action": True,
                "action_output_ref": (
                    "work/steps/step-002-assembly_review/assembly_review/"
                    "flye.purged.fasta"
                ),
                "evidence_refs": ["assembly_eval_bundle.json"],
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "must exist"):
            resume_review_state(state, request)

    def test_resume_assembly_purge_requires_action_evidence_metadata(self):
        state = _assembly_blocked_state()
        purged = Path(state["data_dir"]) / "flye.purged.fasta"
        purged.write_text(">ctg1\nACGT\n", encoding="utf-8")
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="purge",
            candidate_id="flye",
            method_name="external_purge_dups",
            notes=["human note alone is not enough provenance"],
            metadata={
                "human_approved_action": True,
                "action_output_ref": str(purged),
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "evidence metadata"):
            resume_review_state(state, request)

    def test_resume_assembly_purge_requires_method_or_provenance_metadata(self):
        state = _assembly_blocked_state()
        purged = Path(state["data_dir"]) / "flye.purged.fasta"
        purged.write_text(">ctg1\nACGT\n", encoding="utf-8")
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="purge",
            candidate_id="flye",
            metadata={
                "human_approved_action": True,
                "action_output_ref": str(purged),
                "evidence_refs": ["assembly_eval_bundle.json"],
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "method or provenance"):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_action_output_equal_to_input(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="purge",
            candidate_id="flye",
            method_name="external_purge_dups",
            metadata={
                "human_approved_action": True,
                "action_output_ref": str(_candidate_output_path(state, "flye")),
                "evidence_refs": ["assembly_eval_bundle.json"],
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "must differ"):
            resume_review_state(state, request)

    def test_resume_assembly_purge_promotes_human_supplied_purged_ref(self):
        state = _assembly_blocked_state()
        purged = Path(state["data_dir"]) / "flye.purged.fasta"
        purged.write_text(">ctg1\nACGT\n", encoding="utf-8")
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="purge",
            candidate_id="flye",
            method_name="external_purge_dups",
            notes=["human approved purge after duplicated BUSCO review"],
            metadata={
                "human_approved_action": True,
                "action_output_ref": str(purged),
                "evidence_refs": ["assembly_eval_bundle.json"],
            },
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertEqual(update["current_stage"], "hic_scaffolding")
        self.assertEqual(ar_out["action_type"], "purge")
        self.assertEqual(ar_out["action_execution_mode"], "external")
        self.assertEqual(
            ar_out["pre_action_assembly_ref"],
            str(_candidate_output_path(state, "flye")),
        )
        self.assertNotEqual(ar_out["purged_assembly"], str(purged))
        self.assertTrue(Path(ar_out["purged_assembly"]).is_file())
        self.assertEqual(
            update["resource_refs"]["assembly"]["paths"],
            [ar_out["purged_assembly"]],
        )
        action = json.loads(
            _artifact_path(
                state, ar_out["assembly_review_action_artifact_ref"]
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(action["action_type"], "purge")
        self.assertEqual(
            action["pre_action_assembly_ref"],
            str(_candidate_output_path(state, "flye")),
        )
        self.assertEqual(action["output_assembly_ref"], ar_out["purged_assembly"])
        decision_record = json.loads(
            _artifact_path(state, ar_out["review_decision_artifact_ref"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            decision_record["decision"]["metadata"]["action_type"], "purge"
        )
        self.assertEqual(
            decision_record["decision"]["metadata"]["post_action_assembly_ref"],
            Path(ar_out["purged_assembly"])
            .relative_to(Path(state["data_dir"]))
            .as_posix(),
        )

    def test_resume_assembly_polish_requires_action_output_ref(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="polish",
            candidate_id="flye",
            metadata={
                "human_approved_action": True,
                "evidence_notes": ["reviewed polishing report"],
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "action_output_ref"):
            resume_review_state(state, request)

    def test_resume_assembly_short_read_polish_requires_short_read_evidence(self):
        state = _assembly_blocked_state()
        polished = Path(state["data_dir"]) / "flye.polished.fasta"
        polished.write_text(">ctg1\nACGT\n", encoding="utf-8")
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="polish",
            candidate_id="flye",
            method_name="pilon_shortread",
            notes=["human approved polish"],
            metadata={
                "human_approved_action": True,
                "action_output_ref": str(polished),
                "evidence_notes": ["polish requested"],
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "short-read"):
            resume_review_state(state, request)

    def test_resume_assembly_honors_interrupt_decisions_allowed(self):
        state = _assembly_blocked_state(decisions_allowed=["stop_human_review"])
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "not allowed"):
            resume_review_state(state, request)

    def test_empty_decisions_allowed_rejects_all_resume_decisions(self):
        state = _assembly_blocked_state(decisions_allowed=[])
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "not allowed"):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_unknown_candidate(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="unknown",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "unknown"):
            resume_review_state(state, request)

    def test_resume_assembly_stop_records_failed_terminal(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="stop_human_review",
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        self.assertIsNone(update["active_interrupt"])
        self.assertEqual(update["supervision_state"], "failed_terminal")
        self.assertIn("stopped", update["last_error"])
        self.assertTrue(
            _artifact_path(
                state,
                update["stage_outputs"]["assembly_review"][
                    "review_decision_artifact_ref"
                ],
            ).exists()
        )

    def test_resume_rejects_stage_mismatch(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="skip",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "active_interrupt"):
            resume_review_state(state, request)

    def test_inspect_blocked_hic_state_includes_autohic_adoption_result(self):
        state = _hic_blocked_state()

        summary = inspect_state(state)

        self.assertEqual(summary["decision_requirements"]["stage"], "hic_scaffolding")
        self.assertIn(
            "hic_scaffolding.candidate.autohic",
            summary["decision_requirements"]["executed_candidate_methods"],
        )
        proposal = summary["decision_requirements"]["runtime_proposal"]
        self.assertEqual(proposal["kind"], "autohic_result_review")
        self.assertEqual(proposal["parameter_recommendation_status"], "implemented")
        self.assertEqual(
            proposal["related_parameter_gate"],
            {"stage": "hic_scaffolding", "gate_id": "parameter_approval"},
        )

    def test_assembly_review_runtime_proposal_points_retry_to_v3_parameter_gate(self):
        summary = inspect_state(_assembly_blocked_state())

        proposal = summary["decision_requirements"]["runtime_proposal"]
        self.assertEqual(proposal["kind"], "assembly_candidate_review")
        self.assertEqual(proposal["parameter_recommendation_status"], "mixed")
        self.assertEqual(
            proposal["related_parameter_gate"],
            {"stage": "assembly", "gate_id": "parameter_approval"},
        )
        notes = proposal["parameter_recommendation_notes"]
        self.assertTrue(any("canonical v3" in note for note in notes))
        self.assertNotIn(
            "rerun",
            summary["decision_requirements"]["decisions_allowed"],
        )
        template = build_resume_decision_template(summary)
        self.assertEqual(
            template["metadata"]["template_guidance"]["runtime_proposal"], proposal
        )

    def test_inspect_hic_uses_interrupt_decisions_allowed(self):
        state = _hic_blocked_state(decisions_allowed=["keep_prior_assembly"])

        summary = inspect_state(state)

        self.assertEqual(
            summary["decision_requirements"]["decisions_allowed"],
            ["keep_prior_assembly"],
        )

    def test_resume_hic_accepts_autohic_result_without_method_selection(self):
        state = _hic_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="accept_candidate",
            notes=["contact map reviewed before promotion"],
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        self.assertIsNone(update["active_interrupt"])
        self.assertEqual(update["current_stage"], "gapcloser")
        hic_out = update["stage_outputs"]["hic_scaffolding"]
        self.assertEqual(
            hic_out["selected_method_name"], "hic_scaffolding.candidate.autohic"
        )
        self.assertEqual(
            hic_out["output_assembly_ref"],
            state["stage_outputs"]["hic_scaffolding"]["method_candidates"][
                "hic_scaffolding.candidate.autohic"
            ]["scaffold_fasta"],
        )
        self.assertTrue(
            _artifact_path(state, hic_out["review_decision_artifact_ref"]).exists()
        )

    def test_resume_hic_honors_interrupt_decisions_allowed(self):
        state = _hic_blocked_state(decisions_allowed=["keep_prior_assembly"])
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="accept_candidate",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "not allowed"):
            resume_review_state(state, request)

    def test_resume_hic_rejects_unknown_method_identity(self):
        state = _hic_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="accept_candidate",
            method_name="hic_scaffolding.candidate.other",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "method must be"):
            resume_review_state(state, request)

    def test_resume_hic_keep_prior_advances_without_skip_artifact(self):
        state = _hic_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="keep_prior_assembly",
            notes=["keep prior assembly"],
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        self.assertEqual(update["current_stage"], "gapcloser")
        hic_out = update["stage_outputs"]["hic_scaffolding"]
        self.assertEqual(
            hic_out["output_assembly_ref"],
            state["stage_outputs"]["hic_scaffolding"]["pre_hic_assembly_ref"],
        )
        self.assertTrue(
            _artifact_path(state, hic_out["review_decision_artifact_ref"]).exists()
        )
        artifact_index = Path(state["data_dir"]) / "artifacts" / "index.jsonl"
        artifact_types = [
            json.loads(line)["artifact_type"]
            for line in artifact_index.read_text(encoding="utf-8").splitlines()
        ]
        self.assertNotIn("hic_scaffolding.skip", artifact_types)
        self.assertIn("hic_scaffolding.adoption_review.decision", artifact_types)

    def test_manual_resume_does_not_infer_expired_foreground_deadline(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["blocked_at"] = "2026-05-08T10:00:00+00:00"
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        self.assertEqual(update["supervision_state"], "running_stage")
        self.assertEqual(update["current_stage"], "hic_scaffolding")
        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertTrue(
            _artifact_path(state, ar_out["review_decision_artifact_ref"]).exists()
        )
        self.assertEqual(ar_out["decision"], "accept")
        self.assertNotIn("blocked_timeout", ar_out)

    def test_foreground_timeout_applies_bound_llm_recommendation(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["blocked_at"] = "2026-05-08T10:00:00+00:00"
        summary = inspect_state(state)
        configured = review_max_blocked_seconds(summary)
        recommendation = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            decided_by="llm_provisional",
            rationale_artifact_ref=(
                "artifacts/steps/step-002-assembly_review/assembly_review/reviews/"
                "review_assembly/recommendation.json"
            ),
            **_review_binding(state),
        )
        with patch(
            "app.review.recommender.recommend_review_decision",
            return_value=recommendation,
        ):
            request = build_foreground_auto_resume_request(
                summary,
                configured_seconds=configured,
                elapsed_seconds=301.0,
                data_dir=state["data_dir"],
            )

        update = resume_review_state(
            state,
            request,
            blocked_timeout_seconds=configured,
            decision_source="foreground_countdown",
            now=datetime(2026, 5, 8, 10, 5, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(update["supervision_state"], "running_stage")
        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertEqual(ar_out["decision"], "accept")
        self.assertEqual(ar_out["decided_by"], "llm_provisional")
        self.assertEqual(
            ar_out["rationale_artifact_ref"],
            (
                "artifacts/steps/step-002-assembly_review/assembly_review/reviews/"
                "review_assembly/recommendation.json"
            ),
        )
        self.assertEqual(ar_out["selected_candidate_id"], "flye")

    def test_resume_request_rejects_extra_fields(self):
        with self.assertRaises(ValidationError):
            ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
                unexpected=True,
            )

    def test_resume_rejects_decision_not_allowed_by_gate(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="approve",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "not allowed"):
            resume_review_state(state, request)

    def test_inspect_session_reads_durable_records(self):
        state = _assembly_blocked_state()

        summary = inspect_session("s1", state["data_dir"])

        self.assertEqual(summary["checkpoint_persistence"], "memory_only")
        self.assertFalse(summary["durable_resume_available"])
        self.assertEqual(
            summary["latest_review_request"]["event_type"], "review_requested"
        )
        self.assertEqual(
            summary["latest_unresolved_review_request"]["event_type"],
            "review_requested",
        )
        self.assertEqual(summary["supervision_state"], "blocked_human")

    def test_inspect_session_exposes_decision_requirements_from_artifact(self):
        # F8: inspect_session reads decisions_allowed/candidate_ids out of
        # the latest review request artifact JSON so the operator does not
        # have to open it manually before writing a decision file.
        state = _assembly_blocked_state()

        summary = inspect_session("s1", state["data_dir"])

        requirements = summary["decision_requirements"]
        self.assertIsNotNone(requirements)
        self.assertEqual(requirements["stage"], "assembly_review")
        self.assertEqual(
            sorted(requirements["decisions_allowed"]),
            sorted(
                [
                    "pass",
                    "accept",
                    "purge",
                    "polish",
                    "continue_low_confidence",
                    "stop_human_review",
                ]
            ),
        )
        self.assertEqual(sorted(requirements["candidate_ids"]), ["canu", "flye"])
        self.assertTrue(requirements["requires_structured_resume"])
        self.assertEqual(
            requirements["report_refs"]["assembly_review_report"],
            "reports/assembly_review.md",
        )
        self.assertEqual(
            requirements["candidate_comparison_context"]["candidate_comparison_ref"],
            "candidate_comparison.json",
        )

    def test_resume_decision_template_includes_report_guidance(self):
        state = _assembly_blocked_state()

        summary = inspect_session("s1", state["data_dir"])
        template = build_resume_decision_template(summary)

        self.assertEqual(template["gate_id"], "assembly_review")
        guidance = template["metadata"]["template_guidance"]
        self.assertEqual(
            guidance["report_refs"]["assembly_review_report"],
            "reports/assembly_review.md",
        )
        self.assertEqual(
            guidance["candidate_comparison_context"]["candidate_comparison_ref"],
            "candidate_comparison.json",
        )
        self.assertEqual(sorted(guidance["candidate_ids"]), ["canu", "flye"])

    def test_parameter_approval_template_copies_exact_resume_bindings(self):
        bindings = {
            "attempt_id": 2,
            "task": "retry_after_review_failure",
            "previous_attempt_id": 1,
            "recommendation_hash": "a" * 64,
        }
        summary = {
            "session_id": "s1",
            "decision_requirements": {
                "stage": "assembly",
                "gate_id": "parameter_approval",
                "decisions_allowed": ["approve_parameters", "reject_parameters"],
                "resume_metadata": bindings,
            },
        }

        template = build_resume_decision_template(summary)

        self.assertEqual(template["decision"], "approve_parameters")
        self.assertEqual(
            {key: template["metadata"][key] for key in bindings},
            bindings,
        )

    def test_parameter_approval_inspect_exposes_exact_resume_bindings(self):
        bindings = {
            "attempt_id": 1,
            "task": "retry_after_execution_failure",
            "previous_attempt_id": 0,
            "recommendation_hash": "c" * 64,
        }
        payload = {
            "stage": "assembly",
            "gate_id": "parameter_approval",
            "reason": "parameter_approval_required",
            "decisions_allowed": ["approve_parameters", "reject_parameters"],
            "parameter_diff": {"purge_level": {"from": 1, "to": 2}},
            "rationale": "review evidence supports stronger purging",
            "failure_facts": {"review_failure": {"status": "failed"}},
            "evidence_refs": {"evidence_payload_ref": "data/evidence.json"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "session_id": "s1",
                "data_dir": str(v2_run_dir(tmp, "s1")),
                "current_stage": "assembly",
                "supervision_state": "blocked_human",
                "stage_outputs": {"assembly": {"status": "blocked_human"}},
            }
            state.update(_frozen_workflow("assembly", "assembly_review"))
            persisted = persist_review_request(
                state,
                stage="assembly",
                gate_id="parameter_approval",
                reason=payload["reason"],
                payload=payload,
                bindings=bindings,
            )
            state["active_interrupt"] = persisted.compact_interrupt(
                stage="assembly",
                gate_id="parameter_approval",
                reason=payload["reason"],
                bindings=bindings,
            )

            summary = inspect_state(state)

        self.assertEqual(
            summary["decision_requirements"]["resume_metadata"],
            bindings,
        )
        self.assertEqual(
            summary["decision_requirements"]["runtime_proposal"],
            {
                "kind": "parameter_recommendation",
                "label": "Assembly parameter proposal",
                "parameter_recommendation_status": "implemented",
                "human_confirmation_required": True,
            },
        )
        self.assertEqual(
            summary["decision_requirements"]["parameter_diff"],
            payload["parameter_diff"],
        )
        self.assertEqual(
            summary["decision_requirements"]["evidence_refs"], payload["evidence_refs"]
        )

    def test_decision_requirements_loads_verified_v2_artifact_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths.from_run_dir(v2_run_dir(tmp, "s1"))
            state = {"session_id": "s1", "data_dir": str(paths.run_dir)}
            state.update(_frozen_workflow("assembly_review", "hic_scaffolding"))
            persisted = persist_review_request(
                state,
                stage="assembly_review",
                gate_id="assembly_review",
                reason="review_required",
                payload={
                    "stage": "assembly_review",
                    "gate_id": "assembly_review",
                    "reason": "review_required",
                    "decisions_allowed": ["accept", "stop_human_review"],
                    "candidates": [
                        {"candidate_id": "flye"},
                        {"candidate_id": "canu"},
                    ],
                },
            )
            artifact_record = persisted.request_ref.model_dump(mode="json")

            self.assertIn("path_ref", artifact_record)
            requirements = _decision_requirements_from_artifact(
                artifact_record,
                paths=paths,
                expected_request_id=persisted.review_request_id,
                expected_request_hash=persisted.request_hash,
            )

            self.assertIsNotNone(requirements)
            self.assertEqual(requirements["stage"], "assembly_review")
            self.assertEqual(
                sorted(requirements["decisions_allowed"]),
                ["accept", "stop_human_review"],
            )
            self.assertEqual(sorted(requirements["candidate_ids"]), ["canu", "flye"])

    def test_inspect_session_decision_requirements_none_when_no_unresolved(self):
        state = _assembly_blocked_state()
        # Add a decision after the request → unresolved goes away.
        _record_review_decision(state, decision="accept", candidate_id="flye")

        summary = inspect_session("s1", state["data_dir"])

        self.assertIsNone(summary["decision_requirements"])

    def test_inspect_session_rejects_review_occurrence_outside_frozen_plan(self):
        state = _assembly_blocked_state()
        from app.storage.ledger import Ledger

        ledger = Ledger(state["data_dir"])
        _record_review_decision(state, decision="accept", candidate_id="flye")
        hic_state = {**state, **_frozen_workflow("hic_scaffolding", "gapcloser")}
        hic_request = persist_review_request(
            hic_state,
            stage="hic_scaffolding",
            gate_id="hic_scaffolding",
            reason="hic_scaffolding_human_decision_required",
            payload={
                "stage": "hic_scaffolding",
                "gate_id": "hic_scaffolding",
                "reason": "hic_scaffolding_human_decision_required",
                "decisions_allowed": ["accept_candidate", "stop_human_review"],
            },
        )
        ledger.record_review_requested(
            "s1",
            "hic_scaffolding",
            reason="hic_scaffolding_human_decision_required",
            review_request_artifact_ref=hic_request.request_ref.model_dump(mode="json"),
            review_request_id=hic_request.review_request_id,
        )

        summary = inspect_session("s1", state["data_dir"])

        self.assertEqual(summary["supervision_state"], "blocked_human")
        self.assertEqual(
            summary["latest_unresolved_review_request"]["stage"], "hic_scaffolding"
        )
        self.assertIsNone(summary["decision_requirements"])
        self.assertIsNone(summary["active_interrupt"])
        self.assertIsNone(summary["current_stage"])
        self.assertIsNone(summary["current_workflow_step_id"])
        self.assertIn(
            "does not match the verified frozen workflow occurrence",
            summary["review_request_verification_error"],
        )

    def test_inspect_session_keeps_unverified_occurrences_only_in_route_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = v2_run_dir(Path(tmp) / "data", "s1")
            from app.storage.ledger import Ledger

            ledger = Ledger(data_dir)
            ledger.create_session("s1", "control/manifest.yaml")
            ledger.record_stage_entered(
                "s1",
                "assembly",
                workflow_step_id="step-001-assembly",
                route_node="route_controller",
            )
            ledger.record_stage_entered(
                "s1",
                "assembly",
                workflow_step_id="step-003-assembly",
                route_node="route_controller",
            )

            summary = inspect_session("s1", data_dir)

        self.assertIsNone(summary["current_stage"])
        self.assertIsNone(summary["current_workflow_step_id"])
        self.assertEqual(
            summary["route_trace"],
            [
                "step-001-assembly:route_controller",
                "step-003-assembly:route_controller",
            ],
        )

    def test_inspect_session_projects_verified_frozen_plan_progress_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "inspect-progress"
            reads = root / "reads.fastq"
            reads.write_text("@read\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "\n".join(
                    [
                        f"session_id: {session_id}",
                        "assets:",
                        "  - asset_type: raw_long_reads",
                        f"    paths: [{reads}]",
                        "    platform: ont",
                        "    format: fastq",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            run_config = root / "run_config.json"
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [
                            {"stage": "read_qc", "allow_local_execution": False},
                            {"stage": "read_qc", "allow_local_execution": False},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            paths = SessionPaths(root / "data", session_id)
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                selected_methods={},
            )
            ledger = Ledger(paths.run_dir)
            ledger.create_session(session_id, "control/input_manifest.yaml")
            ledger.record_stage_completed(
                session_id,
                "inventory",
                workflow_step_id="step-000-inventory",
            )
            ledger.record_stage_skipped(
                session_id,
                "read_qc",
                workflow_step_id="step-001-read_qc",
                reason="operator_declined",
            )
            ledger.record_stage_completed(
                session_id,
                "assembly",
                workflow_step_id="step-002-read_qc",
            )
            ledger.record_stage_entered(
                session_id,
                "read_qc",
                workflow_step_id="step-002-read_qc",
            )
            ledger.record_stage_failed(
                session_id,
                "read_qc",
                workflow_step_id="step-002-read_qc",
                error="stage failed",
            )
            ledger.record_stage_entered(
                session_id,
                "assembly",
                workflow_step_id="step-002-read_qc",
                idempotency_key="mismatched-stage-entered:step-002-read_qc",
            )

            self.assertFalse(paths.execution_lock_path.exists())
            summary = inspect_session(session_id, paths.run_dir)

            self.assertEqual(summary["inspect_schema_version"], 1)
            self.assertEqual(
                summary["workflow_progress"],
                {
                    "plan_source": "frozen_run_config",
                    "total_steps": 3,
                    "completed_steps": 1,
                    "skipped_steps": 1,
                    "failed_steps": 1,
                    "blocked_steps": 0,
                    "unfinished_steps": 1,
                    "current_workflow_step_id": None,
                    "current_stage": None,
                },
            )
            self.assertIsNone(summary["current_workflow_step_id"])
            self.assertIsNone(summary["current_stage"])
            self.assertEqual(
                summary["execution"],
                {
                    "frozen_inputs_status": "verified",
                    "durable_progress": "partial",
                    "execution_lock_held": False,
                },
            )
            self.assertFalse(paths.execution_lock_path.exists())
            with frozen_execution_lock(paths):
                locked_summary = inspect_session(session_id, paths.run_dir)
            self.assertTrue(locked_summary["execution"]["execution_lock_held"])

    def test_inspect_session_withholds_review_authority_when_freeze_is_invalid(self):
        state = _assembly_blocked_state()
        paths = SessionPaths.from_run_dir(state["data_dir"])
        paths.run_config_path.write_text("{}\n", encoding="utf-8")

        summary = inspect_session(state["session_id"], state["data_dir"])

        self.assertEqual(summary["supervision_state"], "blocked_human")
        self.assertIsNone(summary["decision_requirements"])
        self.assertIsNone(summary["active_interrupt"])
        self.assertIsNone(summary["current_stage"])
        self.assertIsNone(summary["current_workflow_step_id"])
        self.assertEqual(summary["next_action"]["kind"], "unknown")
        self.assertIn(
            "cannot be authorized without verified frozen workflow inputs",
            summary["review_request_verification_error"],
        )

    def test_inspect_session_marks_invalid_frozen_inputs_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "inspect-invalid-frozen-inputs"
            reads = root / "reads.fastq"
            reads.write_text("@read\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                f"session_id: {session_id}\n"
                "assets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths: [{reads}]\n"
                "    platform: ont\n"
                "    format: fastq\n",
                encoding="utf-8",
            )
            run_config = root / "run_config.json"
            run_config.write_text(
                json.dumps(
                    {
                        "requested_stages": [
                            {"stage": "read_qc", "allow_local_execution": False}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            paths = SessionPaths(root / "data", session_id)
            freeze_session_inputs(
                paths,
                manifest_draft=manifest,
                run_config_draft=run_config,
                selected_methods={},
            )
            Ledger(paths.run_dir).create_session(
                session_id, "control/input_manifest.yaml"
            )
            paths.run_config_path.write_text("{}\n", encoding="utf-8")

            summary = inspect_session(session_id, paths.run_dir)

            self.assertEqual(
                summary["workflow_progress"],
                {
                    "plan_source": "unavailable",
                    "total_steps": None,
                    "completed_steps": None,
                    "skipped_steps": None,
                    "failed_steps": None,
                    "blocked_steps": None,
                    "unfinished_steps": None,
                    "current_workflow_step_id": None,
                    "current_stage": None,
                },
            )
            self.assertEqual(
                summary["execution"],
                {
                    "frozen_inputs_status": "invalid_or_missing",
                    "durable_progress": "unavailable",
                    "execution_lock_held": False,
                },
            )

    def test_inspect_session_does_not_treat_operator_report_as_lifecycle_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "operator-report-only"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            from app.storage.ledger import Ledger

            Ledger(data_dir).create_session(session_id, "control/manifest.yaml")
            interrupt = {
                "stage": "assembly",
                "gate_id": "route_selection",
                "decisions_allowed": ["select_ont", "stop_human_review"],
                "reason": "assembly_route_insufficient_data",
            }
            write_operator_report(
                data_dir,
                session_id,
                {
                    "current_stage": "assembly",
                    "supervision_state": "blocked_human",
                    "active_interrupt": interrupt,
                    "route_trace": ["inventory_stage", "read_qc_stage"],
                },
            )

            summary = inspect_session(session_id, data_dir)

            self.assertIsNotNone(summary["latest_operator_report"])
            self.assertIsNone(summary["latest_blocked_operator_report"])
            self.assertIsNone(summary["latest_operator_report_payload"])
            self.assertEqual(summary["supervision_state"], "running")
            self.assertIsNone(summary["current_stage"])
            self.assertEqual(summary["route_trace"], [])
            self.assertIsNone(summary["decision_requirements"])

    def test_inspect_session_ignores_stale_operator_report_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "stale-operator-report"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            from app.storage.ledger import Ledger

            ledger = Ledger(data_dir)
            ledger.create_session(session_id, "control/manifest.yaml")
            write_operator_report(
                data_dir,
                session_id,
                {
                    "current_stage": "assembly",
                    "supervision_state": "blocked_human",
                    "active_interrupt": {
                        "stage": "assembly",
                        "gate_id": "route_selection",
                        "reason": "stale block",
                    },
                    "route_trace": ["assembly_stage"],
                },
            )
            ledger.record_session_terminal(
                session_id,
                outcome="completed",
                stage="base_polish",
            )

            summary = inspect_session(session_id, data_dir)

            self.assertEqual(summary["supervision_state"], "completed")
            self.assertEqual(summary["next_action"]["kind"], "completed")
            self.assertIsNotNone(summary["latest_operator_report"])
            self.assertIsNone(summary["latest_blocked_operator_report"])
            self.assertIsNone(summary["latest_failed_operator_report"])
            self.assertIsNone(summary["latest_operator_report_payload"])

    def test_inspect_session_does_not_require_operator_report(self):
        state = _assembly_blocked_state()
        paths = SessionPaths.from_run_dir(state["data_dir"])
        write_operator_report(
            state["data_dir"],
            state["session_id"],
            state,
        )
        paths.report_path("current_status.json").unlink()

        summary = inspect_session(state["session_id"], state["data_dir"])

        self.assertEqual(summary["supervision_state"], "blocked_human")
        self.assertEqual(
            summary["decision_requirements"]["stage"],
            "assembly_review",
        )
        self.assertIsNone(summary["latest_operator_report"])
        self.assertIsNone(summary["latest_blocked_operator_report"])

    def test_inspect_session_reports_durable_timeout_status(self):
        state = _assembly_blocked_state()

        summary = inspect_session(
            "s1",
            state["data_dir"],
            blocked_timeout_seconds=0,
            now=datetime.now(timezone.utc),
        )

        self.assertTrue(summary["blocked_timeout"]["expired"])


class LoadResumeReviewRequestTests(unittest.TestCase):
    def test_round_trips_valid_decision_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "accept",
                        "candidate_id": "flye",
                        "notes": ["operator reviewed QUAST"],
                    }
                ),
                encoding="utf-8",
            )
            request = load_resume_review_request(path)
            self.assertEqual(request.session_id, "s1")
            self.assertEqual(request.stage, "assembly_review")
            self.assertEqual(request.decision, "accept")
            self.assertEqual(request.candidate_id, "flye")

    def test_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            load_resume_review_request("/nonexistent/decision.json")

    def test_invalid_json_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                load_resume_review_request(path)

    def test_non_object_top_level_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                load_resume_review_request(path)

    def test_extra_field_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "accept",
                        "candidate_id": "flye",
                        "unknown_field": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "validation"):
                load_resume_review_request(path)

    def test_unknown_decision_loads_for_registry_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "approve",
                    }
                ),
                encoding="utf-8",
            )
            request = load_resume_review_request(path)
            self.assertEqual(request.decision, "approve")


def _review_binding(state: dict) -> dict:
    interrupt = state["active_interrupt"]
    return {
        "gate_id": interrupt["gate_id"],
        "review_request_id": interrupt["review_request_id"],
        "review_request_hash": interrupt["request_hash"],
    }


def _artifact_path(state: dict, ref: object) -> Path:
    if isinstance(ref, dict):
        return SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(ref["path_ref"])
    return Path(str(ref))


def _candidate_output_path(state: dict, candidate_id: str) -> Path:
    candidate = next(
        item
        for item in state["stage_outputs"]["assembly_review"]["candidates"]
        if item["candidate_id"] == candidate_id
    )
    return _artifact_path(state, candidate["primary_output_ref"])


def _publish_assembly_candidate(
    data_dir: Path,
    session_id: str,
    *,
    candidate_id: str,
    method_name: str,
    source: Path,
) -> dict:
    paths = SessionPaths.from_run_dir(data_dir)
    candidate_dir = paths.workflow_step_artifact_path(
        "step-001-assembly", "assembly", "candidates", candidate_id
    )
    work_dir = paths.workflow_step_work_path(
        "step-001-assembly", "assembly", candidate_id
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    primary = publish_artifact_record(
        data_dir,
        session_id,
        ArtifactMeta.from_file(
            source,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        ),
        destination=candidate_dir / "draft.fasta",
    )
    planned = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id=candidate_id,
        method_name=method_name,
        status="planned",
        input_type="raw_long_reads",
        input_member_ids={"primary": [f"{candidate_id}:reads"]},
        input_member_platforms={"primary": ["ont"]},
        parameter_attempt_id=0,
        expected_outputs=["primary_assembly"],
    )
    plan_source = work_dir / "candidate.planned.json"
    plan_source.write_text(
        json.dumps(planned.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    published_plan = publish_artifact_record(
        data_dir,
        session_id,
        ArtifactMeta.from_file(
            plan_source,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
        destination=candidate_dir / "candidate.planned.json",
    )
    executed = planned.model_copy(
        update={
            "status": "executed",
            "plan_ref": published_plan.ref,
            "primary_output": primary.ref,
        }
    )
    executed_source = work_dir / "candidate.executed.json"
    executed_source.write_text(
        json.dumps(executed.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    published_executed = publish_artifact_record(
        data_dir,
        session_id,
        ArtifactMeta.from_file(
            executed_source,
            stage="assembly",
            method_name=method_name,
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
        destination=candidate_dir / "candidate.executed.json",
    )
    return {
        "candidate_id": candidate_id,
        "method_name": method_name,
        "source": "test_fixture",
        "status": "executed",
        "bundle_ref": published_executed.ref.model_dump(mode="json"),
        "primary_output_ref": primary.ref.model_dump(mode="json"),
    }


def _freeze_review_test_inputs(
    paths: SessionPaths,
    requested_stages: list[str],
) -> None:
    reads = paths.run_dir / "fixture_reads.fastq"
    reads.parent.mkdir(parents=True, exist_ok=True)
    reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
    freeze_session_inputs(
        paths,
        manifest_draft={
            "session_id": paths.session_id,
            "assets": [
                {
                    "asset_type": "raw_long_reads",
                    "paths": [str(reads.resolve())],
                    "platform": "ont",
                    "format": "fastq",
                }
            ],
        },
        run_config_draft={
            "requested_stages": [
                {"stage": stage, "allow_local_execution": False}
                for stage in requested_stages
            ]
        },
        selected_methods={},
        probe_environments=False,
    )


def _assembly_blocked_state(
    *,
    decisions_allowed: list[str] | None = None,
) -> dict:
    tmp = tempfile.mkdtemp()
    session_id = "s1"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    paths = SessionPaths.from_run_dir(data_dir)
    for method in ("flye", "canu"):
        draft = paths.workflow_step_work_path(
            "step-001-assembly", "assembly", method, "draft.fasta"
        )
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(">ctg1\nACGT\n", encoding="utf-8")
    candidates = [
        _publish_assembly_candidate(
            data_dir,
            session_id,
            candidate_id=method,
            method_name=f"assembly.candidate.{method}",
            source=paths.workflow_step_work_path(
                "step-001-assembly", "assembly", method, "draft.fasta"
            ),
        )
        for method in ("flye", "canu")
    ]
    payload = {
        "stage": "assembly_review",
        "gate_id": "assembly_review",
        "reason": "assembly_review_human_decision_required",
        "candidates": candidates,
        "signals": [],
        "decisions_allowed": (
            decisions_allowed
            if decisions_allowed is not None
            else [
                "pass",
                "accept",
                "purge",
                "polish",
                "continue_low_confidence",
                "stop_human_review",
            ]
        ),
        "notes": [],
        "report_refs": {
            "assembly_review_report": "reports/assembly_review.md",
            "review_packet": (
                "artifacts/steps/step-002-assembly_review/assembly_review/"
                "assembly_review_packet.json"
            ),
            "candidate_comparison": "candidate_comparison.json",
        },
        "candidate_comparison_context": {
            "candidate_ids": ["flye", "canu"],
            "candidate_comparison_ref": "candidate_comparison.json",
        },
        "auto_decidable": True,
    }
    from app.storage.ledger import Ledger

    state = {
        "session_id": session_id,
        "thread_id": session_id,
        "data_dir": str(data_dir),
        "control_state": "running",
        "supervision_state": "blocked_human",
        "current_stage": "assembly_review",
        "requested_stages": None,
        "input_manifest_ref": "control/manifest.yaml",
        "asset_registry_ref": None,
        "stage_plan_ref": None,
        "last_error": None,
        "review_notes": [],
        "route_trace": [],
        "stage_outputs": {
            "assembly_review": {
                "status": "blocked_human",
                "candidates": payload["candidates"],
                "rejected_candidates": {},
            }
        },
        "skip_reasons": {},
    }
    state.update(_frozen_workflow("assembly_review", "hic_scaffolding"))
    _freeze_review_test_inputs(
        paths,
        [step["stage"] for step in state["workflow_steps"][1:]],
    )
    persisted = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason=payload["reason"],
        payload=payload,
        method_name="assembly_review.contract",
        artifact_type="assembly_review.request",
    )
    state["active_interrupt"] = persisted.compact_interrupt(
        stage="assembly_review",
        gate_id="assembly_review",
        reason=payload["reason"],
    )
    state["stage_outputs"]["assembly_review"]["review_request_artifact_ref"] = (
        persisted.request_ref.model_dump(mode="json")
    )
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "control/manifest.yaml")
    ledger.record_review_requested(
        session_id,
        "assembly_review",
        reason=payload["reason"],
        candidates=payload["candidates"],
        review_request_artifact_ref=persisted.request_ref.model_dump(mode="json"),
        review_request_id=persisted.review_request_id,
    )
    return state


def _hic_blocked_state(*, decisions_allowed: list[str] | None = None) -> dict:
    tmp = tempfile.mkdtemp()
    session_id = "s1"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    paths = SessionPaths.from_run_dir(data_dir)
    pre_hic = paths.workflow_step_work_path(
        "step-001-assembly_review", "assembly_review", "pre_hic.fasta"
    )
    scaffold = paths.workflow_step_work_path(
        "step-002-hic_scaffolding", "hic_scaffolding", "autohic.fasta"
    )
    pre_hic.parent.mkdir(parents=True, exist_ok=True)
    scaffold.parent.mkdir(parents=True, exist_ok=True)
    pre_hic.write_text(">ctg1\nACGT\n", encoding="utf-8")
    scaffold.write_text(">chr1\nACGT\n", encoding="utf-8")
    published_pre_hic = publish_artifact_record(
        data_dir,
        session_id,
        ArtifactMeta.from_file(
            pre_hic,
            stage="assembly_review",
            method_name="assembly_review.contract",
            artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        ),
        destination=paths.workflow_step_artifact_path(
            "step-001-assembly_review",
            "assembly_review",
            "selected_assembly.fasta",
        ),
    )
    candidate_dir = paths.workflow_step_artifact_path(
        "step-001-assembly_review",
        "assembly_review",
        "selected_candidate",
    )
    candidate_work_dir = paths.workflow_step_work_path(
        "step-001-assembly_review",
        "assembly_review",
        "selected_candidate",
    )
    candidate_work_dir.mkdir(parents=True, exist_ok=True)
    planned_candidate = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id="reviewed-pre-hic",
        method_name="assembly_review.contract",
        status="planned",
        input_type="reviewed_assembly",
        input_member_ids={"primary": ["reviewed-pre-hic"]},
        input_member_platforms={"primary": ["reviewed"]},
        parameter_attempt_id=0,
        expected_outputs=["primary_assembly"],
    )
    planned_source = candidate_work_dir / "candidate.planned.json"
    planned_source.write_text(
        json.dumps(planned_candidate.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    published_plan = publish_artifact_record(
        data_dir,
        session_id,
        ArtifactMeta.from_file(
            planned_source,
            stage="assembly_review",
            method_name="assembly_review.contract",
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
        destination=candidate_dir / "candidate.planned.json",
    )
    executed_candidate = planned_candidate.model_copy(
        update={
            "status": "executed",
            "plan_ref": published_plan.ref,
            "primary_output": published_pre_hic.ref,
        }
    )
    executed_source = candidate_work_dir / "candidate.executed.json"
    executed_source.write_text(
        json.dumps(executed_candidate.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    published_candidate = publish_artifact_record(
        data_dir,
        session_id,
        ArtifactMeta.from_file(
            executed_source,
            stage="assembly_review",
            method_name="assembly_review.contract",
            artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        ),
        destination=candidate_dir / "candidate.executed.json",
    )
    published_scaffold = publish_artifact_record(
        data_dir,
        session_id,
        ArtifactMeta.from_file(
            scaffold,
            stage="hic_scaffolding",
            method_name="hic_scaffolding.candidate.autohic",
            artifact_type=HIC_SCAFFOLDED_ASSEMBLY,
        ),
        destination=paths.workflow_step_artifact_path(
            "step-002-hic_scaffolding",
            "hic_scaffolding",
            "autohic",
            "scaffold.fasta",
        ),
    )
    pre_hic = published_pre_hic.path
    scaffold = published_scaffold.path
    state = {
        "session_id": session_id,
        "thread_id": session_id,
        "data_dir": str(data_dir),
        "control_state": "running",
        "supervision_state": "blocked_human",
        "current_stage": "hic_scaffolding",
        "requested_stages": None,
        "input_manifest_ref": "control/manifest.yaml",
        "asset_registry_ref": None,
        "stage_plan_ref": None,
        "last_error": None,
        "review_notes": [],
        "route_trace": [],
        "stage_outputs": {
            "hic_scaffolding": {
                "status": "blocked_human",
                "pre_hic_assembly_ref": str(pre_hic),
                "output_assembly_ref": str(pre_hic),
                "hic_read_refs": ["hic1"],
                "method_candidates": {
                    "hic_scaffolding.candidate.autohic": {
                        "status": "executed",
                        "scaffold_fasta": str(scaffold),
                    }
                },
            }
        },
        "resource_refs": {
            "assembly": ResourceRef(
                resource_type="assembly",
                paths=[str(pre_hic)],
                source_stage="assembly_review",
                workflow_step_id="step-001-assembly_review",
                metadata={
                    "candidate_bundle_ref": published_candidate.ref.model_dump(
                        mode="json"
                    ),
                    "primary_output_ref": published_pre_hic.ref.model_dump(mode="json"),
                },
            ).to_dict()
        },
        "skip_reasons": {},
    }
    state.update(_frozen_workflow("hic_scaffolding", "gapcloser"))
    _freeze_review_test_inputs(
        paths,
        [step["stage"] for step in state["workflow_steps"][1:]],
    )
    payload = {
        "stage": "hic_scaffolding",
        "gate_id": "hic_scaffolding",
        "reason": "autohic_adoption_review_required",
        "candidates": [
            {
                "method_name": "hic_scaffolding.candidate.autohic",
                "scaffold_fasta": str(scaffold),
            }
        ],
        "method_candidates": {
            "hic_scaffolding.candidate.autohic": {
                "status": "executed",
                "scaffold_fasta": str(scaffold),
            }
        },
        "decisions_allowed": (
            decisions_allowed
            if decisions_allowed is not None
            else ["accept_candidate", "keep_prior_assembly", "stop_human_review"]
        ),
        "pre_hic_assembly_ref": str(pre_hic),
        "output_assembly_ref": str(pre_hic),
    }
    persisted = persist_review_request(
        state,
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason=payload["reason"],
        payload=payload,
        method_name="hic_scaffolding.contract",
        artifact_type="hic_scaffolding.review_request",
    )
    state["active_interrupt"] = persisted.compact_interrupt(
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason=payload["reason"],
    )
    state["stage_outputs"]["hic_scaffolding"]["review_request_artifact_ref"] = (
        persisted.request_ref.model_dump(mode="json")
    )
    from app.storage.ledger import Ledger

    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "control/manifest.yaml")
    ledger.record_review_requested(
        session_id,
        "hic_scaffolding",
        reason=payload["reason"],
        candidates=payload["candidates"],
        review_request_artifact_ref=persisted.request_ref.model_dump(mode="json"),
        review_request_id=persisted.review_request_id,
    )
    return state


def _read_events(state: dict) -> list[dict]:
    from app.storage.ledger import Ledger

    return Ledger(state["data_dir"]).read_events(state["session_id"])


def _record_review_decision(
    state: dict,
    *,
    decision: str,
    candidate_id: str | None = None,
) -> None:
    interrupt = state["active_interrupt"]
    decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=interrupt["review_request_id"],
        stage=interrupt["stage"],
        gate_id=interrupt["gate_id"],
        decision={"decision": decision, "candidate_id": candidate_id},
        request_hash=interrupt["request_hash"],
        request_ref=interrupt["request_ref"],
    )
    from app.storage.ledger import Ledger

    Ledger(state["data_dir"]).record_review_decision(
        state["session_id"],
        interrupt["stage"],
        decision=decision,
        gate_id=interrupt["gate_id"],
        candidate_id=candidate_id,
        artifact_ref=decision_ref,
        review_request_id=interrupt["review_request_id"],
    )


def _frozen_workflow_steps(stages: list[str]) -> list[dict]:
    latest_assembly_handoff = "step-000-inventory"
    steps = []
    for index, item in enumerate(stages):
        step_id = f"step-{index:03d}-{item}"
        step = {
            "step_id": step_id,
            "stage": item,
            "allow_local_execution": False,
        }
        if item == "assembly_review":
            step["review_target"] = {
                "resource_type": "assembly",
                "producer_step_id": latest_assembly_handoff,
            }
        steps.append(step)
        if item in {
            "inventory",
            "assembly",
            "assembly_dedup",
            "assembly_review",
            "hic_scaffolding",
            "t2t",
        }:
            latest_assembly_handoff = step_id
    return steps


def _frozen_workflow(stage: str, next_stage: str) -> dict:
    stages = ["inventory"]
    if stage == "assembly_review":
        stages.append("assembly")
    elif stage == "hic_scaffolding":
        stages.append("assembly_review")
    if stage != "inventory":
        stages.append(stage)
    if next_stage != "done":
        stages.append(next_stage)
    step_index = stages.index(stage)
    return {
        "current_stage": stage,
        "workflow_steps": _frozen_workflow_steps(stages),
        "workflow_step_index": step_index,
        "entered_workflow_step_index": step_index,
        "stage_plan_ref": "control/run_config.json",
        "stage_seeds": {},
    }


if __name__ == "__main__":
    unittest.main()
