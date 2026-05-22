"""Tests for ask interactive mode and auto-resolve."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.entrypoints.ask_interactive import (
    is_interactive,
    parse_set_params,
    run_interactive_loop,
    _apply_set_params,
    _parse_genome_size,
)
from app.entrypoints.ask_planner import (
    auto_resolve_plan,
    next_clarification,
    save_plan_to_default_path,
)
from app.schemas.ask_plan import AskPlanState


def _base_plan(**overrides) -> AskPlanState:
    base = dict(
        session_id="s1",
        data_dir="data",
        entry_request={"command": "run", "manifest": "test.yaml", "data_dir": "data"},
        manifest_ref="test.yaml",
    )
    base.update(overrides)
    return AskPlanState(**base)


def _ask_ns(**overrides):
    base = dict(
        json=False,
        non_interactive=False,
        resume_session=None,
        set_params=[],
        checkpointer=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class IsInteractiveTests(unittest.TestCase):
    def test_true_when_tty(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            self.assertTrue(is_interactive(_ask_ns()))

    def test_false_when_json_flag(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            self.assertFalse(is_interactive(_ask_ns(json=True)))

    def test_false_when_non_interactive_flag(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            self.assertFalse(is_interactive(_ask_ns(non_interactive=True)))

    def test_false_when_not_tty(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            self.assertFalse(is_interactive(_ask_ns()))


class AutoResolvePlanTests(unittest.TestCase):
    def test_raw_long_reads_only_auto_selects_assembly_flye(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest))
            resolved = auto_resolve_plan(plan)
            self.assertEqual(resolved.selected_stages, ["assembly"])
            self.assertIn("read_qc", resolved.resolved_execution_order)
            self.assertIn("assembly", resolved.resolved_execution_order)
            self.assertEqual(resolved.selected_methods, {"assembly": "assembly.candidate.flye"})

    def test_mixed_asset_types_does_not_auto_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            asm = Path(tmp) / "asm.fa"
            asm.write_text(">ctg1\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n"
                f"  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n"
                f"  - asset_type: assembly_fasta_registered\n    paths:\n      - {asm}\n    format: fasta\n    platform: unknown\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest))
            resolved = auto_resolve_plan(plan)
            self.assertIsNone(resolved.selected_stages)

    def test_already_resolved_plan_not_changed(self):
        plan = _base_plan(selected_stages=["read_qc"])
        resolved = auto_resolve_plan(plan)
        self.assertEqual(resolved.selected_stages, ["read_qc"])

    def test_nanopore_fasta_skips_to_parameter_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "bht.Nanopore.pass.first500.fa"
            reads.write_text(">r1\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fasta\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest), data_dir=tmp)
            resolved = auto_resolve_plan(plan)
            plan_ref = save_plan_to_default_path(resolved)
            clarification = next_clarification(resolved, ask_plan_ref=plan_ref)
            self.assertIn(clarification.kind, ("parameter_values", "confirm"))


class InteractiveLoopTests(unittest.TestCase):
    """Tests exercise the plain-text fallback path (questionary disabled)."""

    def test_confirm_y_proceeds_to_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\ngenome_size_estimate: 2000000\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: cycloneseq\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={"command": "run", "manifest": str(manifest), "data_dir": tmp},
            )
            args = _ask_ns()
            # "y" for auto-resolve confirm, "y" for final confirm
            inputs = iter(["y", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("builtins.input", side_effect=lambda _: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()

    def test_confirm_n_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\ngenome_size_estimate: 2000000\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: cycloneseq\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={"command": "run", "manifest": str(manifest), "data_dir": tmp},
            )
            args = _ask_ns()
            # "y" auto-resolve, "n" reject confirm -> restarts -> stage select (Enter=suggested), method (Enter=default), "y" confirm
            inputs = iter(["y", "n", "", "", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("builtins.input", side_effect=lambda _: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()

    def test_parameter_prompt_then_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest), data_dir=tmp,
                             entry_request={"command": "run", "manifest": str(manifest), "data_dir": tmp})
            args = _ask_ns()
            # "y" for auto-resolve confirm, "500m" for genome_size, "y" for final confirm
            inputs = iter(["y", "500m", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("builtins.input", side_effect=lambda _: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()


class ResumeTests(unittest.TestCase):
    def test_resume_loads_existing_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = _base_plan(data_dir=tmp, session_id="resume-test")
            ref = save_plan_to_default_path(plan)
            from app.schemas.ask_plan import load_ask_plan
            loaded = load_ask_plan(ref)
            self.assertEqual(loaded.session_id, "resume-test")


class SetParamsTests(unittest.TestCase):
    def test_parse_set_params_basic(self):
        result = parse_set_params(["genome_size=500m", "threads=8"])
        self.assertEqual(result, {"genome_size": "500m", "threads": "8"})

    def test_parse_set_params_invalid(self):
        with self.assertRaises(ValueError):
            parse_set_params(["invalid"])

    def test_apply_set_params_merges_genome_size(self):
        plan = _base_plan(
            selected_stages=["assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"assembly": "assembly.candidate.flye"},
        )
        updated = _apply_set_params(plan, ["genome_size=500m"])
        params = updated.method_parameters or {}
        flye = params.get("assembly.candidate.flye", {})
        self.assertEqual(flye.get("estimated_genome_size"), 500_000_000)

    def test_parse_genome_size_suffixes(self):
        self.assertEqual(_parse_genome_size("500m"), 500_000_000)
        self.assertEqual(_parse_genome_size("2.5g"), 2_500_000_000)
        self.assertEqual(_parse_genome_size("100k"), 100_000)
        self.assertEqual(_parse_genome_size("1234"), 1234)


class NonInteractiveProtocolTests(unittest.TestCase):
    def test_non_interactive_emits_protocol_fields(self):
        """Existing non-TTY behavior is preserved (tested via test_entrypoints.py too)."""
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            plan = _base_plan(manifest_ref=str(manifest), data_dir=tmp)
            plan_ref = save_plan_to_default_path(plan)
            clarification = next_clarification(plan, ask_plan_ref=plan_ref)
            self.assertIsNotNone(clarification)
            self.assertEqual(clarification.status, "clarification_required")
            self.assertIn("ask_plan_ref", type(clarification).model_fields)


if __name__ == "__main__":
    unittest.main()
