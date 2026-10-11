"""Tests for ask interactive mode and auto-resolve."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.entrypoints.ask_interactive import (
    _draft_attention_items,
    _edit_draft_stage,
    _group_missing_fields_by_scope,
    _prompt_stage_selection,
    _render_confirm,
    _render_draft_review,
    _render_plan_summary,
)
from app.entrypoints.ask_planner import (
    build_initial_plan_draft,
    build_run_plan_summary,
    next_clarification,
)
from app.schemas.ask_plan import (
    AskClarificationRequest,
    AskPlanState,
    PlanStageOccurrence,
)


def _base_plan(**overrides) -> AskPlanState:
    stages = overrides.pop("stage_names", None)
    if stages is not None:
        overrides["selected_stage_occurrences"] = [
            PlanStageOccurrence(stage=stage, allow_local_execution=True)
            for stage in stages
        ]
    base = dict(
        session_id="s1",
        data_dir="data",
        entry_request={"command": "run", "manifest": "test.yaml", "data_dir": "data"},
        manifest_ref="test.yaml",
    )
    base.update(overrides)
    return AskPlanState(**base)


class BuildInitialPlanDraftTests(unittest.TestCase):
    def test_no_workflow_goal_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
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
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                entry_request={"requested_stages": ["assembly"]},
            )
            draft = build_initial_plan_draft(plan)
            self.assertIsNotNone(draft)
            self.assertEqual(draft.selected_stage_names, ["assembly"])
            self.assertNotIn("assembly", draft.selected_methods or {})
            self.assertNotIn("read_qc", draft.selected_methods or {})

    def test_assembly_goal_mixed_assets_leaves_assembly_method_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            asm = Path(tmp) / "asm.fa"
            asm.write_text(">ctg1\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n"
                "  - asset_type: assembly_fasta_registered\n"
                f"    paths:\n      - {asm}\n"
                "    format: fasta\n    platform: unknown\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                entry_request={"requested_stages": ["assembly"]},
            )
            draft = build_initial_plan_draft(plan)
            self.assertEqual(draft.selected_stage_names, ["assembly"])
            self.assertNotIn("assembly", draft.selected_methods or {})

    def test_multi_stage_goal_keeps_requested_execution_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                entry_request={"requested_stages": ["assembly", "assembly_review"]},
            )
            draft = build_initial_plan_draft(plan)
            self.assertEqual(
                draft.selected_stage_names, ["assembly", "assembly_review"]
            )

    def test_assembly_goal_draft_accepted_asks_only_for_missing_genome_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "bht.Nanopore.pass.first500.fa"
            reads.write_text(">r1\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fasta\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={"requested_stages": ["assembly"]},
            )
            draft = build_initial_plan_draft(plan)
            plan_ref = Path(tmp) / "tui_state.json"
            clarification = next_clarification(draft, ask_plan_ref=plan_ref)
            self.assertEqual(clarification.kind, "confirm")


class InteractiveLoopTests(unittest.TestCase):
    def test_stage_sequence_prompt_keeps_duplicates_and_collects_each_consent(self):
        request = AskClarificationRequest(
            status="clarification_required",
            kind="stage_selection",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Which stages?",
            choices={
                "available_stages": ["t2t", "assembly_review"],
                "suggested_stage_occurrences": [],
            },
            required_fields=["selected_stage_occurrences"],
            response_schema={"kind": "stage_selection"},
            next_action={},
        )
        with (
            patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False),
            patch(
                "builtins.input",
                side_effect=["t2t,assembly_review,t2t", "n", "y", "y"],
            ),
        ):
            response = _prompt_stage_selection(request)

        self.assertEqual(
            [
                item.model_dump(mode="json")
                for item in response.selected_stage_occurrences
            ],
            [
                {"stage": "t2t", "allow_local_execution": False},
                {"stage": "assembly_review", "allow_local_execution": True},
                {"stage": "t2t", "allow_local_execution": True},
            ],
        )

    def test_stage_sequence_prompt_accepts_inventory_only_recommendation(self):
        request = AskClarificationRequest(
            status="clarification_required",
            kind="stage_selection",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Which stages?",
            choices={
                "available_stages": ["assembly"],
                "suggested_stage_occurrences": [],
            },
            required_fields=["selected_stage_occurrences"],
            response_schema={"kind": "stage_selection"},
            next_action={},
        )
        with (
            patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False),
            patch("builtins.input", return_value=""),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            response = _prompt_stage_selection(request)

        self.assertEqual(response.selected_stage_occurrences, [])
        self.assertIn("Recommended: Inventory only", stdout.getvalue())

    def test_questionary_stage_sequence_accepts_inventory_only_recommendation(self):
        request = AskClarificationRequest(
            status="clarification_required",
            kind="stage_selection",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Which stages?",
            choices={
                "available_stages": ["assembly"],
                "suggested_stage_occurrences": [],
            },
            required_fields=["selected_stage_occurrences"],
            response_schema={"kind": "stage_selection"},
            next_action={},
        )
        questionary = MagicMock()
        questionary.text.return_value.ask.return_value = ""
        with (
            patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", True),
            patch("app.entrypoints.ask_interactive.questionary", questionary),
        ):
            response = _prompt_stage_selection(request)

        self.assertEqual(response.selected_stage_occurrences, [])

    def test_plan_summary_renders_inventory_only(self):
        summary = {
            "session_id": "s1",
            "data_dir": "data",
            "manifest_ref": "test.yaml",
            "selected_stage_names": [],
        }
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_plan_summary(summary, title="Execution Plan")

        self.assertIn("Stages: Inventory only", stdout.getvalue())

    """Tests exercise the plain-text fallback path (questionary disabled)."""

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
                "stage_roles": ["executable"],
                "selected_methods": {"assembly": "assembly.candidate.flye"},
                "shared_parameters": {
                    "read_type": "ont_raw",
                    "estimated_genome_size": 500_000_000,
                },
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

    def test_parameter_prompt_does_not_collect_autohic_inputs(self):
        plan = _base_plan(
            stage_names=["hic_scaffolding"],
            selected_methods={"hic_scaffolding": ["hic_scaffolding.candidate.autohic"]},
        )
        clarification = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(clarification.kind, "confirm")

    def test_parameter_prompt_does_not_ask_for_autohic_runtime_defaults(self):
        plan = _base_plan(
            stage_names=["hic_scaffolding"],
            selected_methods={"hic_scaffolding": ["hic_scaffolding.candidate.autohic"]},
            method_parameters={"hic_scaffolding.candidate.autohic": {"threads": 40}},
        )
        clarification = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(clarification.kind, "confirm")

    def test_hic_draft_hides_and_rejects_method_selection(self):
        plan = _base_plan(
            stage_names=["hic_scaffolding"],
            selected_methods={"hic_scaffolding": ["hic_scaffolding.candidate.autohic"]},
        )
        with (
            patch("builtins.input", side_effect=["m", "b"]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            updated = _edit_draft_stage(plan, 0)

        self.assertEqual(updated, plan)
        rendered = stdout.getvalue()
        self.assertNotIn("m      change method", rendered)
        self.assertNotIn("methods:", rendered)
        self.assertIn("fixed AutoHiC pipeline", rendered)

    def test_review_draft_hides_and_rejects_busco_cancellation(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": [
                    "assembly_review.eval.quast",
                    "assembly_review.eval.busco",
                ]
            },
            method_parameters={
                "assembly_review.eval.quast": {"threads": 4},
                "assembly_review.eval.busco": {
                    "threads": 4,
                },
            },
        )
        with (
            patch("builtins.input", side_effect=["m", "b"]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            updated = _edit_draft_stage(plan, 0)

        self.assertEqual(updated, plan)
        rendered = stdout.getvalue()
        self.assertNotIn("m      change method", rendered)
        self.assertIn("fixed QUAST and BUSCO pipeline", rendered)

    def test_edit_draft_stage_returns_after_repeated_invalid_actions(self):
        plan = _base_plan(
            stage_names=["assembly"],
            selected_methods={"assembly": "assembly.candidate.flye"},
        )
        inputs = iter(["x", "?", "nope"])
        with (
            patch("app.entrypoints.ask_interactive._HAS_QUESTIONARY", False),
            patch("builtins.input", side_effect=lambda _: next(inputs)),
        ):
            updated = _edit_draft_stage(plan, 0)
        self.assertEqual(updated, plan)


class PlannerClarificationTests(unittest.TestCase):
    def test_clarification_contains_tui_state_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text("session_id: s1\nassets: []\n", encoding="utf-8")
            plan = _base_plan(manifest_ref=str(manifest), data_dir=tmp)
            plan_ref = Path(tmp) / "tui_state.json"
            clarification = next_clarification(plan, ask_plan_ref=plan_ref)
            self.assertIsNotNone(clarification)
            self.assertEqual(clarification.status, "clarification_required")
            self.assertEqual(clarification.ask_plan_ref, str(plan_ref.resolve()))


class PlanPreviewRenderTests(unittest.TestCase):
    def test_preview_marks_pending_methods_and_params(self):
        summary = {
            "session_id": "s1",
            "data_dir": "data",
            "manifest_ref": "test.yaml",
            "source_paths": ["/in/reads.fastq"],
            "selected_stage_names": ["read_qc", "assembly"],
            "stage_roles": ["dependency", "executable"],
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
            "stage_roles": ["executable", "executable"],
            "selected_stage_names": ["genome_survey", "hic_scaffolding"],
            "selected_methods": {
                "genome_survey": [
                    "genome_survey.kmer_count.jellyfish",
                    "genome_survey.model.genomescope2",
                ],
                "hic_scaffolding": ["hic_scaffolding.candidate.autohic"],
            },
            "shared_parameter_defaults": {"platform": "ont", "read_type": "ont_raw"},
            "parameter_defaults": {
                "genome_survey.kmer_count.jellyfish": {
                    "kmer_size": 21,
                    "hash_size": "1G",
                    "threads": 4,
                },
                "genome_survey.model.genomescope2": {"kmer_size": 21},
                "hic_scaffolding.candidate.autohic": {
                    "threads": 10,
                    "number_of_edit_rounds": 2,
                    "debris_adjust": False,
                },
            },
            "parameter_default_sources": {
                "genome_survey.kmer_count.jellyfish": {
                    "kmer_size": "default",
                    "hash_size": "default",
                    "threads": "default",
                },
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
                            "parameters": {
                                "kmer_size": 21,
                                "hash_size": "1G",
                                "threads": 4,
                            },
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
                            "missing_parameters": [
                                "restriction_enzyme",
                                "error_model_ref",
                                "chromosome_model_ref",
                            ],
                        }
                    ],
                },
                {
                    "stage": "t2t",
                    "role": "executable",
                    "status": "no registered method yet",
                    "inputs": [],
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

    def test_confirm_render_labels_execution_plan_without_claiming_final_runtime_values(
        self,
    ):
        summary = {
            "session_id": "s1",
            "data_dir": "data",
            "manifest_ref": "test.yaml",
            "stage_roles": ["executable"],
            "selected_methods": {"assembly": "assembly.candidate.flye"},
        }
        req = AskClarificationRequest(
            status="confirm_required",
            kind="confirm",
            session_id="s1",
            ask_plan_ref="/tmp/plan.json",
            manifest_ref="test.yaml",
            question="Proceed?",
            summary=summary,
            response_schema={},
            next_action={},
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
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
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
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                _render_draft_review(draft, build_run_plan_summary(draft))
            rendered = stdout.getvalue()
            self.assertIn("Plan Draft", rendered)
            self.assertIn(f"raw_long_reads [ont/fastq]: {reads}", rendered)
            self.assertIn("This draft becomes the working plan.", rendered)
            self.assertIn("Execution will NOT start yet.", rendered)
            self.assertIn("Runtime-derived proposals are deferred", rendered)
            self.assertNotIn("assembly.candidate.flye", rendered)
            self.assertIn("Route Selection: deferred", rendered)
            self.assertNotIn("[flye] threads:", rendered)
            self.assertNotIn("catalog L2: asm_coverage, min_overlap", rendered)
            self.assertIn("Needs attention:", rendered)
            self.assertIn(
                "run config is not ready: deferred Assembly requires "
                "input_manifest.genome_size_estimate or a prior locally "
                "executed Genome Survey occurrence",
                rendered,
            )

    def test_plan_summary_exposes_generic_deferred_human_gate_contract(self):
        plan = _base_plan(
            stage_names=["assembly", "assembly_review", "hic_scaffolding", "t2t"],
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
        self.assertEqual(
            parameter_gate["parameter_recommendation_status"], "implemented"
        )
        self.assertIn("prerequisite results/evidence", parameter_gate["trigger"])
        self.assertIn(("assembly_review", "assembly_review"), points)
        self.assertEqual(
            points[("hic_scaffolding", "hic_scaffolding")][
                "parameter_recommendation_status"
            ],
            "implemented",
        )
        self.assertIn(("hic_scaffolding", "parameter_approval"), points)
        self.assertEqual(
            points[("t2t", "t2t_review")]["parameter_recommendation_status"],
            "implemented",
        )
        self.assertNotIn(("t2t", "parameter_approval"), points)
        self.assertIn(("t2t", "finishing_recommendation"), points)

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            _render_plan_summary(summary, title="Execution Plan")
        rendered = stdout.getvalue()
        self.assertIn(
            "Runtime human gates (proposals are not available yet):", rendered
        )
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
                entry_request={
                    "requested_stages": [
                        "read_qc",
                        "genome_survey",
                        "assembly",
                        "assembly_review",
                        "t2t",
                    ]
                },
                stage_names=[
                    "read_qc",
                    "genome_survey",
                    "assembly",
                    "assembly_review",
                    "t2t",
                ],
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
            self.assertEqual(
                contract["user_constraints"], {"kmer_length": 31, "purge_level": 2}
            )

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                _render_draft_review(plan, summary)
            rendered = stdout.getvalue()
            self.assertIn("[hifiasm_ont] threads: 32", rendered)
            self.assertNotIn("[hifiasm_ont] kmer_length: 31", rendered)
            self.assertNotIn("[hifiasm_ont] purge_level: 2", rendered)
            self.assertIn(
                "runtime-derived parameters [hifiasm_ont]: deferred", rendered
            )
            self.assertIn("user constraints (not frozen values)", rendered)
            self.assertIn("Assembly parameter proposal: deferred", rendered)

    def test_plan_draft_multi_stage_goal_marks_requested_targets_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fa"
            reads.write_text(">r1\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fasta\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                data_dir=tmp,
                entry_request={
                    "command": "run",
                    "manifest": str(manifest),
                    "data_dir": tmp,
                    "requested_stages": ["assembly", "assembly_review"],
                },
            )
            draft = build_initial_plan_draft(plan)
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                _render_draft_review(draft, build_run_plan_summary(draft))
            rendered = stdout.getvalue()
            self.assertIn("Plan Draft", rendered)
            self.assertIn(f"raw_long_reads [ont/fasta]: {reads}", rendered)
            self.assertIn("1. assembly", rendered)
            self.assertIn("2. assembly_review", rendered)
            self.assertIn("1          edit assembly", rendered)
            self.assertIn("2          edit assembly_review", rendered)
            self.assertNotIn("read_qc", rendered)
            self.assertNotIn("assembly_review: pending", rendered)
            self.assertIn("Actions:", rendered)

    def test_plan_draft_attention_places_missing_fields_in_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
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
            self.assertIn(
                (
                    "assembly",
                    "run config is not ready: deferred Assembly requires "
                    "input_manifest.genome_size_estimate or a prior locally "
                    "executed Genome Survey occurrence",
                ),
                attention,
            )
            self.assertNotIn(("assembly", "platform must be confirmed"), attention)
            self.assertNotIn(("assembly", "threads must be confirmed"), attention)

    def test_build_initial_plan_draft_skips_when_no_goal_was_parsed(self):
        plan = _base_plan(
            entry_request={
                "command": "run",
                "manifest": "test.yaml",
                "data_dir": "data",
            }
        )
        self.assertIsNone(build_initial_plan_draft(plan))


class GroupMissingFieldsByScopeTests(unittest.TestCase):
    def test_shared_stage_and_method_fields_are_grouped(self):
        grouped = _group_missing_fields_by_scope(
            [
                "shared_parameters.platform",
                "assembly.eval",
                "assembly.candidate.flye.threads",
            ],
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
        self.assertTrue(
            any("assembly.candidate.flye.threads" in line for line in logs.output)
        )


if __name__ == "__main__":
    unittest.main()
