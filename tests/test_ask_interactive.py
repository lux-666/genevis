"""Tests for ask interactive mode and auto-resolve."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.entrypoints.ask_interactive import (
    is_interactive,
    _UserAbort,
    _parse_set_params,
    run_interactive_loop,
    _apply_set_params,
    _attempt_preflight_validation,
    _draft_attention_items,
    _edit_draft_stage,
    _group_missing_fields_by_scope,
    _parse_genome_size,
    _prompt_parameter_values,
    _prompt_text_value,
    _render_draft_review,
    _render_confirm,
    _render_plan_summary,
    _update_derived_defaults,
)
from app.entrypoints.ask_planner import (
    build_initial_plan_draft,
    build_run_plan_summary,
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


class BuildInitialPlanDraftTests(unittest.TestCase):
    def test_no_workflow_goal_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest))
            self.assertIsNone(build_initial_plan_draft(plan))

    def test_assembly_goal_raw_long_reads_prefills_flye_without_read_qc(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest), entry_request={"requested_stages": ["assembly"]})
            draft = build_initial_plan_draft(plan)
            self.assertIsNotNone(draft)
            self.assertEqual(draft.selected_stages, ["assembly"])
            self.assertEqual(draft.resolved_execution_order, ["assembly"])
            self.assertEqual(draft.selected_methods["assembly"], "assembly.candidate.flye")
            self.assertNotIn("read_qc", draft.selected_methods)

    def test_assembly_goal_mixed_assets_leaves_assembly_method_pending(self):
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
            plan = _base_plan(manifest_ref=str(manifest), entry_request={"requested_stages": ["assembly"]})
            draft = build_initial_plan_draft(plan)
            self.assertEqual(draft.selected_stages, ["assembly"])
            self.assertNotIn("assembly", draft.selected_methods or {})

    def test_multi_stage_goal_keeps_requested_execution_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                entry_request={"requested_stages": ["assembly", "gene_annotation"]},
            )
            draft = build_initial_plan_draft(plan)
            self.assertEqual(draft.selected_stages, ["assembly", "gene_annotation"])
            self.assertEqual(draft.resolved_execution_order, ["assembly", "gene_annotation"])

    def test_assembly_goal_draft_accepted_asks_only_for_missing_genome_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "bht.Nanopore.pass.first500.fa"
            reads.write_text(">r1\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fasta\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={"requested_stages": ["assembly"]},
            )
            draft = build_initial_plan_draft(plan)
            plan_ref = save_plan_to_default_path(draft)
            clarification = next_clarification(draft, ask_plan_ref=plan_ref)
            self.assertEqual(clarification.kind, "parameter_values")
            self.assertIn("shared_parameters.estimated_genome_size", clarification.required_fields)


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
                entry_request={
                    "command": "run",
                    "manifest": str(manifest),
                    "data_dir": tmp,
                    "requested_stages": ["assembly"],
                },
            )
            args = _ask_ns()
            # Draft prefills flye + genome size (from manifest) + platform.
            # "y" accept draft (nothing left to clarify), "y" confirm.
            inputs = iter(["y", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("app.entrypoints.ask_interactive._attempt_preflight_validation", return_value=False), \
                 patch("builtins.input", side_effect=lambda *args: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()

    def test_draft_continue_with_missing_fields_blocks_until_stage_edit_completes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={
                    "command": "run",
                    "manifest": str(manifest),
                    "data_dir": tmp,
                    "requested_stages": ["assembly"],
                },
            )
            args = _ask_ns()
            inputs = iter(["", "1p", "500m", "", "", "", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("app.entrypoints.ask_interactive._attempt_preflight_validation", return_value=False), \
                 patch("builtins.input", side_effect=lambda *args: next(inputs)), \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)

            self.assertEqual(code, 0)
            self.assertIn("Cannot continue yet.", stdout.getvalue())
            self.assertIn("assembly: estimated_genome_size is missing", stdout.getvalue())
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
            response = _prompt_parameter_values(req, _base_plan())

        self.assertIsNotNone(response.shared_parameters)
        self.assertEqual(response.shared_parameters.read_type, "ont_raw")

    def test_parameter_prompt_does_not_collect_autohic_inputs(self):
        plan = _base_plan(
            selected_stages=["hic_scaffolding"],
            resolved_execution_order=["hic_scaffolding"],
            selected_methods={"hic_scaffolding": ["hic_scaffolding.candidate.autohic"]},
        )
        clarification = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(clarification.kind, "confirm")

    def test_parameter_prompt_does_not_ask_for_autohic_runtime_defaults(self):
        plan = _base_plan(
            selected_stages=["hic_scaffolding"],
            resolved_execution_order=["hic_scaffolding"],
            selected_methods={"hic_scaffolding": ["hic_scaffolding.candidate.autohic"]},
            method_parameters={
                "hic_scaffolding.candidate.autohic": {"threads": 40}
            },
        )
        clarification = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(clarification.kind, "confirm")

    def test_hic_draft_hides_and_rejects_method_selection(self):
        plan = _base_plan(
            selected_stages=["hic_scaffolding"],
            resolved_execution_order=["hic_scaffolding"],
            selected_methods={
                "hic_scaffolding": ["hic_scaffolding.candidate.autohic"]
            },
        )
        with patch("builtins.input", side_effect=["m", "b"]), patch(
            "sys.stdout", new_callable=io.StringIO
        ) as stdout, patch(
            "app.entrypoints.ask_interactive._edit_stage_methods"
        ) as edit_methods:
            updated = _edit_draft_stage(plan, "hic_scaffolding")

        self.assertEqual(updated, plan)
        edit_methods.assert_not_called()
        rendered = stdout.getvalue()
        self.assertNotIn("m      change method", rendered)
        self.assertNotIn("methods:", rendered)
        self.assertIn("fixed AutoHiC pipeline", rendered)

    def test_parameter_prompt_accepts_function_database_provenance_fields(self):
        req = AskClarificationRequest(
            status="clarification_required",
            kind="parameter_values",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Parameters",
            choices={"stage_defaults": {}},
            required_fields=["function_annotation.database_provenance"],
            response_schema={},
            next_action={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "uniprot.dmnd"
            db_path.write_text("placeholder", encoding="utf-8")
            plan = _base_plan(
                data_dir="/data",
                selected_stages=["function_annotation"],
                resolved_execution_order=["function_annotation"],
                selected_methods={"function_annotation": ["function_annotation.candidate.diamond"]},
            )
            inputs = iter([str(db_path), "UniProt", "2026_02"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("builtins.input", side_effect=lambda _: next(inputs)):
                response = _prompt_parameter_values(req, plan)

        self.assertIsNotNone(response.stage_parameters)
        provenance = response.stage_parameters["function_annotation"]["database_provenance"]
        self.assertEqual(provenance[0]["method_name"], "function_annotation.candidate.diamond")
        self.assertEqual(provenance[0]["database_name"], "UniProt")
        self.assertIsNone(provenance[0]["input_proteins_ref"])

    def test_parameter_prompt_rejects_missing_function_database_path(self):
        req = AskClarificationRequest(
            status="clarification_required",
            kind="parameter_values",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Parameters",
            choices={"stage_defaults": {}},
            required_fields=["function_annotation.database_provenance"],
            response_schema={},
            next_action={},
        )
        plan = _base_plan(
            data_dir="/data",
            selected_stages=["function_annotation"],
            resolved_execution_order=["function_annotation"],
            selected_methods={"function_annotation": ["function_annotation.candidate.diamond"]},
        )
        inputs = iter(["/missing/db1.dmnd", "/missing/db2.dmnd", "/missing/db3.dmnd"])
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", side_effect=lambda _: next(inputs)):
            with self.assertRaises(_UserAbort):
                _prompt_parameter_values(req, plan)

    def test_parameter_prompt_accepts_platform_default(self):
        req = AskClarificationRequest(
            status="clarification_required",
            kind="parameter_values",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Parameters",
            choices={"shared_defaults": {"platform": "unknown"}},
            required_fields=["shared_parameters.platform"],
            response_schema={},
            next_action={},
        )
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", return_value="ont"):
            response = _prompt_parameter_values(req, _base_plan())

        self.assertIsNotNone(response.shared_parameters)
        self.assertEqual(response.shared_parameters.platform, "ont")

    def test_parameter_prompt_accepts_platform_number(self):
        req = AskClarificationRequest(
            status="clarification_required",
            kind="parameter_values",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Parameters",
            choices={"shared_defaults": {"platform": "unknown"}},
            required_fields=["shared_parameters.platform"],
            response_schema={},
            next_action={},
        )
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", return_value="2"):
            response = _prompt_parameter_values(req, _base_plan())

        self.assertIsNotNone(response.shared_parameters)
        self.assertEqual(response.shared_parameters.platform, "ont")

    def test_parameter_prompt_derives_read_type_from_platform(self):
        req = AskClarificationRequest(
            status="clarification_required",
            kind="parameter_values",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Parameters",
            choices={"shared_defaults": {"platform": "unknown"}},
            required_fields=["shared_parameters.platform", "shared_parameters.read_type"],
            response_schema={},
            next_action={},
        )
        inputs = iter(["1"])
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", side_effect=lambda _: next(inputs)):
            response = _prompt_parameter_values(req, _base_plan())

        self.assertIsNotNone(response.shared_parameters)
        self.assertEqual(response.shared_parameters.platform, "cycloneseq")
        self.assertEqual(response.shared_parameters.read_type, "cyclone")

    def test_apply_set_params_accepts_platform_alias(self):
        plan = _base_plan()

        updated = _apply_set_params(plan, ["platform=nanopore"])

        self.assertIsNotNone(updated.shared_parameters)
        self.assertEqual(updated.shared_parameters.platform, "ont")
        self.assertEqual(updated.shared_parameters.read_type, "ont_raw")

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
            response = _prompt_parameter_values(req, _base_plan())

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
                entry_request={
                    "command": "run",
                    "manifest": str(manifest),
                    "data_dir": tmp,
                    "requested_stages": ["assembly"],
                },
            )
            args = _ask_ns()
            # "n" reject draft -> manual editing.
            # "" stage selection (intent default = assembly),
            # "" assembly method, "" platform, "" genome size, "" threads,
            # "n" reject confirm -> restart from stage selection,
            # "" stage selection, "" assembly method, "" platform, "" genome size,
            # "" threads, "y" confirm
            inputs = iter([
                "n",
                "", "", "", "", "", "n",
                "", "", "", "", "", "y",
            ])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("app.entrypoints.ask_interactive._attempt_preflight_validation", return_value=False), \
                 patch("builtins.input", side_effect=lambda *args: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()

    def test_confirm_n_restart_clears_shared_parameters(self):
        plan = _base_plan(
            selected_stages=["assembly"],
            resolved_execution_order=["assembly"],
            selected_methods={
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={"read_type": "ont_raw", "estimated_genome_size": 500_000_000},
            method_parameters={"assembly.candidate.flye": {"threads": 4}},
        )
        args = _ask_ns()
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("app.entrypoints.ask_interactive._attempt_preflight_validation", return_value=False), \
             patch("builtins.input", side_effect=["n", EOFError]), \
             patch("app.entrypoints.ask_interactive.save_plan_to_default_path") as mock_save:
            code = run_interactive_loop(plan, args)

        self.assertEqual(code, 1)
        restarted = mock_save.call_args_list[1].args[0]
        self.assertIsNone(restarted.shared_parameters)
        self.assertIsNone(restarted.method_parameters)

    def test_provide_data_revalidation_pass_continues_to_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                selected_stages=["assembly"],
                resolved_execution_order=["assembly"],
                selected_methods={"assembly": "assembly.candidate.flye"},
                shared_parameters={"read_type": "ont_raw", "estimated_genome_size": 500_000_000},
                method_parameters={"assembly.candidate.flye": {"threads": 4}},
            )
            args = _ask_ns()
            inputs = iter(["", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("app.entrypoints.ask_interactive._attempt_preflight_validation", side_effect=[True, False]) as mock_preflight, \
                 patch("app.entrypoints.ask_interactive._prompt_validation_action", return_value="provide_data"), \
                 patch("builtins.input", side_effect=lambda *args: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)

            self.assertEqual(code, 0)
            self.assertEqual(mock_preflight.call_count, 2)
            mock_exec.assert_called_once()

    def test_provide_data_interrupt_prints_single_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                selected_stages=["assembly"],
                resolved_execution_order=["assembly"],
                selected_methods={"assembly": "assembly.candidate.flye"},
                shared_parameters={"read_type": "ont_raw", "estimated_genome_size": 500_000_000},
                method_parameters={"assembly.candidate.flye": {"threads": 4}},
            )
            args = _ask_ns()
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("app.entrypoints.ask_interactive._attempt_preflight_validation", return_value=True), \
                 patch("app.entrypoints.ask_interactive._prompt_validation_action", return_value="provide_data"), \
                 patch("builtins.input", side_effect=KeyboardInterrupt), \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout:
                code = run_interactive_loop(plan, args)

            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue().count("Aborted."), 1)

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
                             entry_request={
                                 "command": "run",
                                 "manifest": str(manifest),
                                 "data_dir": tmp,
                                 "requested_stages": ["assembly"],
                             })
            args = _ask_ns()
            # Edit assembly parameters from the draft, fill genome size, return
            # to the plan, continue, and confirm the final run plan.
            inputs = iter(["1p", "500m", "", "", "", "y"])
            with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
                 patch("app.entrypoints.ask_interactive._attempt_preflight_validation", return_value=False), \
                 patch("builtins.input", side_effect=lambda _: next(inputs)), \
                 patch("app.entrypoints.ask_interactive._execute_confirmed_plan", return_value=0) as mock_exec:
                code = run_interactive_loop(plan, args)
            self.assertEqual(code, 0)
            mock_exec.assert_called_once()

    def test_edit_draft_stage_returns_after_repeated_invalid_actions(self):
        plan = _base_plan(
            selected_stages=["assembly"],
            resolved_execution_order=["assembly"],
            selected_methods={"assembly": "assembly.candidate.flye"},
        )
        inputs = iter(["x", "?", "nope"])
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", side_effect=lambda _: next(inputs)):
            updated = _edit_draft_stage(plan, "assembly")
        self.assertEqual(updated, plan)

    def test_required_text_value_reprompts_then_aborts_on_empty_input(self):
        inputs = iter(["", "", ""])
        with patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False), \
             patch("builtins.input", side_effect=lambda _: next(inputs)):
            with self.assertRaises(_UserAbort):
                _prompt_text_value("required field", None, required=True)

    def test_preflight_validation_exception_blocks_confirmation(self):
        plan = _base_plan(manifest_ref="manifest.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            plan = plan.model_copy(update={"manifest_ref": str(manifest)})
            with patch("app.stages.inventory.nodes.validate_inputs", side_effect=RuntimeError("boom")):
                self.assertTrue(_attempt_preflight_validation(plan))

    def test_preflight_validation_isolated_from_shared_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_data = root / "data"
            reads = root / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\n"
                "assets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths: [{reads!s}]\n"
                "    platform: ont\n"
                "    format: fastq\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                data_dir=str(shared_data),
                manifest_ref=str(manifest),
                selected_stages=["read_qc"],
            )
            captured = {}
            from app.stages.inventory.nodes import register_assets as real_register_assets

            def capture_register(state):
                captured["data_dir"] = Path(state["data_dir"])
                return real_register_assets(state)

            with patch("app.stages.inventory.nodes.register_assets", side_effect=capture_register):
                self.assertFalse(_attempt_preflight_validation(plan))

            self.assertIn("data_dir", captured)
            self.assertFalse(captured["data_dir"].exists())
            self.assertFalse(shared_data.exists())


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
        result = _parse_set_params(["genome_size=500m", "threads=8"])
        self.assertEqual(result, {"genome_size": "500m", "threads": "8"})

    def test_parse_set_params_invalid(self):
        with self.assertRaises(ValueError):
            _parse_set_params(["invalid"])

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


class PlanPreviewRenderTests(unittest.TestCase):
    def test_preview_marks_pending_methods_and_params(self):
        summary = {
            "session_id": "s1",
            "data_dir": "data",
            "manifest_ref": "test.yaml",
            "source_paths": ["/in/reads.fastq"],
            "resolved_execution_order": ["read_qc", "assembly"],
            "stage_roles": {"read_qc": "dependency", "assembly": "executable"},
            "selected_methods": {"assembly": "assembly.candidate.flye"},
        }
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_plan_summary(summary, title="Plan Preview", preview=True)
        rendered = stdout.getvalue()
        self.assertIn("Plan Preview", rendered)
        self.assertIn("Input:    /in/reads.fastq", rendered)
        self.assertIn("read_qc", rendered)
        self.assertIn("assembly: assembly.candidate.flye", rendered)
        # read_qc has no selected method yet -> pending
        self.assertIn("read_qc: pending", rendered)
        self.assertIn("Shared parameters: pending", rendered)
        self.assertIn("Parameters: pending", rendered)
        self.assertIn("Advisory preview.", rendered)

    def test_preview_shows_parser_source_recommended_defaults_and_pending_fields(self):
        summary = {
            "session_id": "s1",
            "data_dir": "data",
            "manifest_ref": "test.yaml",
            "intent_parse_source": "deterministic_fallback",
            "resolved_execution_order": ["genome_survey", "hic_scaffolding"],
            "stage_roles": {"genome_survey": "executable", "hic_scaffolding": "executable"},
            "selected_methods": {
                "genome_survey": [
                    "genome_survey.kmer_count.jellyfish",
                    "genome_survey.model.genomescope2",
                ],
                "hic_scaffolding": ["hic_scaffolding.candidate.autohic"],
            },
            "shared_parameter_defaults": {"platform": "ont", "read_type": "ont_raw"},
            "parameter_defaults": {
                "genome_survey.kmer_count.jellyfish": {"kmer_size": 21, "hash_size": "1G", "threads": 4},
                "genome_survey.model.genomescope2": {"kmer_size": 21},
                "hic_scaffolding.candidate.autohic": {
                    "threads": 10,
                    "number_of_edit_rounds": 2,
                    "debris_adjust": False,
                },
            },
            "parameter_default_sources": {
                "genome_survey.kmer_count.jellyfish": {"kmer_size": "default", "hash_size": "default", "threads": "default"},
                "hic_scaffolding.candidate.autohic": {
                    "threads": "default",
                    "number_of_edit_rounds": "default",
                    "debris_adjust": "default",
                },
            },
            "missing_parameter_fields": [
                "hic_scaffolding.candidate.autohic.restriction_enzyme",
                "hic_scaffolding.candidate.autohic.error_model_ref",
                "hic_scaffolding.candidate.autohic.chromosome_model_ref",
            ],
            "stage_plan_details": [
                {
                    "stage": "genome_survey",
                    "role": "executable",
                    "status": "configured",
                    "inputs": [],
                    "methods": [
                        {
                            "method": "genome_survey.kmer_count.jellyfish",
                            "parameters": {"kmer_size": 21, "hash_size": "1G", "threads": 4},
                            "missing_parameters": [],
                        }
                    ],
                },
                {
                    "stage": "hic_scaffolding",
                    "role": "executable",
                    "status": "configured",
                    "inputs": ["missing paired Hi-C reads in manifest"],
                    "methods": [
                        {
                            "method": "hic_scaffolding.candidate.autohic",
                            "parameters": {"threads": 10, "number_of_edit_rounds": 2},
                            "missing_parameters": ["restriction_enzyme", "error_model_ref", "chromosome_model_ref"],
                        }
                    ],
                },
                {
                    "stage": "t2t",
                    "role": "executable",
                    "status": "no registered method yet",
                    "inputs": ["T2T finishing usually needs Hi-C/contact-map or telomere evidence; none registered in manifest"],
                    "methods": [],
                },
            ],
        }
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_plan_summary(summary, title="Plan Preview", preview=True)
        rendered = stdout.getvalue()
        self.assertIn("Intent parser: local deterministic fallback", rendered)
        self.assertIn("platform: ont", rendered)
        self.assertIn("[jellyfish] kmer_size: 21  (default)", rendered)
        self.assertIn("[autohic] threads: 10  (default)", rendered)
        self.assertIn("[autohic] restriction_enzyme: pending", rendered)
        self.assertIn("[autohic] error_model_ref: pending", rendered)
        self.assertIn("[autohic] chromosome_model_ref: pending", rendered)
        self.assertIn("Stage details:", rendered)
        self.assertIn("hic_scaffolding: [run] configured", rendered)
        self.assertIn("input: missing paired Hi-C reads in manifest", rendered)
        self.assertIn("t2t: [run] no registered method yet", rendered)

    def test_preview_renders_input_assets(self):
        summary = {
            "session_id": "s1",
            "data_dir": "data",
            "manifest_ref": "test.yaml",
            "input_assets": [
                {
                    "asset_type": "raw_long_reads",
                    "platform": "ont",
                    "format": "fastq",
                    "paths": ["/in/reads.fastq.gz"],
                }
            ],
        }
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_plan_summary(summary, title="Plan Preview", preview=True)
        rendered = stdout.getvalue()
        self.assertIn("Inputs:", rendered)
        self.assertIn("raw_long_reads [ont/fastq]: /in/reads.fastq.gz", rendered)

    def test_confirm_render_labels_execution_plan_without_claiming_final_runtime_values(self):
        summary = {
            "session_id": "s1",
            "data_dir": "data",
            "manifest_ref": "test.yaml",
            "resolved_execution_order": ["assembly"],
            "stage_roles": {"assembly": "executable"},
            "selected_methods": {"assembly": "assembly.candidate.flye"},
        }
        req = AskClarificationRequest(
            status="confirm_required", kind="confirm", session_id="s1",
            ask_plan_ref="/tmp/plan.json", manifest_ref="test.yaml",
            question="Proceed?", summary=summary, response_schema={}, next_action={},
        )
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_confirm(req)
        rendered = stdout.getvalue()
        self.assertIn("Execution Plan", rendered)
        self.assertIn("assembly: assembly.candidate.flye", rendered)
        self.assertNotIn("pending", rendered)
        self.assertNotIn("Advisory preview.", rendered)

    def test_plan_draft_renders_recommended_methods_and_input_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest), data_dir=tmp,
                             entry_request={
                                 "command": "run",
                                 "manifest": str(manifest),
                                 "data_dir": tmp,
                                 "requested_stages": ["assembly"],
            })
            draft = build_initial_plan_draft(plan)
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                _render_draft_review(draft, build_run_plan_summary(draft))
            rendered = stdout.getvalue()
            self.assertIn("Plan Draft", rendered)
            self.assertIn(f"raw_long_reads [ont/fastq]: {reads}", rendered)
            self.assertIn("This draft becomes the working plan.", rendered)
            self.assertIn("Execution will NOT start yet.", rendered)
            self.assertIn("Runtime-derived proposals are deferred", rendered)
            self.assertIn("assembly.candidate.flye", rendered)
            self.assertIn("estimated_genome_size: missing", rendered)
            self.assertIn("[flye] threads: 4  (default)", rendered)
            self.assertIn("Needs attention:", rendered)

    def test_plan_summary_exposes_generic_deferred_human_gate_contract(self):
        plan = _base_plan(
            selected_stages=["assembly", "assembly_review", "hic_scaffolding", "t2t"],
            resolved_execution_order=["assembly", "assembly_review", "hic_scaffolding", "t2t"],
            selected_methods={},
        )

        summary = build_run_plan_summary(plan)
        ordered_keys = [
            (point["stage"], point["gate_id"])
            for point in summary["runtime_decision_points"]
        ]
        self.assertLess(
            ordered_keys.index(("assembly", "route_selection")),
            ordered_keys.index(("assembly", "parameter_approval")),
        )
        self.assertLess(
            ordered_keys.index(("assembly", "parameter_approval")),
            ordered_keys.index(("assembly_review", "assembly_review")),
        )
        points = {
            (point["stage"], point["gate_id"]): point
            for point in summary["runtime_decision_points"]
        }

        parameter_gate = points[("assembly", "parameter_approval")]
        self.assertEqual(parameter_gate["status"], "deferred")
        self.assertEqual(parameter_gate["proposal_kind"], "parameter_recommendation")
        self.assertEqual(parameter_gate["parameter_recommendation_status"], "implemented")
        self.assertIn("prerequisite results/evidence", parameter_gate["trigger"])
        self.assertIn(("assembly_review", "assembly_review"), points)
        self.assertEqual(
            points[("hic_scaffolding", "hic_scaffolding")]["parameter_recommendation_status"],
            "implemented",
        )
        self.assertIn(("hic_scaffolding", "parameter_approval"), points)
        self.assertEqual(
            points[("t2t", "t2t_review")]["parameter_recommendation_status"],
            "implemented",
        )
        self.assertIn(("t2t", "parameter_approval"), points)

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_plan_summary(summary, title="Execution Plan")
        rendered = stdout.getvalue()
        self.assertIn("Runtime human gates (proposals are not available yet):", rendered)
        self.assertIn("Assembly parameter proposal: deferred", rendered)
        self.assertIn("The TUI pauses at each reached gate", rendered)

    def test_ont_t2t_draft_defers_hifiasm_l2_until_runtime_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "ont.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n"
                f"  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={"requested_stages": ["read_qc", "genome_survey", "assembly", "assembly_review", "t2t"]},
                selected_stages=["read_qc", "genome_survey", "assembly", "assembly_review", "t2t"],
                resolved_execution_order=["read_qc", "genome_survey", "assembly", "assembly_review", "t2t"],
                selected_methods={"assembly": "assembly.candidate.hifiasm_ont"},
                method_parameters={
                    "assembly.candidate.hifiasm_ont": {
                        "threads": 32,
                        "kmer_length": 31,
                        "purge_level": 2,
                    }
                },
            )

            summary = build_run_plan_summary(plan)
            contract = summary["runtime_parameter_contracts"][0]
            self.assertEqual(contract["status"], "deferred")
            self.assertIn("kmer_length", contract["parameter_names"])
            self.assertIn("purge_level", contract["parameter_names"])
            self.assertEqual(contract["user_constraints"], {"kmer_length": 31, "purge_level": 2})

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                _render_draft_review(plan, summary)
            rendered = stdout.getvalue()
            self.assertIn("[hifiasm_ont] threads: 32", rendered)
            self.assertNotIn("[hifiasm_ont] kmer_length: 31", rendered)
            self.assertNotIn("[hifiasm_ont] purge_level: 2", rendered)
            self.assertIn("runtime-derived parameters [hifiasm_ont]: deferred", rendered)
            self.assertIn("user constraints (not frozen values)", rendered)
            self.assertIn("Assembly parameter proposal: deferred", rendered)

    def test_plan_draft_multi_stage_goal_marks_requested_targets_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fa"
            reads.write_text(">r1\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n"
                f"  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fasta\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={
                    "command": "run",
                    "manifest": str(manifest),
                    "data_dir": tmp,
                    "requested_stages": ["assembly", "gene_annotation"],
                },
            )
            draft = build_initial_plan_draft(plan)
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                _render_draft_review(draft, build_run_plan_summary(draft))
            rendered = stdout.getvalue()
            self.assertIn("Plan Draft", rendered)
            self.assertIn(f"raw_long_reads [ont/fasta]: {reads}", rendered)
            self.assertIn("1. assembly", rendered)
            self.assertIn("2. gene_annotation", rendered)
            self.assertIn("1          edit assembly", rendered)
            self.assertIn("2          edit gene_annotation", rendered)
            self.assertNotIn("read_qc", rendered)
            self.assertNotIn("repeat_annotation", rendered)
            self.assertNotIn("assembly_review: pending", rendered)
            self.assertIn("Actions:", rendered)

    def test_plan_draft_attention_places_missing_fields_in_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={
                    "command": "run",
                    "manifest": str(manifest),
                    "data_dir": tmp,
                    "requested_stages": ["assembly"],
                },
            )
            draft = build_initial_plan_draft(plan)
            attention = _draft_attention_items(draft, build_run_plan_summary(draft))
            self.assertIn(("assembly", "estimated_genome_size is missing"), attention)
            self.assertNotIn(("assembly", "platform must be confirmed"), attention)
            self.assertNotIn(("assembly", "threads must be confirmed"), attention)

    def test_build_initial_plan_draft_skips_when_no_goal_was_parsed(self):
        plan = _base_plan(entry_request={"command": "run", "manifest": "test.yaml", "data_dir": "data"})
        self.assertIsNone(build_initial_plan_draft(plan))


class GroupMissingFieldsByScopeTests(unittest.TestCase):
    def test_shared_stage_and_method_fields_are_grouped(self):
        grouped = _group_missing_fields_by_scope(
            ["shared_parameters.platform", "assembly.eval", "assembly.candidate.flye.threads"],
            methods={"assembly.candidate.flye"},
        )
        self.assertEqual(grouped["shared"], ["platform"])
        self.assertEqual(grouped["stage"], {"assembly": ["eval"]})
        self.assertEqual(grouped["method"], {"assembly.candidate.flye": ["threads"]})

    def test_single_part_field_is_dropped_and_logged(self):
        with self.assertLogs("app.entrypoints.ask_interactive", level="DEBUG") as logs:
            grouped = _group_missing_fields_by_scope(["platform"])
        self.assertEqual(grouped, {"shared": [], "stage": {}, "method": {}})
        self.assertTrue(any("platform" in line for line in logs.output))

    def test_unmatched_multipart_field_is_dropped_and_logged(self):
        with self.assertLogs("app.entrypoints.ask_interactive", level="DEBUG") as logs:
            grouped = _group_missing_fields_by_scope(
                ["assembly.candidate.flye.threads"],
                methods={"other.method"},
            )
        self.assertEqual(grouped["method"], {})
        self.assertEqual(grouped["stage"], {})
        self.assertTrue(any("assembly.candidate.flye.threads" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
