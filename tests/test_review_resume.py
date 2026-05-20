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
    inspect_state,
    resume_review_state,
)
from app.review.base import ResumeReviewRequest, load_resume_review_request
from app.schemas.artifacts import ArtifactMeta


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
        self.assertTrue(Path(ar_out["review_decision_artifact_ref"]).exists())
        self.assertTrue(Path(ar_out["assembly_review_action_artifact_ref"]).exists())
        events = _read_events(state)
        self.assertEqual(events[-1]["event_type"], "review_decision")
        self.assertEqual(events[-1]["candidate_id"], "flye")
        self.assertEqual(events[-1]["metadata"]["action_type"], "pass")

    def test_resume_assembly_review_pass_advances_to_hic(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = [*state["active_interrupt"]["decisions_allowed"], "pass"]
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="pass",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye/draft.fasta",
            notes=["human passed flye through unchanged"],
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
        )

        update = resume_review_state(state, request)

        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertEqual(update["current_stage"], "hic_scaffolding")
        self.assertEqual(ar_out["action_type"], "purge")
        self.assertEqual(ar_out["action_execution_mode"], "external")
        self.assertEqual(ar_out["pre_action_assembly_ref"], "work/assembly/flye/draft.fasta")
        self.assertEqual(ar_out["purged_assembly"], str(purged))
        self.assertEqual(ar_out["final_assembly_for_scaffolding_or_annotation"], str(purged))
        action = json.loads(Path(ar_out["assembly_review_action_artifact_ref"]).read_text(encoding="utf-8"))
        self.assertEqual(action["action_type"], "purge")
        self.assertEqual(action["pre_action_assembly_ref"], "work/assembly/flye/draft.fasta")
        events = _read_events(state)
        self.assertEqual(events[-1]["metadata"]["action_type"], "purge")
        self.assertEqual(events[-1]["metadata"]["post_action_assembly_ref"], str(purged))

    def test_resume_assembly_polish_requires_action_output_ref(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = [*state["active_interrupt"]["decisions_allowed"], "polish"]
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="polish",
            candidate_id="flye",
            metadata={"human_approved_action": True, "evidence_notes": ["reviewed polishing report"]},
        )

        with self.assertRaisesRegex(ResumeValidationError, "action_output_ref"):
            resume_review_state(state, request)

    def test_resume_assembly_short_read_polish_requires_short_read_evidence(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = [*state["active_interrupt"]["decisions_allowed"], "polish"]
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
        )

        with self.assertRaisesRegex(ResumeValidationError, "short-read"):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_missing_final_ref_without_mutation(self):
        state = _assembly_blocked_state()
        before = copy.deepcopy(state)
        request = ResumeReviewRequest(session_id="s1", stage="assembly_review", decision="accept", candidate_id="flye")

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
        )

        with self.assertRaisesRegex(ResumeValidationError, "does not match"):
            resume_review_state(state, request)

        self.assertEqual(state, before)

    def test_resume_assembly_honors_interrupt_decisions_allowed(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = ["stop_human_review"]
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye/draft.fasta",
        )

        with self.assertRaisesRegex(ResumeValidationError, "not allowed"):
            resume_review_state(state, request)

    def test_empty_decisions_allowed_rejects_all_resume_decisions(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = []
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="accept",
            candidate_id="flye",
            final_assembly_ref="work/assembly/flye/draft.fasta",
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
        )

        with self.assertRaisesRegex(ResumeValidationError, "unknown"):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_rerun_without_parameters(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = [*state["active_interrupt"]["decisions_allowed"], "rerun"]
        request = ResumeReviewRequest(session_id="s1", stage="assembly_review", decision="rerun")

        with self.assertRaises(ResumeValidationError):
            resume_review_state(state, request)

    def test_resume_assembly_rejects_rerun_unknown_candidate(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = [*state["active_interrupt"]["decisions_allowed"], "rerun"]
        request = ResumeReviewRequest(
            session_id="s1",
            stage="assembly_review",
            decision="rerun",
            candidate_id="unknown",
            metadata={"rerun_parameters": {"read_type": "nano-raw", "estimated_genome_size": 100, "threads": 1}},
        )

        with self.assertRaisesRegex(ResumeValidationError, "unknown"):
            resume_review_state(state, request)

    def test_resume_assembly_stop_records_failed_terminal(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(session_id="s1", stage="assembly_review", decision="stop_human_review")

        update = resume_review_state(state, request)

        self.assertIsNone(update["active_interrupt"])
        self.assertEqual(update["supervision_state"], "failed_terminal")
        self.assertIn("stopped", update["last_error"])
        self.assertTrue(Path(update["stage_outputs"]["assembly_review"]["review_decision_artifact_ref"]).exists())

    def test_resume_rejects_stage_mismatch(self):
        state = _assembly_blocked_state()
        request = ResumeReviewRequest(session_id="s1", stage="hic_scaffolding", decision="skip")

        with self.assertRaisesRegex(ResumeValidationError, "active_interrupt"):
            resume_review_state(state, request)

    def test_inspect_blocked_hic_state_includes_plan(self):
        state = _hic_blocked_state()

        summary = inspect_state(state)

        self.assertEqual(summary["decision_requirements"]["stage"], "hic_scaffolding")
        self.assertIn("hic_scaffolding.candidate.yahs", summary["decision_requirements"]["candidate_methods"])
        self.assertEqual(summary["latest_artifact_refs"]["hic_scaffolding"]["scaffold_plan_ref"], "plan.json")

    def test_inspect_hic_uses_interrupt_decisions_allowed(self):
        state = _hic_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = ["skip"]

        summary = inspect_state(state)

        self.assertEqual(summary["decision_requirements"]["decisions_allowed"], ["skip"])

    def test_resume_hic_select_method_records_decision_and_fallback(self):
        state = _hic_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="select_method",
            method_name="hic_scaffolding.candidate.yahs",
            notes=["contact map must be manually reviewed before promotion"],
        )

        update = resume_review_state(state, request)

        self.assertIsNone(update["active_interrupt"])
        self.assertEqual(update["current_stage"], "repeat_annotation")
        hic_out = update["stage_outputs"]["hic_scaffolding"]
        self.assertEqual(hic_out["selected_method_name"], "hic_scaffolding.candidate.yahs")
        self.assertEqual(hic_out["final_assembly_for_annotation"], "pre_hic.fasta")
        self.assertTrue(Path(hic_out["review_decision_artifact_ref"]).exists())

    def test_resume_hic_honors_interrupt_decisions_allowed(self):
        state = _hic_blocked_state()
        state["active_interrupt"]["decisions_allowed"] = ["skip"]
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="select_method",
            method_name="hic_scaffolding.candidate.yahs",
            notes=["contact map policy approved"],
        )

        with self.assertRaisesRegex(ResumeValidationError, "not allowed"):
            resume_review_state(state, request)

    def test_resume_hic_rejects_missing_contact_policy(self):
        state = _hic_blocked_state()
        request = ResumeReviewRequest(
            session_id="s1",
            stage="hic_scaffolding",
            decision="select_method",
            method_name="hic_scaffolding.candidate.yahs",
        )

        with self.assertRaisesRegex(ResumeValidationError, "contact-map"):
            resume_review_state(state, request)

    def test_resume_hic_skip_advances_with_fallback(self):
        state = _hic_blocked_state()
        request = ResumeReviewRequest(session_id="s1", stage="hic_scaffolding", decision="skip", notes=["skip Hi-C"])

        update = resume_review_state(state, request)

        self.assertEqual(update["current_stage"], "repeat_annotation")
        hic_out = update["stage_outputs"]["hic_scaffolding"]
        self.assertEqual(hic_out["final_assembly_for_annotation"], "pre_hic.fasta")
        self.assertTrue(Path(hic_out["skip_record_ref"]).exists())
        self.assertTrue(Path(hic_out["review_decision_artifact_ref"]).exists())
        artifact_index = Path(state["data_dir"]) / "artifacts" / state["session_id"] / "artifacts.jsonl"
        artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
        self.assertIn("hic_scaffolding.skip", artifact_types)
        self.assertIn("hic_scaffolding.decision", artifact_types)

    def test_blocked_timeout_is_checked_synchronously(self):
        state = _assembly_blocked_state()
        state["active_interrupt"]["blocked_at"] = "2026-05-08T10:00:00+00:00"
        request = ResumeReviewRequest(session_id="s1", stage="assembly_review", decision="stop_human_review")

        update = resume_review_state(
            state,
            request,
            blocked_timeout_seconds=1,
            now=datetime(2026, 5, 8, 10, 0, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(update["supervision_state"], "failed_terminal")
        self.assertIn("timeout", update["last_error"])
        ar_out = update["stage_outputs"]["assembly_review"]
        self.assertTrue(Path(ar_out["review_decision_artifact_ref"]).exists())
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

    def test_resume_request_rejects_impossible_decision(self):
        with self.assertRaises(ValidationError):
            ResumeReviewRequest(session_id="s1", stage="assembly_review", decision="approve")

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
            sorted(["pass", "accept", "purge", "polish", "continue_low_confidence", "stop_human_review"]),
        )
        self.assertEqual(sorted(requirements["candidate_ids"]), ["canu", "flye"])
        self.assertTrue(requirements["requires_structured_resume"])

    def test_decision_requirements_uses_artifact_meta_file_path_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "assembly_review_request.json"
            path.write_text(
                json.dumps(
                    {
                        "stage": "assembly_review",
                        "decisions_allowed": ["accept", "stop_human_review"],
                        "candidates": [
                            {"candidate_id": "flye"},
                            {"candidate_id": "canu"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            artifact_record = ArtifactMeta.from_file(
                path,
                stage="assembly_review",
                method_name="assembly_review.contract",
                artifact_type="assembly_review.request",
            ).model_dump(mode="json")

            self.assertIn("file_path", artifact_record)
            requirements = _decision_requirements_from_artifact(artifact_record)

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

    def test_unknown_decision_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.json"
            path.write_text(
                json.dumps({"session_id": "s1", "stage": "assembly_review", "decision": "approve"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "validation"):
                load_resume_review_request(path)


def _assembly_blocked_state() -> dict:
    tmp = tempfile.mkdtemp()
    data_dir = Path(tmp) / "data"
    session_id = "s1"
    review_dir = data_dir / "artifacts" / "assembly_review" / session_id
    review_dir.mkdir(parents=True, exist_ok=True)
    request_ref = review_dir / "assembly_review_request.json"
    payload = {
        "stage": "assembly_review",
        "reason": "assembly_review_human_decision_required",
        "candidates": [
            {"candidate_id": "flye", "method_name": "assembly.candidate.flye", "final_assembly_ref": "work/assembly/flye/draft.fasta"},
            {"candidate_id": "canu", "method_name": "assembly.candidate.canu", "final_assembly_ref": "work/assembly/canu/draft.fasta"},
        ],
        "signals": [],
        "decisions_allowed": ["pass", "accept", "purge", "polish", "continue_low_confidence", "stop_human_review"],
        "notes": [],
        "blocked_at": datetime.now(timezone.utc).isoformat(),
        "review_request_artifact_ref": str(request_ref),
    }
    request_ref.write_text(json.dumps(payload), encoding="utf-8")
    from app.control.ledger import Ledger
    from app.execution.artifacts import append_artifact_records
    from app.schemas.artifacts import ArtifactMeta

    append_artifact_records(
        data_dir,
        session_id,
        [
            ArtifactMeta.from_file(
                request_ref,
                stage="assembly_review",
                method_name="assembly_review.contract",
                artifact_type="assembly_review.request",
            )
        ],
    )

    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "manifest.yaml")
    ledger.record_review_requested(session_id, "assembly_review", reason=payload["reason"], candidates=payload["candidates"])
    return {
        "session_id": session_id,
        "thread_id": session_id,
        "data_dir": str(data_dir),
        "control_state": "running",
        "supervision_state": "blocked_human",
        "current_stage": "assembly_review",
        "requested_stages": None,
        "input_manifest_ref": "manifest.yaml",
        "asset_registry_ref": None,
        "stage_plan_ref": None,
        "active_interrupt": payload,
        "last_error": None,
        "review_notes": [],
        "route_trace": [],
        "stage_outputs": {
            "assembly_review": {
                "status": "blocked_human",
                "candidates": payload["candidates"],
                "rejected_candidates": {},
                "review_request_artifact_ref": str(request_ref),
            }
        },
        "skip_reasons": {},
    }


def _hic_blocked_state() -> dict:
    tmp = tempfile.mkdtemp()
    data_dir = Path(tmp) / "data"
    session_id = "s1"
    data_dir.mkdir(parents=True, exist_ok=True)
    return {
        "session_id": session_id,
        "thread_id": session_id,
        "data_dir": str(data_dir),
        "control_state": "running",
        "supervision_state": "blocked_human",
        "current_stage": "hic_scaffolding",
        "requested_stages": None,
        "input_manifest_ref": "manifest.yaml",
        "asset_registry_ref": None,
        "stage_plan_ref": None,
        "active_interrupt": {
            "stage": "hic_scaffolding",
            "reason": "hic_scaffolding_human_decision_required",
            "candidate_methods": ["hic_scaffolding.candidate.yahs"],
            "scaffold_plan_ref": "plan.json",
            "pre_hic_assembly_ref": "pre_hic.fasta",
            "final_assembly_for_annotation": "pre_hic.fasta",
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        },
        "last_error": None,
        "review_notes": [],
        "route_trace": [],
        "stage_outputs": {
            "hic_scaffolding": {
                "status": "blocked_human",
                "candidate_methods": ["hic_scaffolding.candidate.yahs"],
                "scaffold_plan_ref": "plan.json",
                "pre_hic_assembly_ref": "pre_hic.fasta",
                "final_assembly_for_annotation": "pre_hic.fasta",
                "hic_read_refs": ["hic1"],
            }
        },
        "skip_reasons": {},
    }


def _read_events(state: dict) -> list[dict]:
    events_path = Path(state["data_dir"]) / "ledger" / state["session_id"] / "events.jsonl"
    return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
