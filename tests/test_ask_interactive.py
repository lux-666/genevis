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
    _prompt_parameter_values,
    _render_confirm,
    _update_derived_defaults,
)
from app.entrypoints.ask_planner import (
    auto_resolve_plan,
    next_clarification,
    save_plan_to_default_path,
)
from app.schemas.ask_plan import AskClarificationRequest, AskPlanState


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

    def test_nanopore_fasta_asks_for_read_qc_filter_choice(self):
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
            self.assertEqual(clarification.kind, "method_selection")
            self.assertIn("read_qc", clarification.choices["methods_by_stage"])


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
            # "y" auto-resolve, "" read_qc filter, "" assembly method,
            # "" x3 for params (accept defaults), "y" confirm
            inputs = iter(["y", "", "", "", "", "", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("builtins.input", side_effect=lambda _: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()

    def test_confirm_render_includes_shared_parameters(self):
        req = AskClarificationRequest(
            status="confirm_required",
            kind="confirm",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Proceed?",
            summary={
                "session_id": "s1",
                "data_dir": "data",
                "manifest_ref": "test.yaml",
                "resolved_execution_order": ["assembly"],
                "stage_roles": {"assembly": "executable"},
                "selected_methods": {"assembly": "assembly.candidate.flye"},
                "shared_parameters": {"read_type": "ont_raw", "estimated_genome_size": 500_000_000},
                "method_parameters": {"assembly.candidate.flye": {"threads": 4}},
            },
            response_schema={},
            next_action={},
        )
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_confirm(req)

        rendered = stdout.getvalue()
        self.assertIn("Shared parameters:", rendered)
        self.assertIn("read_type: ont_raw", rendered)
        self.assertIn("estimated_genome_size: 500000000", rendered)

    def test_parameter_prompt_maps_flye_read_type_alias(self):
        req = AskClarificationRequest(
            status="clarification_required",
            kind="parameter_values",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Parameters",
            choices={"shared_defaults": {}},
            required_fields=["shared_parameters.read_type"],
            response_schema={},
            next_action={},
        )
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", return_value="nano-raw"):
            response = _prompt_parameter_values(req)

        self.assertIsNotNone(response.shared_parameters)
        self.assertEqual(response.shared_parameters.read_type, "ont_raw")

    def test_parameter_prompt_retries_invalid_shared_read_type(self):
        req = AskClarificationRequest(
            status="clarification_required",
            kind="parameter_values",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Parameters",
            choices={"shared_defaults": {}},
            required_fields=["shared_parameters.read_type"],
            response_schema={},
            next_action={},
        )
        inputs = iter(["nano-old", "pacbio-hifi"])
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", side_effect=lambda _: next(inputs)):
            response = _prompt_parameter_values(req)

        self.assertIsNotNone(response.shared_parameters)
        self.assertEqual(response.shared_parameters.read_type, "pacbio_hifi")

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
            # "n" reject auto-resolve immediately,
            # "assembly" stage select (just assembly, avoids assembly_review),
            # "" read_qc filter, "" assembly method,
            # "" "" "" shared/flye params, "n" reject confirm,
            # "assembly" stage select again,
            # "" read_qc filter, "" assembly method,
            # "" "" "" shared/flye params, "y" confirm
            inputs = iter([
                "n",
                "assembly", "", "", "", "", "", "n",
                "assembly", "", "", "", "", "", "y",
            ])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("builtins.input", side_effect=lambda _: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()

    def test_confirm_n_restart_clears_shared_parameters(self):
        plan = _base_plan(
            selected_stages=["assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={
                "read_qc": [
                    "read_qc.long_read_stats.seqkit_stats",
                    "read_qc.length_filter.skip",
                    "read_qc.make_fofn.local",
                ],
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={"read_type": "ont_raw", "estimated_genome_size": 500_000_000},
            method_parameters={"assembly.candidate.flye": {"threads": 4}},
        )
        args = _ask_ns()
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", side_effect=["n", EOFError]), \
             patch("app.entrypoints.ask_interactive.save_plan_to_default_path") as mock_save:
            code = run_interactive_loop(plan, args)

        self.assertEqual(code, 1)
        restarted = mock_save.call_args_list[1].args[0]
        self.assertIsNone(restarted.shared_parameters)
        self.assertIsNone(restarted.method_parameters)

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
            # "y" auto-resolve, "" read_qc filter, "" assembly method,
            # "" read_type [ont_raw], "500m" genome_size, "" threads [4], "y" confirm
            inputs = iter(["y", "", "", "", "500m", "", "y"])
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
        shared = updated.shared_parameters
        self.assertIsNotNone(shared)
        self.assertEqual(shared.estimated_genome_size, 500_000_000)

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


class DerivedDefaultsTests(unittest.TestCase):
    def test_busco_lineage_path_derives_name_version_source(self):
        defaults: dict = {}
        _update_derived_defaults("assembly_review.eval.busco", "lineage_path", "/db/busco/insecta_odb10", defaults)
        self.assertEqual(defaults["assembly_review.eval.busco"]["lineage_name"], "insecta_odb10")
        self.assertEqual(defaults["assembly_review.eval.busco"]["lineage_version"], "odb10")
        self.assertEqual(defaults["assembly_review.eval.busco"]["lineage_source"], "local")

    def test_busco_lineage_path_no_odb_version(self):
        defaults: dict = {}
        _update_derived_defaults("assembly_review.eval.busco", "lineage_path", "/db/custom_lineage", defaults)
        self.assertEqual(defaults["assembly_review.eval.busco"]["lineage_name"], "custom_lineage")
        self.assertNotIn("lineage_version", defaults["assembly_review.eval.busco"])

    def test_busco_does_not_overwrite_existing_defaults(self):
        defaults: dict = {"assembly_review.eval.busco": {"lineage_name": "manual_name"}}
        _update_derived_defaults("assembly_review.eval.busco", "lineage_path", "/db/insecta_odb10", defaults)
        self.assertEqual(defaults["assembly_review.eval.busco"]["lineage_name"], "manual_name")

    def test_non_busco_method_is_noop(self):
        defaults: dict = {}
        _update_derived_defaults("assembly.candidate.flye", "threads", "8", defaults)
        self.assertEqual(defaults, {})


if __name__ == "__main__":
    unittest.main()
