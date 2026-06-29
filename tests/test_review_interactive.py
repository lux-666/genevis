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
    prompt_hic_scaffolding_decision,
    render_blocked_summary,
    render_decision_brief,
    run_interactive_review,
)
from app.execution.artifacts import append_artifact_records
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ArtifactMeta


def _assembly_blocked_session(tmp: str) -> tuple[str, str]:
    """Create a minimal blocked assembly_review session. Returns (session_id, data_dir)."""
    data_dir = Path(tmp) / "data"
    session_id = "review-test-1"
    review_dir = data_dir / "artifacts" / "assembly_review" / session_id
    review_dir.mkdir(parents=True, exist_ok=True)
    request_ref = review_dir / "assembly_review_request.json"
    payload = {
        "stage": "assembly_review",
        "reason": "assembly_review_human_decision_required",
        "candidates": [
            {"candidate_id": "flye", "method_name": "assembly.candidate.flye", "final_assembly_ref": str(review_dir / "draft.fasta")},
        ],
        "signals": [],
        "decisions_allowed": ["accept", "pass", "purge", "polish", "continue_low_confidence", "stop_human_review", "rerun"],
        "notes": [],
        "report_refs": {},
        "candidate_comparison_context": {},
    }
    request_ref.write_text(json.dumps(payload), encoding="utf-8")
    (review_dir / "draft.fasta").write_text(">ctg1\nACGT\n", encoding="utf-8")

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
    ledger.record_review_requested(
        session_id, "assembly_review",
        reason=payload["reason"],
        candidates=payload["candidates"],
    )
    return session_id, str(data_dir)


def _hic_blocked_session(tmp: str) -> tuple[str, str]:
    """Create a minimal blocked hic_scaffolding session. Returns (session_id, data_dir)."""
    data_dir = Path(tmp) / "data"
    session_id = "review-test-hic"
    hic_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id
    hic_dir.mkdir(parents=True, exist_ok=True)
    request_ref = hic_dir / "hic_scaffolding_review_request.json"
    payload = {
        "stage": "hic_scaffolding",
        "reason": "hic_scaffolding_human_decision_required",
        "candidates": [{"method_name": "hic_scaffolding.candidate.yahs"}],
        "decisions_allowed": ["select_method", "skip", "stop_human_review"],
        "notes": ["Hi-C reads available"],
    }
    request_ref.write_text(json.dumps(payload), encoding="utf-8")

    append_artifact_records(
        data_dir,
        session_id,
        [
            ArtifactMeta.from_file(
                request_ref,
                stage="hic_scaffolding",
                method_name="hic_scaffolding.contract",
                artifact_type="hic_scaffolding.request",
            )
        ],
    )
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, "manifest.yaml")
    ledger.record_review_requested(
        session_id, "hic_scaffolding",
        reason=payload["reason"],
        candidates=payload["candidates"],
    )
    return session_id, str(data_dir)


class PromptAssemblyReviewTests(unittest.TestCase):
    def test_accept_single_candidate_auto_selects(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept", "pass"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/to/draft.fasta"},
        }
        inputs = iter(["1", ""])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate_id"], "flye")
        self.assertEqual(result["final_assembly_ref"], "/path/to/draft.fasta")

    def test_decision_prompt_describes_hic_t2t_downstream(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept", "pass", "rerun"],
            "candidate_ids": ["flye"],
            "candidate_final_assembly_refs": {"flye": "/path/to/draft.fasta"},
        }
        inputs = iter(["1", ""])
        out = io.StringIO()
        with patch("builtins.input", side_effect=lambda _: next(inputs)), \
                patch("sys.stdout", out):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        rendered = out.getvalue()
        self.assertIn("continue to Hi-C scaffolding, then T2T", rendered)
        self.assertIn("downstream_flow: assembly_review -> hic_scaffolding -> t2t", rendered)

    def test_accept_multiple_candidates_prompts_choice(self):
        requirements = {
            "stage": "assembly_review",
            "decisions_allowed": ["accept"],
            "candidate_ids": ["flye", "canu"],
            "candidate_final_assembly_refs": {
                "flye": "/path/flye.fasta",
                "canu": "/path/canu.fasta",
            },
        }
        inputs = iter(["1", "2", ""])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_assembly_review_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(result["candidate_id"], "canu")
        self.assertEqual(result["rejected_candidates"], ["flye"])

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
    def test_select_method_single_auto_selects(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["select_method", "skip"],
            "candidate_methods": ["hic_scaffolding.candidate.yahs"],
        }
        inputs = iter(["1", ""])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "select_method")
        self.assertEqual(result["method_name"], "hic_scaffolding.candidate.yahs")

    def test_skip_decision(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["select_method", "skip"],
            "candidate_methods": ["hic_scaffolding.candidate.yahs"],
        }
        inputs = iter(["2", "no Hi-C data available"])
        with patch("builtins.input", side_effect=lambda _: next(inputs)):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "skip")
        self.assertIsNone(result["method_name"])
        self.assertEqual(result["notes"], ["no Hi-C data available"])

    def test_cancel_returns_none(self):
        requirements = {
            "stage": "hic_scaffolding",
            "decisions_allowed": ["select_method", "skip"],
            "candidate_methods": [],
        }
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = prompt_hic_scaffolding_decision(requirements)

        self.assertIsNone(result)


class RunInteractiveReviewTests(unittest.TestCase):
    def test_session_not_found_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            code = run_interactive_review("nonexistent", str(data_dir))
            self.assertEqual(code, 1)

    def test_not_blocked_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            session_id = "completed-session"
            ledger = Ledger(data_dir)
            ledger.create_session(session_id, "manifest.yaml")
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
            data_dir = Path(tmp) / "data"
            session_id = "review-cancel-intake"
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True)
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
            decision_dir = data_dir / "artifacts" / "assembly_review" / session_id
            self.assertFalse(list(decision_dir.glob("assembly_review_interactive_decision_*.json")) if decision_dir.exists() else [])
            events = Ledger(data_dir).read_events(session_id)
            self.assertFalse(any(event["event_type"] == "review_decision" for event in events))

    def test_dry_run_writes_decision_without_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _assembly_blocked_session(tmp)
            inputs = iter(["1", ""])
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                code = run_interactive_review(session_id, data_dir, dry_run=True)
            self.assertEqual(code, 0)
            decision_dir = (
                Path(data_dir) / "artifacts" / "assembly_review" / session_id
            )
            decisions = list(decision_dir.glob("assembly_review_interactive_decision_*.json"))
            self.assertEqual(len(decisions), 1)
            decision = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision["decision"], "accept")
            self.assertEqual(decision["candidate_id"], "flye")
            self.assertEqual(decision["decided_by"], "human")

    def test_hic_dry_run_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_id, data_dir = _hic_blocked_session(tmp)
            inputs = iter(["2", "skipping scaffolding"])
            with patch("builtins.input", side_effect=lambda _: next(inputs)):
                code = run_interactive_review(session_id, data_dir, dry_run=True)
            self.assertEqual(code, 0)
            decision_dir = (
                Path(data_dir) / "artifacts" / "hic_scaffolding" / session_id
            )
            decisions = list(decision_dir.glob("hic_scaffolding_interactive_decision_*.json"))
            self.assertEqual(len(decisions), 1)
            decision = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision["decision"], "skip")
            self.assertEqual(decision["stage"], "hic_scaffolding")

    def test_collect_gate_decision_uses_checkpoint_blocked_state_before_ledger_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            session_id = "inventory-blocked"
            Ledger(data_dir).create_session(session_id, "manifest.yaml")
            db_path = data_dir / "checkpoints" / "checkpoints.sqlite"
            db_path.parent.mkdir(parents=True)
            db_path.write_text("", encoding="utf-8")
            state = {
                "session_id": session_id,
                "data_dir": str(data_dir),
                "supervision_state": "blocked_human",
                "current_stage": "inventory",
                "active_interrupt": {
                    "stage": "inventory",
                    "gate_id": "inventory_review",
                    "reason": "review_required",
                    "messages": ["raw_long_reads: platform unknown"],
                    "decisions_allowed": ["continue", "stop_human_review"],
                },
                "stage_outputs": {
                    "inventory": {
                        "status": "validated",
                        "valid": True,
                        "warnings": ["raw_long_reads: platform unknown"],
                    }
                },
                "route_trace": [],
            }
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
                "decisions_allowed": ["accept", "pass"],
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
                "decisions_allowed": ["accept", "pass", "rerun"],
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
        self.assertIn("next_after_assembly_review", rendered)
        self.assertIn("assembly_review -> hic_scaffolding -> t2t", rendered)
        self.assertIn("does not jump straight to annotation", rendered)

    def test_renders_hic_scaffolding_without_error(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "hic_scaffolding",
                "decisions_allowed": ["select_method", "skip"],
                "candidate_methods": ["hic_scaffolding.candidate.yahs"],
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
    def test_assembly_review_brief_structure(self):
        summary = {
            "session_id": "s1",
            "supervision_state": "blocked_human",
            "decision_requirements": {
                "stage": "assembly_review",
                "decisions_allowed": ["accept", "pass", "rerun"],
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
        self.assertEqual(brief["decisions_allowed"], ["accept", "pass", "rerun"])
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
                "candidate_methods": ["hic_scaffolding.candidate.yahs"],
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
        self.assertEqual(brief["candidates"][0]["method"], "hic_scaffolding.candidate.yahs")
        self.assertEqual(brief["evidence"]["report_refs"]["pre_hic_assembly_ref"], "/tmp/pre_hic.fasta")

    def test_hic_brief_with_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "hic_request.json"
            artifact_path.write_text(json.dumps({
                "stage": "hic_scaffolding",
                "candidates": [{"method_name": "hic_scaffolding.candidate.yahs"}],
                "notes": ["Hi-C reads available", "Contact map review required"],
            }), encoding="utf-8")

            summary = {
                "session_id": "s4",
                "supervision_state": "blocked_human",
                "decision_requirements": {
                    "stage": "hic_scaffolding",
                    "decisions_allowed": ["select_method", "skip"],
                    "candidate_methods": ["hic_scaffolding.candidate.yahs"],
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

    def test_empty_brief_renders_without_error(self):
        brief = {
            "session_id": "s1",
            "stage": "unknown",
            "decisions_allowed": [],
        }
        render_decision_brief(brief)


if __name__ == "__main__":
    unittest.main()
