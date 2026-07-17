"""Tests for the interactive review CLI (gene-agent review)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.ledger import Ledger
from app.entrypoints.review_interactive import (
    build_decision_brief,
    collect_gate_decision,
    prompt_assembly_review_decision,
    prompt_generic_gate_decision,
    prompt_hic_scaffolding_decision,
    render_blocked_summary,
    render_decision_brief,
    run_interactive_review,
    _prompt_choice,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import persist_review_request
from tests.data_v2 import v2_run_dir


def _write_fasta(tmp: str, name: str = "draft.fasta") -> str:
    path = Path(tmp) / name
    path.write_text(">ctg1\nACGT\n", encoding="utf-8")
    return str(path)


def _assembly_blocked_session(tmp: str) -> tuple[str, str]:
    """Create a minimal blocked assembly_review session. Returns (session_id, data_dir)."""
    session_id = "review-test-1"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    draft_ref = "work/assembly/flye/draft.fasta"
    draft_path = data_dir / draft_ref
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(">ctg1\nACGT\n", encoding="utf-8")
    payload = {
        "stage": "assembly_review",
        "gate_id": "assembly_review",
        "reason": "assembly_review_human_decision_required",
        "candidates": [
            {
                "candidate_id": "flye",
                "method_name": "assembly.candidate.flye",
                "final_assembly_ref": draft_ref,
            },
        ],
        "signals": [],
        "decisions_allowed": ["accept", "rerun", "stop_human_review"],
        "notes": [],
        "report_refs": {},
        "candidate_comparison_context": {},
    }
    state = {"session_id": session_id, "data_dir": str(data_dir)}
    persisted = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason=payload["reason"],
        payload=payload,
        method_name="assembly_review.contract",
        artifact_type="assembly_review.request",
    )
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "control/manifest.yaml")
    ledger.record_review_requested(
        session_id, "assembly_review",
        reason=payload["reason"],
        candidates=payload["candidates"],
        review_request_artifact_ref=persisted.request_ref.model_dump(mode="json"),
        review_request_id=persisted.review_request_id,
    )
    return session_id, str(data_dir)


def _hic_blocked_session(tmp: str) -> tuple[str, str]:
    """Create a minimal blocked hic_scaffolding session. Returns (session_id, data_dir)."""
    session_id = "review-test-hic"
    data_dir = v2_run_dir(Path(tmp) / "data", session_id)
    payload = {
        "stage": "hic_scaffolding",
        "gate_id": "hic_scaffolding",
        "reason": "autohic_adoption_review_required",
        "candidates": [{"method_name": "hic_scaffolding.candidate.autohic"}],
        "decisions_allowed": [
            "accept_candidate",
            "keep_prior_assembly",
            "stop_human_review",
        ],
        "notes": ["Hi-C reads available"],
    }
    state = {"session_id": session_id, "data_dir": str(data_dir)}
    persisted = persist_review_request(
        state,
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason=payload["reason"],
        payload=payload,
        method_name="hic_scaffolding.contract",
        artifact_type="hic_scaffolding.review_request",
    )
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "control/manifest.yaml")
    ledger.record_review_requested(
        session_id, "hic_scaffolding",
        reason=payload["reason"],
        candidates=payload["candidates"],
        review_request_artifact_ref=persisted.request_ref.model_dump(mode="json"),
        review_request_id=persisted.review_request_id,
    )
    return session_id, str(data_dir)


class PromptAssemblyReviewTests(unittest.TestCase):
    def test_parameter_approval_prompt_copies_exact_resume_bindings(self):
        bindings = {
            "attempt_id": 1,
            "task": "retry_after_execution_failure",
            "previous_attempt_id": 0,
            "recommendation_hash": "b" * 64,
        }
        requirements = {
            "stage": "assembly",
            "gate_id": "parameter_approval",
            "decisions_allowed": ["approve_parameters", "reject_parameters"],
            "resume_metadata": bindings,
        }
        inputs = iter(["1", "approve exact recommendation"])

        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            decision = prompt_generic_gate_decision(requirements)

        self.assertIsNotNone(decision)
        self.assertEqual(decision["decision"], "approve_parameters")
        self.assertEqual(decision["metadata"], bindings)

    def test_prompt_choice_retries_invalid_input(self):
        inputs = iter(["bad", "2"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            choice = _prompt_choice("decision", ["accept", "rerun"])
        self.assertEqual(choice, "rerun")

    def test_accept_single_candidate_auto_selects(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = _write_fasta(tmp)
            requirements = {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "pass"],
                "candidate_ids": ["flye"],
                "candidate_final_assembly_refs": {"flye": draft},
            }
            inputs = iter(["1", ""])
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate_id"], "flye")
        self.assertEqual(result["final_assembly_ref"], draft)

    def test_decision_prompt_describes_hic_t2t_downstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft = _write_fasta(tmp)
            requirements = {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "rerun", "stop_human_review"],
                "candidate_ids": ["flye"],
                "candidate_final_assembly_refs": {"flye": draft},
            }
            inputs = iter(["1", ""])
            out = io.StringIO()
            with patch("builtins.input", side_effect=lambda _: next(inputs)), \
                    patch("sys.stdout", out):
                result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        rendered = out.getvalue()
        self.assertIn("continue to Hi-C scaffolding, then T2T", rendered)
        self.assertIn("next: assembly_review -> hic_scaffolding -> t2t", rendered)

    def test_accept_multiple_candidates_prompts_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            flye = _write_fasta(tmp, "flye.fasta")
            canu = _write_fasta(tmp, "canu.fasta")
            requirements = {
                "stage": "assembly_review",
                "decisions_allowed": ["accept"],
                "candidate_ids": ["flye", "canu"],
                "candidate_final_assembly_refs": {
                    "flye": flye,
                    "canu": canu,
                },
            }
            inputs = iter(["1", "2", ""])
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate_id"], "canu")
        self.assertEqual(result["rejected_candidates"], ["flye"])

    def test_accept_filters_missing_candidates_and_shows_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            flye = _write_fasta(tmp, "flye.fasta")
            report = str(Path(tmp) / "assembly_review.md")
            metric = str(Path(tmp) / "quast_summary.json")
            requirements = {
                "stage": "assembly_review",
                "decisions_allowed": ["accept"],
                "candidate_ids": ["flye", "nextdenovo"],
                "candidate_final_assembly_refs": {
                    "flye": flye,
                    "nextdenovo": str(Path(tmp) / "missing.fasta"),
                },
                "candidate_report_refs": {"flye": [metric]},
                "report_refs": {"assembly_review_report": report},
            }
            inputs = iter(["1", ""])
            out = io.StringIO()
            with patch("builtins.input", side_effect=lambda _: next(inputs)), \
                    patch("sys.stdout", out):
                result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["candidate_id"], "flye")
        rendered = out.getvalue()
        self.assertIn("unavailable_candidates", rendered)
        self.assertIn("nextdenovo", rendered)
        self.assertIn("candidate_evidence", rendered)
        self.assertIn(metric, rendered)
        self.assertIn(report, rendered)

    def test_cancel_on_eof_returns_none(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/draft.fasta"},
        }
        with patch("builtins.input", side_effect=EOFError):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNone(result)

    def test_stop_human_review_skips_candidate_selection(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept", "stop_human_review"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/draft.fasta"},
        }
        inputs = iter(["2", "stopping review"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "stop_human_review")
        self.assertIsNone(result["candidate_id"])

    def test_purge_collects_transform_metadata(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["purge", "accept"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/draft.fasta"},
        }
        inputs = iter([
            "1",  # select purge
            "/path/purged.fasta",  # action_output_ref
            "purge_dups",  # method_name
            "purge_dups log shows 5% removed",  # evidence_notes
            "purged assembly",  # notes
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "purge")
        self.assertEqual(result["candidate_id"], "flye")
        self.assertEqual(result["final_assembly_ref"], "/path/purged.fasta")
        self.assertEqual(result["metadata"]["action_output_ref"], "/path/purged.fasta")
        self.assertTrue(result["metadata"]["human_approved_action"])
        self.assertEqual(result["metadata"]["method_name"], "purge_dups")
        self.assertIn("purge_dups log shows 5% removed", result["metadata"]["evidence_notes"])

    def test_rerun_collects_parameters(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["rerun", "accept"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/draft.fasta"},
        }
        inputs = iter([
            "1",  # select rerun
            "500m",  # estimated_genome_size
            "nano-raw",  # read_type
            "8",  # threads
            "retry with adjusted params",  # notes
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "rerun")
        self.assertIn("rerun_parameters", result["metadata"])
        self.assertEqual(result["metadata"]["rerun_parameters"]["estimated_genome_size"], "500m")
        self.assertEqual(result["metadata"]["rerun_parameters"]["read_type"], "nano-raw")
        self.assertEqual(result["metadata"]["rerun_parameters"]["threads"], 8)

    def test_purge_missing_evidence_returns_none(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["purge"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/draft.fasta"},
        }
        inputs = iter([
            "1",  # select purge
            "/path/purged.fasta",  # action_output_ref
            "purge_dups",  # method_name
            "",  # empty evidence_notes → error
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNone(result)

    def test_purge_missing_method_name_returns_none(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["purge"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/draft.fasta"},
        }
        inputs = iter([
            "1",  # select purge
            "/path/purged.fasta",  # action_output_ref
            "",  # empty method_name → error
        ])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNone(result)


class PromptHicScaffoldingTests(unittest.TestCase):
    def test_accept_candidate_prompt_uses_operator_labels(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": [
                "accept_candidate",
                "keep_prior_assembly",
                "stop_human_review",
            ],
            "executed_candidate_methods": ["hic_scaffolding.candidate.autohic"],
        }
        inputs = iter(["1", ""])
        out = io.StringIO()

        def input_with_prompt(prompt):
            out.write(prompt)
            return next(inputs)

        with patch("builtins.input", side_effect=input_with_prompt), \
                patch("sys.stdout", out):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept_candidate")
        rendered = out.getvalue()
        self.assertIn("action:", rendered)
        self.assertIn("select action [1-3]", rendered)
        self.assertIn("accept scaffold result [accept_candidate]", rendered)
        self.assertIn("operator note (optional, audit only)", rendered)

    def test_keep_prior_assembly_needs_no_method_selection(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["accept_candidate", "keep_prior_assembly"],
        }
        inputs = iter(["2", ""])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "keep_prior_assembly")
        self.assertNotIn("method_name", result)

    def test_keep_prior_assembly_decision(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["accept_candidate", "keep_prior_assembly"],
        }
        inputs = iter(["2", "no Hi-C data available"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "keep_prior_assembly")
        self.assertNotIn("method_name", result)
        self.assertEqual(result["notes"], ["no Hi-C data available"])

    def test_cancel_returns_none(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["accept_candidate", "keep_prior_assembly"],
        }
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNone(result)


class RunInteractiveReviewTests(unittest.TestCase):
    def test_session_not_found_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "nonexistent"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            code = run_interactive_review(session_id, str(data_dir))
            self.assertEqual(code, 1)

    def test_not_blocked_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "completed-session"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            ledger = Ledger(data_dir)
            ledger.create_session(session_id, "control/manifest.yaml")
            code = run_interactive_review(session_id, str(data_dir))
            self.assertEqual(code, 1)

    def test_wrong_supervision_state_does_not_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-wrong-state"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "supervision_state": "completed",
                "current_stage": "assembly_review",
                "stage_outputs": {"assembly_review": {"status": "completed"}},
                "route_trace": [],
            }
            graph = type(
                "FakeGraph",
                (),
                {
                    "get_state": lambda self, config: type("Snapshot", (), {"values": state})(),
                    "update_state": lambda self, config, update: (_ for _ in ()).throw(AssertionError("should not update")),
                    "invoke": lambda self, state_arg, config: (_ for _ in ()).throw(AssertionError("should not invoke")),
                },
            )()
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
                final_assembly_ref="draft.fasta",
            )
            raw_decision = request.model_dump(mode="json")
            with patch("app.entrypoints.review_interactive.collect_gate_decision", return_value=(request, raw_decision)), \
                 patch("app.entrypoints.run.build_graph", return_value=graph):
                code = run_interactive_review(session_id, str(data_dir))
            self.assertEqual(code, 1)

    def test_cancelled_prompt_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _assembly_blocked_session(tmp)
            with patch("builtins.input", side_effect=EOFError):
                code = run_interactive_review(session_id, data_dir)
            self.assertEqual(code, 1)

    def test_annotation_intake_cancel_does_not_resume_or_write_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-cancel-intake"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "supervision_state": "blocked_human",
                "current_stage": "assembly_review",
                "requested_stages": ["read_qc", "assembly", "assembly_review"],
                "active_interrupt": {"stage": "assembly_review", "gate_id": "assembly_review"},
                "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
                "route_trace": [],
            }
            graph = type(
                "FakeGraph",
                (),
                {
                    "get_state": lambda self, config: type("Snapshot", (), {"values": state})(),
                    "update_state": lambda self, config, update: None,
                    "invoke": lambda self, state_arg, config: {"supervision_state": "completed"},
                },
            )()
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                gate_id="assembly_review",
                decision="accept",
                candidate_id="flye",
                final_assembly_ref="draft.fasta",
            )
            raw_decision = request.model_dump(mode="json")

            with patch("app.entrypoints.review_interactive.collect_gate_decision", return_value=(request, raw_decision)), \
                    patch("app.entrypoints.run.build_graph", return_value=graph), \
                    patch("app.review.annotation_intake.collect_annotation_intake_selection", side_effect=RuntimeError("annotation intake cancelled")) as intake_prompt, \
                    patch("app.control.lifecycle.resume_review_state") as resume_mock:
                code = run_interactive_review(session_id, str(data_dir))

            self.assertEqual(code, 1)
            intake_prompt.assert_called_once()
            resume_mock.assert_not_called()
            decision_dir = data_dir / "artifacts" / "reviews"
            self.assertFalse(list(decision_dir.glob("*/decision.json")) if decision_dir.exists() else [])
            events = Ledger(data_dir).read_events(session_id)
            self.assertFalse(any(event["event_type"] == "review_decision" for event in events))

    def test_update_state_failure_does_not_invoke_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-update-failure"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            Ledger(data_dir).create_session(session_id, "control/manifest.yaml")
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "supervision_state": "blocked_human",
                "current_stage": "assembly_review",
                "active_interrupt": {"stage": "assembly_review", "gate_id": "assembly_review"},
                "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
                "route_trace": [],
            }
            graph = type(
                "FakeGraph",
                (),
                {
                    "get_state": lambda self, config: type("Snapshot", (), {"values": state})(),
                    "update_state": lambda self, config, update: (_ for _ in ()).throw(RuntimeError("update boom")),
                    "invoke": lambda self, state_arg, config: (_ for _ in ()).throw(AssertionError("should not invoke")),
                },
            )()
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
                final_assembly_ref="draft.fasta",
            )
            raw_decision = request.model_dump(mode="json")
            decision_path = data_dir / "artifacts" / "reviews" / "review_test" / "decision.json"
            decision_path.parent.mkdir(parents=True, exist_ok=True)
            decision_path.write_text("{}\n", encoding="utf-8")
            with patch("app.entrypoints.review_interactive.collect_gate_decision", return_value=(request, raw_decision)), \
                 patch("app.entrypoints.run.build_graph", return_value=graph), \
                 patch("app.entrypoints.review_interactive.applied_decision_artifact_path", return_value=decision_path), \
                 patch("app.review.annotation_intake.collect_annotation_intake_selection", return_value=None), \
                 patch("app.control.lifecycle.resume_review_state", return_value={"supervision_state": "running_stage"}):
                code = run_interactive_review(session_id, str(data_dir))

            self.assertEqual(code, 1)
            events = Ledger(data_dir).read_events(session_id)
            self.assertTrue(any(event["event_type"] == "resume_failed" for event in events))

    def test_annotation_intake_value_error_is_reported_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "review-intake-value-error"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            Ledger(data_dir).create_session(session_id, "control/manifest.yaml")
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "supervision_state": "blocked_human",
                "current_stage": "assembly_review",
                "active_interrupt": {"stage": "assembly_review", "gate_id": "assembly_review"},
                "stage_outputs": {"assembly_review": {"status": "blocked_human"}},
                "route_trace": [],
            }
            graph = type(
                "FakeGraph",
                (),
                {
                    "get_state": lambda self, config: type("Snapshot", (), {"values": state})(),
                    "update_state": lambda self, config, update: (_ for _ in ()).throw(AssertionError("should not update")),
                    "invoke": lambda self, state_arg, config: (_ for _ in ()).throw(AssertionError("should not invoke")),
                },
            )()
            request = ResumeReviewRequest(
                session_id=session_id,
                stage="assembly_review",
                decision="accept",
                candidate_id="flye",
                final_assembly_ref="draft.fasta",
            )
            raw_decision = request.model_dump(mode="json")
            intake_selection = type(
                "IntakeSelection",
                (),
                {"recommended_plan": object(), "final_plan": object()},
            )()
            with patch("app.entrypoints.review_interactive.collect_gate_decision", return_value=(request, raw_decision)), \
                 patch("app.entrypoints.run.build_graph", return_value=graph), \
                 patch("app.review.annotation_intake.collect_annotation_intake_selection", return_value=intake_selection), \
                 patch("app.review.annotation_intake.persist_annotation_intake_plan", side_effect=ValueError("missing provenance")), \
                 patch("app.control.lifecycle.resume_review_state", return_value={"supervision_state": "running_stage"}):
                code = run_interactive_review(session_id, str(data_dir))

            self.assertEqual(code, 1)
            events = Ledger(data_dir).read_events(session_id)
            rejected = [event for event in events if event["event_type"] == "resume_rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertIn("ValueError: missing provenance", rejected[0]["error"])

    def test_dry_run_writes_decision_without_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _assembly_blocked_session(tmp)
            inputs = iter(["1", ""])
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                code = run_interactive_review(session_id, data_dir, dry_run=True)
            self.assertEqual(code, 0)
            decision_dir = Path(data_dir) / "work" / "reviews"
            decisions = list(decision_dir.glob("*/decision.preview.json"))
            self.assertEqual(len(decisions), 1)
            decision = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision["decision"], "accept")
            self.assertEqual(decision["candidate_id"], "flye")
            self.assertEqual(decision["decided_by"], "human")

    def test_hic_dry_run_keep_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _hic_blocked_session(tmp)
            inputs = iter(["2", "skipping scaffolding"])
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                code = run_interactive_review(session_id, data_dir, dry_run=True)
            self.assertEqual(code, 0)
            decision_dir = Path(data_dir) / "work" / "reviews"
            decisions = list(decision_dir.glob("*/decision.preview.json"))
            self.assertEqual(len(decisions), 1)
            decision = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision["decision"], "keep_prior_assembly")
            self.assertEqual(decision["stage"], "hic_scaffolding")

    def test_collect_gate_decision_uses_checkpoint_blocked_state_before_ledger_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "inventory-blocked"
            data_dir = v2_run_dir(Path(tmp) / "data", session_id)
            Ledger(data_dir).create_session(session_id, "control/manifest.yaml")
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            db_path.write_text("", encoding="utf-8")
            payload = {
                "stage": "inventory",
                "gate_id": "inventory_review",
                "reason": "review_required",
                "messages": ["raw_long_reads: platform unknown"],
                "decisions_allowed": ["continue", "stop_human_review"],
            }
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "supervision_state": "blocked_human",
                "current_stage": "inventory",
                "stage_outputs": {
                    "inventory": {
                        "status": "validated",
                        "valid": True,
                        "warnings": ["raw_long_reads: platform unknown"],
                    }
                },
                "route_trace": [],
            }
            persisted = persist_review_request(
                state,
                stage="inventory",
                gate_id="inventory_review",
                reason=payload["reason"],
                payload=payload,
            )
            state["active_interrupt"] = persisted.compact_interrupt(
                stage="inventory",
                gate_id="inventory_review",
                reason=payload["reason"],
            )
            graph = type(
                "FakeGraph",
                (),
                {"get_state": lambda self, config: type("Snapshot", (), {"values": state})()},
            )()
            inputs = iter(["1", "continue inventory"])

            with patch("app.graph.builder.build_graph", return_value=graph), \
                    patch("builtins.input", side_effect=lambda _: next(inputs)):
                result = collect_gate_decision(session_id, str(data_dir))

            self.assertIsNotNone(result)
            request, raw = result
            self.assertEqual(request.stage, "inventory")
            self.assertEqual(request.gate_id, "inventory_review")
            self.assertEqual(request.decision, "continue")
            self.assertEqual(raw["notes"], ["continue inventory"])


class RenderBlockedSummaryTests(unittest.TestCase):
    def test_renders_assembly_review_without_error(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "rerun", "stop_human_review"],
                "candidate_ids": ["flye"],
                "candidate_final_assembly_refs": {"flye": "/tmp/nonexistent.fasta"},
                "report_refs": {"report": "/tmp/report.json"},
                "candidate_comparison_context": {"flye_n50": 10000},
            },
            "latest_review_request_artifact": {"file_path": "/tmp/request.json"},
            "next_action": {"relevant_refs": ["/tmp/ref"]},
        }
        render_blocked_summary(summary)

    def test_renders_assembly_review_downstream_flow(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "rerun", "stop_human_review"],
                "candidate_ids": ["flye"],
                "candidate_final_assembly_refs": {"flye": "/tmp/nonexistent.fasta"},
            },
            "latest_review_request_artifact": None,
            "next_action": {},
        }
        out = io.StringIO()
        with patch("sys.stdout", out):
            render_blocked_summary(summary)

        rendered = out.getvalue()
        self.assertIn("after_accepting_assembly", rendered)
        self.assertIn("assembly_review -> hic_scaffolding -> t2t", rendered)
        self.assertIn("continues to hic_scaffolding, then t2t", rendered)

    def test_renders_hic_scaffolding_without_error(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "hic_scaffolding",
                "decisions_allowed": ["select_method", "skip"],
                "candidate_methods": ["hic_scaffolding.candidate.autohic"],
            },
            "latest_review_request_artifact": None,
            "next_action": {},
        }
        render_blocked_summary(summary)


class ValidationTests(unittest.TestCase):
    def test_unknown_decision_value_is_schema_valid(self):
        raw = {
            "session_id": "s1",
            "stage": "assembly_review",
            "decision": "invalid_decision",
            "candidate_id": "flye",
            "decided_by": "human",
        }
        request = ResumeReviewRequest.model_validate(raw)
        self.assertEqual(request.decision, "invalid_decision")

    def test_unsupported_stage_fails(self):
        raw = {
            "session_id": "s1",
            "stage": "genome_survey",
            "decision": "accept",
            "decided_by": "human",
        }
        request = ResumeReviewRequest.model_validate(raw)
        self.assertEqual(request.stage, "genome_survey")


class DecisionBriefTests(unittest.TestCase):
    def test_parameter_approval_brief_renders_diff_failure_and_top5_paper_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            basis_path = root / "basis_snapshot.json"
            basis_path.write_text(
                json.dumps(
                    {
                        "literature_cases": [
                            {
                                "case_id": f"paper-{index}",
                                "source": {"doi": f"10.1000/{index}"},
                                "source_excerpts": [
                                    {"span_id": f"methods-{index}", "quote": f"Exact methods quote {index}."}
                                ],
                            }
                            for index in range(6)
                        ]
                    }
                ),
                encoding="utf-8",
            )
            recommendation_path = root / "validated_recommendation.json"
            recommendation_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "method_id": "hifiasm",
                        "task": "retry_after_execution_failure",
                        "recommendation": {"params": {"purge_level": 2}},
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = root / "recommendation_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "refs": {
                            "basis_snapshot_ref": {"path_ref": str(basis_path)},
                        },
                    }
                ),
                encoding="utf-8",
            )
            summary = {
                "session_id": "s1",
                "supervision_state": "blocked_human",
                "decision_requirements": {
                    "stage": "assembly",
                    "gate_id": "parameter_approval",
                    "decisions_allowed": ["approve_parameters", "reject_parameters"],
                    "resume_metadata": {
                        "attempt_id": 1,
                        "task": "retry_after_execution_failure",
                        "previous_attempt_id": 0,
                        "recommendation_hash": "d" * 64,
                    },
                    "recommendation_manifest_ref": {"path_ref": str(manifest_path)},
                    "recommendation_ref": {"path_ref": str(recommendation_path)},
                    "parameter_diff": {"purge_level": {"from": 1, "to": 2}},
                    "rationale": "retry after OOM",
                    "failure_facts": {"signals": {"return_code": 137}},
                    "evidence_refs": {"basis_snapshot_ref": str(basis_path)},
                },
                "latest_review_request_artifact": None,
                "next_action": {},
            }

            brief = build_decision_brief(summary, tmp)
            out = io.StringIO()
            with patch("sys.stdout", out):
                render_decision_brief(brief)

        proposal = brief["parameter_proposal"]
        self.assertEqual(proposal["parameter_diff"]["purge_level"]["to"], 2)
        self.assertEqual(proposal["failure_facts"]["signals"]["return_code"], 137)
        self.assertEqual(len(proposal["literature_cases"]), 5)
        rendered = out.getvalue()
        self.assertIn("production paper evidence (5, top-5 maximum)", rendered)
        self.assertIn("doi=10.1000/0", rendered)
        self.assertIn("Exact methods quote 0.", rendered)

    def test_assembly_review_brief_structure(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "rerun", "stop_human_review"],
                "candidate_ids": ["flye"],
                "candidate_final_assembly_refs": {"flye": "/tmp/draft.fasta"},
                "report_refs": {"quast": "/tmp/quast_report.json"},
                "candidate_comparison_context": {"flye_n50": 50000, "flye_total_length": 500000000},
            },
            "latest_review_request_artifact": {"file_path": "/tmp/request.json"},
            "next_action": {"relevant_refs": ["/tmp/ref1"]},
        }
        brief = build_decision_brief(summary, "/tmp/data")

        self.assertEqual(brief["session_id"], "s1")
        self.assertEqual(brief["stage"], "assembly_review")
        self.assertEqual(brief["supervision_state"], "blocked_human")
        self.assertEqual(brief["decisions_allowed"], ["accept", "rerun", "stop_human_review"])
        self.assertEqual(brief["review_request_ref"], "/tmp/request.json")
        self.assertEqual(len(brief["candidates"]), 1)
        self.assertEqual(brief["candidates"][0]["id"], "flye")
        self.assertEqual(brief["candidates"][0]["ref"], "/tmp/draft.fasta")
        self.assertIn("exists", brief["candidates"][0])
        self.assertEqual(brief["evidence"]["metrics"]["flye_n50"], 50000)
        self.assertEqual(brief["evidence"]["report_refs"]["quast"], "/tmp/quast_report.json")
        self.assertEqual(brief["relevant_refs"], ["/tmp/ref1"])

    def test_assembly_brief_with_request_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "request.json"
            artifact_path.write_text(json.dumps({
                "stage": "assembly_review",
                "candidates": [
                    {"candidate_id": "flye", "method_name": "assembly.candidate.flye", "status": "completed"},
                ],
                "notes": ["N50 above threshold"],
                "signals": ["busco_complete_above_90"],
            }), encoding="utf-8")

            summary = {
                "session_id": "s2",
                "supervision_state": "blocked_human",
                "decision_requirements": {
                    "stage": "assembly_review",
                    "decisions_allowed": ["accept"],
                    "candidate_ids": ["flye"],
                    "candidate_final_assembly_refs": {"flye": "/tmp/draft.fasta"},
                    "report_refs": {},
                    "candidate_comparison_context": {},
                },
                "latest_review_request_artifact": {"file_path": str(artifact_path)},
                "next_action": {},
            }
            brief = build_decision_brief(summary, tmp)

            self.assertEqual(brief["candidates"][0]["method"], "assembly.candidate.flye")
            self.assertEqual(brief["candidates"][0]["status"], "completed")
            self.assertIn("N50 above threshold", brief["evidence"]["notes"])
            self.assertIn("busco_complete_above_90", brief["evidence"]["notes"])

    def test_hic_scaffolding_brief_structure(self):
        summary = {
            "session_id": "s3",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "hic_scaffolding",
                "decisions_allowed": ["select_method", "skip"],
                "candidate_methods": ["hic_scaffolding.candidate.autohic"],
            },
            "latest_review_request_artifact": None,
            "latest_review_request": {
                "payload": {"pre_hic_assembly_ref": "/tmp/pre_hic.fasta"},
            },
            "next_action": {},
        }
        brief = build_decision_brief(summary, "/tmp/data")

        self.assertEqual(brief["stage"], "hic_scaffolding")
        self.assertEqual(len(brief["candidates"]), 1)
        self.assertEqual(brief["candidates"][0]["method"], "hic_scaffolding.candidate.autohic")
        self.assertEqual(brief["evidence"]["report_refs"]["pre_hic_assembly_ref"], "/tmp/pre_hic.fasta")

    def test_hic_brief_with_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "hic_request.json"
            artifact_path.write_text(json.dumps({
                "stage": "hic_scaffolding",
                "candidates": [{"method_name": "hic_scaffolding.candidate.autohic"}],
                "notes": ["Hi-C reads available", "Contact map review required"],
            }), encoding="utf-8")

            summary = {
                "session_id": "s4",
                "supervision_state": "blocked_human",
                "decision_requirements": {
                    "stage": "hic_scaffolding",
                    "decisions_allowed": ["select_method", "skip"],
                    "candidate_methods": ["hic_scaffolding.candidate.autohic"],
                },
                "latest_review_request_artifact": {"file_path": str(artifact_path)},
                "latest_review_request": {"payload": {}},
                "next_action": {},
            }
            brief = build_decision_brief(summary, tmp)

            self.assertIn("Hi-C reads available", brief["evidence"]["notes"])
            self.assertIn("Contact map review required", brief["evidence"]["notes"])

    def test_render_decision_brief_no_crash(self):
        brief = {
            "session_id": "s1",
            "stage": "assembly_review",
            "supervision_state": "blocked_human",
            "decisions_allowed": ["accept", "pass"],
            "review_request_ref": "/tmp/request.json",
            "runtime_proposal": {
                "kind": "assembly_candidate_review",
                "label": "Assembly result and evidence review",
                "parameter_recommendation_status": "mixed",
                "related_parameter_gate": {"stage": "assembly", "gate_id": "parameter_approval"},
                "parameter_recommendation_notes": ["Operator-directed rerun is a supported manual path outside parameter recommendation."],
                "human_confirmation_required": True,
            },
            "candidates": [
                {"id": "flye", "ref": "/tmp/draft.fasta", "method": "assembly.candidate.flye", "exists": False},
            ],
            "evidence": {
                "metrics": {"flye_n50": 50000},
                "report_refs": {"quast": "/tmp/quast.json"},
                "notes": ["N50 above threshold"],
            },
            "relevant_refs": ["/tmp/ref1"],
        }
        render_decision_brief(brief)

    def test_t2t_runtime_proposal_does_not_claim_parameter_recommender(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "t2t",
                "gate_id": "t2t_review",
                "decisions_allowed": ["continue_without_patch"],
                "runtime_proposal": {
                    "kind": "finishing_candidate_review",
                    "label": "T2T finishing candidate review",
                    "parameter_recommendation_status": "not_implemented",
                    "human_confirmation_required": True,
                },
            },
            "latest_review_request_artifact": None,
            "next_action": {},
        }

        brief = build_decision_brief(summary, "/tmp")
        out = io.StringIO()
        with patch("sys.stdout", out):
            render_decision_brief(brief)

        self.assertEqual(brief["runtime_proposal"]["parameter_recommendation_status"], "not_implemented")
        self.assertIn("parameter recommendation: not_implemented", out.getvalue())

    def test_empty_brief_renders_without_error(self):
        brief = {
            "session_id": "s1",
            "stage": "unknown",
            "decisions_allowed": [],
        }
        render_decision_brief(brief)


if __name__ == "__main__":
    unittest.main()
