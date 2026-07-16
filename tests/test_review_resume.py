"""Tests for review inspect and in-process resume helpers."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.control.lifecycle import (
    ResumeValidationError,
    _decision_requirements_from_artifact,
    inspect_session,
    build_resume_decision_template,
    inspect_state,
    resume_review_state,
)
from app.review.base import ResumeReviewRequest, load_resume_review_request
from app.review.persistence import persist_review_request
from app.reports.review_report import write_operator_report
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
                final_assembly_ref="work/assembly/flye/draft.fasta",
                decided_by="llm_provisional",
            )

        self.assertIn("rationale_artifact_ref", str(ctx.exception))

    def test_llm_provisional_resume_request_is_valid_with_rationale(self):
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye/draft.fasta",
            decided_by="llm_provisional",
            rationale_artifact_ref="artifacts/assembly_review/recommendation.json",
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
        self.assertIn("review_request_artifact_ref", summary["latest_artifact_refs"]["assembly_review"])

    def test_resume_assembly_review_accept_advances_to_hic(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye/draft.fasta",
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
        self.assertEqual(ar_out["pre_action_assembly_ref"], "work/assembly/flye/draft.fasta")
        self.assertEqual(ar_out["final_assembly_for_scaffolding_or_annotation"], "work/assembly/flye/draft.fasta")
        self.assertTrue(_artifact_path(state, ar_out["review_decision_artifact_ref"]).exists())
        self.assertTrue(_artifact_path(state, ar_out["assembly_review_action_artifact_ref"]).exists())
        events = _read_events(state)
        self.assertEqual(events[-1]["event_type"], "review_decision")
        self.assertEqual(events[-1]["candidate_id"], "flye")
        decision_record = json.loads(
            _artifact_path(state, ar_out["review_decision_artifact_ref"]).read_text(encoding="utf-8")
        )
        self.assertEqual(decision_record["decision"]["metadata"]["action_type"], "pass")

    def test_resume_assembly_review_pass_advances_to_hic(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="pass",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye/draft.fasta",
            notes=["human passed flye through unchanged"],
            **_review_binding(state),
        )

        update = resume_review_state(state, request)

        self.assertEqual(update["current_stage"], "hic_scaffolding")
        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertEqual(ar_out["decision"], "pass")
        self.assertEqual(ar_out["action_type"], "pass")
        self.assertEqual(ar_out["action_execution_mode"], "none")
        self.assertEqual(ar_out["final_assembly_for_scaffolding_or_annotation"], "work/assembly/flye/draft.fasta")

    def test_resume_assembly_purge_requires_materialized_output(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="purge",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye.purged.fasta",
            method_name="external_purge_dups",
            metadata={
                "human_approved_action": True,
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
            final_assembly_ref=str(purged),
            method_name="external_purge_dups",
            notes=["human note alone is not enough provenance"],
            metadata={"human_approved_action": True},
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
            final_assembly_ref=str(purged),
            metadata={
                "human_approved_action": True,
                "evidence_refs": ["assembly_eval_bundle.json"],
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "method or provenance"):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_mismatched_action_output_ref(self):
        state = _assembly_blocked_state()
        purged = Path(state["data_dir"]) / "flye.purged.fasta"
        other = Path(state["data_dir"]) / "other.purged.fasta"
        purged.write_text(">ctg1\nACGT\n", encoding="utf-8")
        other.write_text(">ctg1\nACGT\n", encoding="utf-8")
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="purge",
            candidate_id="flye",
            final_assembly_ref=str(purged),
            method_name="external_purge_dups",
            metadata={
                "human_approved_action": True,
                "action_output_ref": str(other),
                "evidence_refs": ["assembly_eval_bundle.json"],
            },
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "must match"):
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
            final_assembly_ref=str(purged),
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
        self.assertEqual(ar_out["pre_action_assembly_ref"], "work/assembly/flye/draft.fasta")
        self.assertEqual(ar_out["purged_assembly"], str(purged))
        self.assertEqual(ar_out["final_assembly_for_scaffolding_or_annotation"], str(purged))
        action = json.loads(_artifact_path(state, ar_out["assembly_review_action_artifact_ref"]).read_text(encoding="utf-8"))
        self.assertEqual(action["action_type"], "purge")
        self.assertEqual(action["pre_action_assembly_ref"], "work/assembly/flye/draft.fasta")
        decision_record = json.loads(
            _artifact_path(state, ar_out["review_decision_artifact_ref"]).read_text(encoding="utf-8")
        )
        self.assertEqual(decision_record["decision"]["metadata"]["action_type"], "purge")
        self.assertEqual(
            decision_record["decision"]["metadata"]["post_action_assembly_ref"],
            purged.relative_to(Path(state["data_dir"])).as_posix(),
        )

    def test_resume_assembly_polish_requires_action_output_ref(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="polish",
            candidate_id="flye",
            metadata={"human_approved_action": True, "evidence_notes": ["reviewed polishing report"]},
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
            final_assembly_ref=str(polished),
            method_name="pilon_shortread",
            notes=["human approved polish"],
            metadata={"human_approved_action": True, "evidence_notes": ["polish requested"]},
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "short-read"):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_missing_final_ref_without_mutation(self):
        state = _assembly_blocked_state()
        before = copy.deepcopy(state)
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "final_assembly_ref"):
            resume_review_state(state, request)

        self.assertEqual(state, before)

    def test_resume_assembly_rejects_mismatched_final_ref(self):
        state = _assembly_blocked_state()
        before = copy.deepcopy(state)
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            final_assembly_ref="work/assembly/canu/draft.fasta",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "does not match"):
            resume_review_state(state, request)

        self.assertEqual(state, before)

    def test_resume_assembly_honors_interrupt_decisions_allowed(self):
        state = _assembly_blocked_state(decisions_allowed=["stop_human_review"])
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye/draft.fasta",
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
            final_assembly_ref="work/assembly/flye/draft.fasta",
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
            final_assembly_ref="draft.fasta",
            **_review_binding(state),
        )

        with self.assertRaisesRegex(ResumeValidationError, "unknown"):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_rerun_without_parameters(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="rerun",
            **_review_binding(state),
        )

        with self.assertRaises(ResumeValidationError):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_rerun_unknown_candidate(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="rerun",
            candidate_id="unknown",
            metadata={"rerun_parameters": {"read_type": "nano-raw", "estimated_genome_size": 100, "threads": 1}},
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
        self.assertTrue(_artifact_path(state, update["stage_outputs"]["assembly_review"]["review_decision_artifact_ref"]).exists())

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

    def test_assembly_review_runtime_proposal_points_rerun_to_parameter_gate(self):
        summary = inspect_state(_assembly_blocked_state())

        proposal = summary["decision_requirements"]["runtime_proposal"]
        self.assertEqual(proposal["kind"], "assembly_candidate_review")
        self.assertEqual(proposal["parameter_recommendation_status"], "mixed")
        self.assertEqual(
            proposal["related_parameter_gate"],
            {"stage": "assembly", "gate_id": "parameter_approval"},
        )
        self.assertTrue(any("legacy manual Flye" in note for note in proposal["parameter_recommendation_notes"]))
        template = build_resume_decision_template(summary)
        self.assertEqual(template["metadata"]["template_guidance"]["runtime_proposal"], proposal)

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
        self.assertEqual(update["current_stage"], "t2t")
        hic_out = update["stage_outputs"]["hic_scaffolding"]
        self.assertEqual(hic_out["selected_method_name"], "hic_scaffolding.candidate.autohic")
        self.assertTrue(hic_out["final_assembly_for_annotation"].endswith("autohic.fasta"))
        self.assertTrue(_artifact_path(state, hic_out["review_decision_artifact_ref"]).exists())

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

        self.assertEqual(update["current_stage"], "t2t")
        hic_out = update["stage_outputs"]["hic_scaffolding"]
        self.assertTrue(
            hic_out["final_assembly_for_annotation"].endswith(
                "work/hic_scaffolding/pre_hic.fasta"
            )
        )
        self.assertTrue(_artifact_path(state, hic_out["review_decision_artifact_ref"]).exists())
        artifact_index = Path(state["data_dir"]) / "artifacts" / "index.jsonl"
        artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("hic_scaffolding.skip", artifact_types)
        self.assertIn("hic_scaffolding.adoption_review.decision", artifact_types)

    def test_blocked_timeout_is_checked_synchronously(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["blocked_at"] = "2026-05-08T10:00:00+00:00"
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="stop_human_review",
            **_review_binding(state),
        )

        update = resume_review_state(
            state,
            request,
            blocked_timeout_seconds=1,
            now=datetime(2026, 5, 8, 10, 0, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(update["supervision_state"], "failed_terminal")
        self.assertIn("timeout", update["last_error"])
        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertTrue(_artifact_path(state, ar_out["review_decision_artifact_ref"]).exists())
        self.assertEqual(ar_out["decision"], "stop_human_review")

    def test_resume_request_rejects_extra_fields(self):
        with self.assertRaises(ValidationError):
            ResumeReviewRequest(
                session_id="s1",
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
                final_assembly_ref="draft.fasta",
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
        self.assertEqual(summary["latest_review_request"]["event_type"], "review_requested")
        self.assertEqual(summary["latest_unresolved_review_request"]["event_type"], "review_requested")
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
            sorted(["pass", "accept", "purge", "polish", "rerun", "continue_low_confidence", "stop_human_review"]),
        )
        self.assertEqual(sorted(requirements["candidate_ids"]), ["canu", "flye"])
        self.assertTrue(requirements["requires_structured_resume"])
        self.assertEqual(requirements["report_refs"]["assembly_review_report"], "reports/assembly_review.md")
        self.assertEqual(requirements["candidate_comparison_context"]["candidate_comparison_ref"], "candidate_comparison.json")

    def test_resume_decision_template_includes_report_guidance(self):
        state = _assembly_blocked_state()

        summary = inspect_session("s1", state["data_dir"])
        template = build_resume_decision_template(summary)

        self.assertEqual(template["gate_id"], "assembly_review")
        guidance = template["metadata"]["template_guidance"]
        self.assertEqual(guidance["report_refs"]["assembly_review_report"], "reports/assembly_review.md")
        self.assertEqual(guidance["candidate_comparison_context"]["candidate_comparison_ref"], "candidate_comparison.json")
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
        self.assertEqual(summary["decision_requirements"]["evidence_refs"], payload["evidence_refs"])

    def test_decision_requirements_loads_verified_v2_artifact_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths.from_run_dir(v2_run_dir(tmp, "s1"))
            state = {"session_id": "s1", "data_dir": str(paths.run_dir)}
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
        from app.control.ledger import Ledger

        Ledger(state["data_dir"]).record_review_decision(
            "s1", "assembly_review", decision="accept", candidate_id="flye"
        )

        summary = inspect_session("s1", state["data_dir"])

        self.assertIsNone(summary["decision_requirements"])

    def test_inspect_session_uses_latest_review_event_order(self):
        state = _assembly_blocked_state()
        from app.control.ledger import Ledger

        ledger = Ledger(state["data_dir"])
        ledger.record_review_decision("s1", "assembly_review", decision="accept", candidate_id="flye")
        ledger.record_review_requested("s1", "hic_scaffolding", reason="hic_scaffolding_human_decision_required")

        summary = inspect_session("s1", state["data_dir"])

        self.assertEqual(summary["supervision_state"], "blocked_human")
        self.assertEqual(summary["latest_unresolved_review_request"]["stage"], "hic_scaffolding")

    def test_inspect_session_does_not_treat_operator_report_as_lifecycle_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "operator-report-only"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            from app.control.ledger import Ledger

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

            self.assertIsNotNone(summary["latest_blocked_operator_report"])
            self.assertEqual(summary["supervision_state"], "running")
            self.assertIsNone(summary["current_stage"])
            self.assertEqual(summary["route_trace"], [])
            self.assertIsNone(summary["decision_requirements"])

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
                        "final_assembly_ref": "draft.fasta",
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
                        "final_assembly_ref": "draft.fasta",
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
                json.dumps({"session_id": "s1", "stage": "assembly_review", "decision": "approve"}),
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


def _assembly_blocked_state(
    *,
    decisions_allowed: list[str] | None = None,
) -> dict:
    tmp = tempfile.mkdtemp()
    session_id = "s1"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    for method in ("flye", "canu"):
        draft = data_dir / "work" / "assembly" / method / "draft.fasta"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(">ctg1\nACGT\n", encoding="utf-8")
    payload = {
        "stage": "assembly_review",
        "gate_id": "assembly_review",
        "reason": "assembly_review_human_decision_required",
        "candidates": [
            {"candidate_id": "flye", "method_name": "assembly.candidate.flye", "final_assembly_ref": "work/assembly/flye/draft.fasta"},
            {"candidate_id": "canu", "method_name": "assembly.candidate.canu", "final_assembly_ref": "work/assembly/canu/draft.fasta"},
        ],
        "signals": [],
        "decisions_allowed": (
            decisions_allowed
            if decisions_allowed is not None
            else ["pass", "accept", "purge", "polish", "rerun", "continue_low_confidence", "stop_human_review"]
        ),
        "notes": [],
        "report_refs": {
            "assembly_review_report": "reports/assembly_review.md",
            "review_packet": "artifacts/assembly_review/assembly_review_packet.json",
            "candidate_comparison": "candidate_comparison.json",
        },
        "candidate_comparison_context": {
            "candidate_ids": ["flye", "canu"],
            "candidate_final_assembly_refs": {
                "flye": "work/assembly/flye/draft.fasta",
                "canu": "work/assembly/canu/draft.fasta",
            },
            "candidate_comparison_ref": "candidate_comparison.json",
        },
    }
    from app.control.ledger import Ledger
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
    pre_hic = data_dir / "work" / "hic_scaffolding" / "pre_hic.fasta"
    scaffold = data_dir / "work" / "hic_scaffolding" / "autohic.fasta"
    pre_hic.parent.mkdir(parents=True, exist_ok=True)
    pre_hic.write_text(">ctg1\nACGT\n", encoding="utf-8")
    scaffold.write_text(">chr1\nACGT\n", encoding="utf-8")
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
                "final_assembly_for_annotation": str(pre_hic),
                "hic_read_refs": ["hic1"],
                "method_candidates": {
                    "hic_scaffolding.candidate.autohic": {
                        "status": "executed",
                        "scaffold_fasta": str(scaffold),
                    }
                },
            }
        },
        "skip_reasons": {},
    }
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
        "final_assembly_for_annotation": str(pre_hic),
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
    from app.control.ledger import Ledger

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
    from app.control.ledger import Ledger

    return Ledger(state["data_dir"]).read_events(state["session_id"])


if __name__ == "__main__":
    unittest.main()
