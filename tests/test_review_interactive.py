"""Tests for the interactive review CLI (gene-agent review)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.ledger import Ledger
from app.entrypoints.review_interactive import (
    prompt_assembly_review_decision,
    prompt_hic_scaffolding_decision,
    render_blocked_summary,
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
    def test_invalid_decision_value_fails(self):
        raw = {
            "session_id": "s1",
            "stage": "assembly_review",
            "decision": "invalid_decision",
            "candidate_id": "flye",
            "decided_by": "human",
        }
        with self.assertRaises(Exception):
            ResumeReviewRequest.model_validate(raw)

    def test_unsupported_stage_fails(self):
        raw = {
            "session_id": "s1",
            "stage": "genome_survey",
            "decision": "accept",
            "decided_by": "human",
        }
        with self.assertRaises(Exception):
            ResumeReviewRequest.model_validate(raw)


if __name__ == "__main__":
    unittest.main()
