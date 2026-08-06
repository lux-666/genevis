"""Tests for ask plan schemas and staged planner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.entrypoints.ask_planner import (
    READ_QC_FILTER_SEQKIT,
    READ_QC_FILTER_SKIP,
    READ_QC_STATS_METHOD,
    _all_selectable_stages,
    _auto_fill_inferred_defaults,
    _extract_busco_version,
    _infer_parameter_defaults,
    _recommended_stages_from_manifest,
    build_initial_plan_draft,
    build_planner_method_catalog,
    build_run_config_payload,
    build_run_plan_summary,
    merge_clarification_response,
    next_clarification,
)
from app.schemas.ask_plan import (
    AskClarificationResponse,
    AskPlanState,
    PlanStageOccurrence,
)
from app.schemas.descriptors import DescriptorRegistry


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


def _read_qc_default_methods() -> list[str]:
    return [READ_QC_STATS_METHOD, READ_QC_FILTER_SKIP]


def _requested_stage_names(payload: dict) -> list[str]:
    return [request["stage"] for request in payload["requested_stages"]]


def _local_execution_stage_names(payload: dict) -> list[str]:
    return [
        request["stage"]
        for request in payload["requested_stages"]
        if request["allow_local_execution"]
    ]


class AskPlanSchemaTests(unittest.TestCase):
    def test_stage_selection_requires_occurrences(self):
        with self.assertRaisesRegex(ValueError, "selected_stage_occurrences"):
            AskClarificationResponse(kind="stage_selection")

    def test_inventory_only_stage_selection_requires_an_explicit_empty_list(self):
        response = AskClarificationResponse(
            kind="stage_selection", selected_stage_occurrences=[]
        )
        self.assertEqual(response.selected_stage_occurrences, [])

    def test_inventory_only_selection_clears_stale_choices_and_advances_to_confirm(
        self,
    ):
        plan = _base_plan(
            stage_names=["assembly"],
            selected_methods={"assembly": "assembly.candidate.flye"},
            method_parameters={"assembly.candidate.flye": {"threads": 4}},
            stage_parameters={"assembly": {"unused": "value"}},
        )

        updated = merge_clarification_response(
            plan,
            AskClarificationResponse(
                kind="stage_selection", selected_stage_occurrences=[]
            ),
        )

        self.assertEqual(updated.selected_stage_occurrences, [])
        self.assertIsNone(updated.selected_methods)
        self.assertIsNone(updated.method_parameters)
        self.assertIsNone(updated.stage_parameters)
        self.assertEqual(
            next_clarification(updated, ask_plan_ref="/tmp/plan.json").kind,
            "confirm",
        )
        self.assertEqual(build_run_config_payload(updated)["requested_stages"], [])

    def test_method_selection_requires_selected_methods(self):
        with self.assertRaisesRegex(ValueError, "selected_methods"):
            AskClarificationResponse(kind="method_selection")

    def test_parameter_values_requires_parameter_payload(self):
        with self.assertRaisesRegex(
            ValueError, "method_parameters, shared_parameters, or stage_parameters"
        ):
            AskClarificationResponse(kind="parameter_values")

    def test_parameter_values_accepts_shared_parameters_only(self):
        response = AskClarificationResponse(
            kind="parameter_values",
            shared_parameters={
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
        )
        self.assertEqual(response.shared_parameters.read_type, "ont_raw")

    def test_confirm_requires_confirmed(self):
        with self.assertRaisesRegex(ValueError, "confirmed"):
            AskClarificationResponse(kind="confirm")

    def test_valid_stage_selection(self):
        response = AskClarificationResponse(
            kind="stage_selection",
            selected_stage_occurrences=[
                {"stage": "read_qc", "allow_local_execution": True},
                {"stage": "assembly", "allow_local_execution": True},
            ],
        )
        self.assertEqual(
            [occurrence.stage for occurrence in response.selected_stage_occurrences],
            ["read_qc", "assembly"],
        )

    def test_stage_occurrence_rejects_unknown_stage(self):
        with self.assertRaisesRegex(ValueError, "stage"):
            PlanStageOccurrence(
                stage="not_a_stage",
                allow_local_execution=False,
            )

    def test_plan_state_rejects_removed_execution_order_field(self):
        with self.assertRaisesRegex(ValueError, "resolved_execution_order"):
            _base_plan(resolved_execution_order=["assembly"])

    def test_plan_state_rejects_legacy_selected_stages_field(self):
        with self.assertRaisesRegex(ValueError, "selected_stages"):
            AskPlanState(
                session_id="s1",
                data_dir="data",
                entry_request={},
                manifest_ref="test.yaml",
                selected_stages=["assembly"],
            )


class AskPlannerStageSelectionTests(unittest.TestCase):
    def test_starts_with_stage_selection(self):
        plan = _base_plan()
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "stage_selection")
        self.assertEqual(result.status, "clarification_required")

    def test_intent_requested_stages_drive_stage_selection_default(self):
        plan = _base_plan(entry_request={"requested_stages": ["assembly"]})

        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertEqual(result.kind, "stage_selection")
        self.assertEqual(
            result.choices["suggested_stage_occurrences"],
            [{"stage": "assembly", "allow_local_execution": True}],
        )
        self.assertEqual(
            result.response_schema["selected_stage_occurrences"],
            [{"stage": "assembly", "allow_local_execution": True}],
        )

    def test_no_intent_or_manifest_recommendation_defaults_to_inventory_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_short_dna_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: illumina\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest), entry_request={})

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

            self.assertEqual(result.kind, "stage_selection")
            self.assertEqual(
                result.choices["available_stages"], _all_selectable_stages()
            )
            self.assertEqual(result.choices["suggested_stage_occurrences"], [])
            selected = merge_clarification_response(
                plan,
                AskClarificationResponse(
                    kind="stage_selection", selected_stage_occurrences=[]
                ),
            )
            self.assertEqual(
                next_clarification(selected, ask_plan_ref="/tmp/plan.json").kind,
                "confirm",
            )

    def test_stage_selection_keeps_requested_execution_boundary(self):
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
            response = AskClarificationResponse(
                kind="stage_selection",
                selected_stage_occurrences=[
                    {"stage": "assembly", "allow_local_execution": True}
                ],
            )
            updated = merge_clarification_response(plan, response)
            self.assertEqual(updated.selected_stage_names, ["assembly"])

    def test_stage_selection_keeps_custom_order_and_repeated_occurrences(self):
        plan = _base_plan()
        response = AskClarificationResponse(
            kind="stage_selection",
            selected_stage_occurrences=[
                {"stage": "assembly_review", "allow_local_execution": True},
                {"stage": "assembly", "allow_local_execution": True},
                {"stage": "assembly_review", "allow_local_execution": True},
            ],
        )

        updated = merge_clarification_response(plan, response)

        self.assertEqual(
            updated.selected_stage_names,
            ["assembly_review", "assembly", "assembly_review"],
        )

    def test_t2t_only_with_raw_reads_does_not_auto_add_assembly(self):
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
            response = AskClarificationResponse(
                kind="stage_selection",
                selected_stage_occurrences=[
                    {"stage": "t2t", "allow_local_execution": True}
                ],
            )
            updated = merge_clarification_response(plan, response)
            self.assertEqual(updated.selected_stage_names, ["t2t"])

    def test_plan_summary_includes_input_assets_from_manifest(self):
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
                entry_request={"command": "run", "manifest": str(manifest)},
            )

            summary = build_run_plan_summary(plan)

            self.assertEqual(summary["input_assets"][0]["asset_type"], "raw_long_reads")
            self.assertEqual(summary["input_assets"][0]["platform"], "ont")
            self.assertEqual(summary["input_assets"][0]["format"], "fastq")
            self.assertEqual(summary["input_assets"][0]["paths"], [str(reads)])

    def test_stage_plan_details_reuses_passed_manifest(self):
        import app.entrypoints.ask_planner as planner

        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest_path = Path(tmp) / "manifest.yaml"
            manifest_path.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest_path),
                entry_request={"command": "run", "manifest": str(manifest_path)},
                selected_methods={"assembly": "assembly.candidate.flye"},
            )
            manifest = planner._load_manifest(str(manifest_path))
            with patch.object(planner, "_load_manifest") as load_spy:
                planner._stage_plan_details(
                    plan,
                    parameter_defaults={},
                    missing_fields=[],
                    manifest=manifest,
                )
            load_spy.assert_not_called()


class BuildInitialPlanDraftTests(unittest.TestCase):
    def test_no_requested_stages_returns_none(self):
        plan = _base_plan()
        self.assertIsNone(build_initial_plan_draft(plan))

    def test_assembly_goal_keeps_requested_stage_only(self):
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
            self.assertEqual(draft.selected_stage_names, ["assembly"])
            self.assertEqual(
                draft.selected_methods["assembly"], "assembly.candidate.flye"
            )

    def test_pacbio_hifi_assembly_draft_writes_hifi_route_to_run_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\ngenome_size_estimate: 2000000\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: pacbio_hifi\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                entry_request={"requested_stages": ["assembly", "assembly_review"]},
            )

            draft = build_initial_plan_draft(plan)
            payload = build_run_config_payload(
                draft.model_copy(update={"confirmed": True})
            )

            self.assertEqual(
                draft.selected_methods["assembly"], "assembly.candidate.hifiasm"
            )
            self.assertEqual(payload["assembly"]["assembly_method"], "hifiasm")
            self.assertEqual(payload["assembly"]["assembly_route"], "hifi")

    def test_multi_stage_goal_keeps_requested_stages_only(self):
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

    def test_pipeline_draft_prefills_recommended_methods_and_default_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\ngenome_size_estimate: 2000000\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                entry_request={
                    "requested_stages": [
                        "read_qc",
                        "genome_survey",
                        "assembly",
                        "assembly_review",
                    ]
                },
            )

            draft = build_initial_plan_draft(plan)

            self.assertEqual(
                draft.selected_methods["read_qc"], _read_qc_default_methods()
            )
            self.assertEqual(
                draft.selected_methods["genome_survey"],
                [
                    "genome_survey.kmer_count.jellyfish",
                    "genome_survey.model.genomescope2",
                ],
            )
            self.assertEqual(
                draft.selected_methods["assembly"], "assembly.candidate.flye"
            )
            self.assertEqual(
                draft.selected_methods["assembly_review"],
                ["assembly_review.eval.quast"],
            )
            self.assertEqual(draft.shared_parameters.platform, "ont")
            self.assertEqual(draft.shared_parameters.read_type, "ont_raw")
            self.assertEqual(draft.shared_parameters.estimated_genome_size, 2_000_000)
            self.assertEqual(
                draft.method_parameters["genome_survey.kmer_count.jellyfish"][
                    "kmer_size"
                ],
                21,
            )
            self.assertEqual(
                draft.method_parameters["genome_survey.kmer_count.jellyfish"][
                    "hash_size"
                ],
                "1G",
            )
            self.assertEqual(
                draft.method_parameters["genome_survey.model.genomescope2"][
                    "kmer_size"
                ],
                21,
            )
            self.assertEqual(draft.method_parameters["assembly.candidate.flye"], {})
            self.assertEqual(
                draft.method_parameters["assembly_review.eval.quast"]["threads"], 4
            )

    def test_t2t_draft_keeps_method_parameter_block_empty(self):
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
                entry_request={"requested_stages": ["t2t"]},
            )

            draft = build_initial_plan_draft(plan)

            self.assertEqual(draft.selected_methods["t2t"], "t2t.alignment.minimap2")
            params = draft.method_parameters["t2t.alignment.minimap2"]
            self.assertEqual(params, {})


class AskPlannerMethodSelectionTests(unittest.TestCase):
    def test_stage_methods_match_descriptor_registry(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        catalog = build_planner_method_catalog(registry)
        for stage, methods in catalog.by_stage.items():
            with self.subTest(stage=stage):
                planner_methods = {m["method_name"] for m in methods}
                registered_methods = set(registry.by_stage.get(stage, []))
                self.assertEqual(planner_methods, registered_methods)

    def test_ask_planner_has_no_hardcoded_stage_method_registry(self):
        source = Path("app/entrypoints/ask_planner.py").read_text(encoding="utf-8")
        self.assertNotIn("_STAGE_METHODS", source)
        self.assertNotIn("_GENOME_SURVEY_EXECUTABLE_METHODS", source)

    def test_interactive_planner_uses_descriptor_derived_methods(self):
        source = Path("app/entrypoints/ask_interactive.py").read_text(encoding="utf-8")
        self.assertNotIn("_STAGE_METHODS", source)

    def test_method_selection_includes_non_pipeline_stages(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertIn("read_qc", result.choices["methods_by_stage"])
        self.assertIn("assembly", result.choices["methods_by_stage"])
        read_qc_defaults = result.choices["defaults"]["read_qc"]
        self.assertEqual(read_qc_defaults, _read_qc_default_methods())
        read_qc_methods = {
            m["method_name"]: m for m in result.choices["methods_by_stage"]["read_qc"]
        }
        self.assertEqual(
            read_qc_methods[READ_QC_FILTER_SKIP]["display_name"],
            "skip filtering, use raw reads",
        )
        flye = [
            m
            for m in result.choices["methods_by_stage"]["assembly"]
            if m["method_name"] == "assembly.candidate.flye"
        ]
        self.assertNotIn("execution_support", flye[0])

    def test_assembly_review_method_selection_uses_registered_method_names(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        method_names = {
            m["method_name"]
            for m in result.choices["methods_by_stage"]["assembly_review"]
        }
        self.assertIn("assembly_review.eval.quast", method_names)
        self.assertIn("assembly_review.eval.busco", method_names)
        self.assertNotIn("assembly_review.check.merqury", method_names)

    def test_assembly_dedup_is_default_auto_without_method_or_parameter_questions(self):
        plan = _base_plan(
            stage_names=["assembly_dedup"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")

    def test_read_qc_only_asks_for_filter_choice(self):
        plan = _base_plan(
            stage_names=["read_qc"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertEqual(
            result.choices["defaults"]["read_qc"], _read_qc_default_methods()
        )

    def test_empty_method_name_rejected(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
        )
        response = AskClarificationResponse(
            kind="method_selection", selected_methods={"assembly": ""}
        )
        with self.assertRaisesRegex(ValueError, "empty method name"):
            merge_clarification_response(plan, response)

    def test_unknown_method_name_rejected(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
        )
        response = AskClarificationResponse(
            kind="method_selection",
            selected_methods={"assembly": "assembly.candidate.unknown"},
        )
        with self.assertRaisesRegex(ValueError, "unknown method"):
            merge_clarification_response(plan, response)

    def test_method_name_must_belong_to_selected_stage(self):
        plan = _base_plan(
            stage_names=["genome_survey"],
        )
        response = AskClarificationResponse(
            kind="method_selection",
            selected_methods={"genome_survey": "assembly_review.eval.quast"},
        )
        with self.assertRaisesRegex(ValueError, "method is not valid"):
            merge_clarification_response(plan, response)

    def test_genome_survey_rejects_partial_executable_selection(self):
        plan = _base_plan(
            stage_names=["genome_survey"],
        )
        response = AskClarificationResponse(
            kind="method_selection",
            selected_methods={"genome_survey": ["genome_survey.kmer_count.jellyfish"]},
        )
        with self.assertRaisesRegex(ValueError, "requires both"):
            merge_clarification_response(plan, response)

    def test_read_qc_rejects_multiple_filter_choices(self):
        plan = _base_plan(
            stage_names=["read_qc"],
        )
        response = AskClarificationResponse(
            kind="method_selection",
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    READ_QC_FILTER_SKIP,
                    "read_qc.length_filter.seqkit_seq",
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "exactly one length filter"):
            merge_clarification_response(plan, response)


class AskPlannerParameterTests(unittest.TestCase):
    def test_flye_requests_shared_facts_without_requesting_threads(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "parameter_values")
        self.assertTrue(any("read_type" in f for f in result.required_fields))
        self.assertTrue(
            any("estimated_genome_size" in f for f in result.required_fields)
        )
        self.assertFalse(
            any(field.endswith(".threads") for field in result.required_fields)
        )

    def test_raw_short_read_executable_survey_defers_genome_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_reads = Path(tmp) / "long.fq"
            short_reads = Path(tmp) / "short.fq"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {long_reads}\n"
                "    format: fastq\n    platform: ont\n"
                "  - asset_type: raw_short_dna_reads\n"
                f"    paths:\n      - {short_reads}\n"
                "    format: fastq\n    platform: illumina\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                stage_names=["genome_survey", "assembly"],
                selected_methods={
                    "genome_survey": [
                        "genome_survey.kmer_count.jellyfish",
                        "genome_survey.model.genomescope2",
                    ],
                    "assembly": "assembly.candidate.flye",
                },
            )

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertFalse(
            any("estimated_genome_size" in field for field in result.required_fields)
        )
        self.assertEqual(
            result.choices["deferred_shared_parameters"]["estimated_genome_size"][
                "status"
            ],
            "awaiting_genome_survey",
        )

    def test_clean_short_read_executable_survey_defers_genome_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_reads = Path(tmp) / "long.fq"
            short_reads = Path(tmp) / "short.fq"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {long_reads}\n"
                "    format: fastq\n    platform: ont\n"
                "  - asset_type: clean_short_dna_reads\n"
                f"    paths:\n      - {short_reads}\n"
                "    format: fastq\n    platform: illumina\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                stage_names=["genome_survey", "assembly"],
                selected_methods={
                    "genome_survey": [
                        "genome_survey.kmer_count.jellyfish",
                        "genome_survey.model.genomescope2",
                    ],
                    "assembly": "assembly.candidate.flye",
                },
            )

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertFalse(
            any("estimated_genome_size" in field for field in result.required_fields)
        )
        self.assertEqual(
            result.choices["deferred_shared_parameters"]["estimated_genome_size"][
                "status"
            ],
            "awaiting_genome_survey",
        )

    def test_long_read_executable_survey_defers_genome_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_reads = Path(tmp) / "long.fq"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {long_reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                stage_names=["genome_survey", "assembly"],
                selected_methods={
                    "genome_survey": [
                        "genome_survey.kmer_count.jellyfish",
                        "genome_survey.model.genomescope2",
                    ],
                    "assembly": "assembly.candidate.flye",
                },
            )

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertFalse(
            any("estimated_genome_size" in field for field in result.required_fields)
        )
        self.assertEqual(
            result.choices["deferred_shared_parameters"]["estimated_genome_size"][
                "status"
            ],
            "awaiting_genome_survey",
        )

    def test_unexecuted_survey_still_requires_genome_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_reads = Path(tmp) / "long.fq"
            short_reads = Path(tmp) / "short.fq"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {long_reads}\n"
                "    format: fastq\n    platform: ont\n"
                "  - asset_type: clean_short_dna_reads\n"
                f"    paths:\n      - {short_reads}\n"
                "    format: fastq\n    platform: illumina\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                stage_names=["genome_survey", "assembly"],
                selected_methods={
                    "genome_survey": [
                        "genome_survey.kmer_count.meryl",
                        "genome_survey.model.smudgeplot",
                    ],
                    "assembly": "assembly.candidate.flye",
                },
            )

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertTrue(
            any("estimated_genome_size" in field for field in result.required_fields)
        )
        self.assertNotIn("deferred_shared_parameters", result.choices)

    def test_nonlocal_prior_survey_does_not_authorize_genome_size_defer(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_reads = Path(tmp) / "long.fq"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {long_reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                selected_stage_occurrences=[
                    PlanStageOccurrence(
                        stage="genome_survey",
                        allow_local_execution=False,
                    ),
                    PlanStageOccurrence(
                        stage="assembly",
                        allow_local_execution=True,
                    ),
                    PlanStageOccurrence(
                        stage="genome_survey",
                        allow_local_execution=True,
                    ),
                ],
                selected_methods={
                    "genome_survey": [
                        "genome_survey.kmer_count.jellyfish",
                        "genome_survey.model.genomescope2",
                    ],
                    "assembly": "assembly.candidate.flye",
                },
            )

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertTrue(
            any("estimated_genome_size" in field for field in result.required_fields)
        )
        self.assertNotIn("deferred_shared_parameters", result.choices)

    def test_manifest_platform_infers_defaults_shows_parameter_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: cycloneseq\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                stage_names=["read_qc", "assembly"],
                selected_methods={
                    "read_qc": _read_qc_default_methods(),
                    "assembly": "assembly.candidate.flye",
                },
            )
            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "parameter_values")
        defaults = result.choices.get("defaults", {})
        shared_defaults = result.choices.get("shared_defaults", {})
        flye_defaults = defaults.get("assembly.candidate.flye", {})
        self.assertIn("shared_parameters.platform", result.required_fields)
        self.assertNotIn("shared_parameters.read_type", result.required_fields)
        self.assertEqual(shared_defaults.get("platform"), "cycloneseq")
        self.assertEqual(shared_defaults.get("read_type"), "cyclone")
        self.assertEqual(shared_defaults.get("estimated_genome_size"), 2_000_000)
        self.assertEqual(flye_defaults, {})

    def test_unknown_manifest_platform_is_exposed_as_optional_shared_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: unknown\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                stage_names=["read_qc", "assembly"],
                selected_methods={
                    "read_qc": _read_qc_default_methods(),
                    "assembly": "assembly.candidate.flye",
                },
            )

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

            self.assertEqual(result.kind, "parameter_values")
            self.assertIn("shared_parameters.platform", result.required_fields)
            self.assertNotIn("shared_parameters.read_type", result.required_fields)
            self.assertEqual(result.choices["shared_defaults"]["platform"], "unknown")

    def test_confirmed_unknown_platform_falls_back_to_read_type_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: unknown\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                stage_names=["read_qc", "assembly"],
                selected_methods={
                    "read_qc": _read_qc_default_methods(),
                    "assembly": "assembly.candidate.flye",
                },
                shared_parameters={
                    "platform": "unknown",
                    "estimated_genome_size": 2_000_000,
                },
            )

            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

            self.assertEqual(result.kind, "parameter_values")
            self.assertIn("shared_parameters.read_type", result.required_fields)

    def test_parameter_response_derives_read_type_from_platform(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
        )
        response = AskClarificationResponse(
            kind="parameter_values",
            shared_parameters={
                "platform": "cycloneseq",
                "estimated_genome_size": 2_000_000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 4}},
        )

        updated = merge_clarification_response(plan, response)

        self.assertIsNotNone(updated.shared_parameters)
        self.assertEqual(updated.shared_parameters.platform, "cycloneseq")
        self.assertEqual(updated.shared_parameters.read_type, "cyclone")

    def test_genome_survey_jellyfish_does_not_request_read_length(self):
        plan = _base_plan(
            stage_names=["read_qc", "genome_survey"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "genome_survey": [
                    "genome_survey.kmer_count.jellyfish",
                    "genome_survey.model.genomescope2",
                ],
            },
        )

        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertEqual(result.kind, "parameter_values")
        self.assertIn(
            "genome_survey.kmer_count.jellyfish.kmer_size", result.required_fields
        )
        self.assertIn(
            "genome_survey.kmer_count.jellyfish.hash_size", result.required_fields
        )
        self.assertIn(
            "genome_survey.kmer_count.jellyfish.threads", result.required_fields
        )
        self.assertFalse(
            any(field.endswith(".read_length") for field in result.required_fields)
        )

    def test_genome_size_conflict_surfaces_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: cycloneseq\n",
                encoding="utf-8",
            )
            plan = _base_plan(
                manifest_ref=str(manifest),
                entry_request={
                    "command": "run",
                    "manifest": str(manifest),
                    "data_dir": "data",
                    "genome_size_estimate": 5000000,
                },
                stage_names=["read_qc", "assembly"],
                selected_methods={
                    "read_qc": _read_qc_default_methods(),
                    "assembly": "assembly.candidate.flye",
                },
            )
            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
            self.assertEqual(result.kind, "parameter_values")
            self.assertIn("warnings", result.choices)
            self.assertTrue(
                any("genome_size conflict" in w for w in result.choices["warnings"])
            )


class AskPlannerConfirmTests(unittest.TestCase):
    def test_confirm_step_after_parameters(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        self.assertEqual(result.status, "confirm_required")
        self.assertIn("run_config_preview", result.summary)
        self.assertEqual(
            result.summary["stage_roles"],
            ["executable", "executable"],
        )

    def test_confirm_response_completes_plan(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
        )
        response = AskClarificationResponse(kind="confirm", confirmed=True)
        updated = merge_clarification_response(plan, response)
        self.assertTrue(updated.confirmed)
        result = next_clarification(updated, ask_plan_ref="/tmp/plan.json")
        self.assertIsNone(result)

    def test_confirm_rejected_stays_unconfirmed(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
        )
        response = AskClarificationResponse(kind="confirm", confirmed=False)
        updated = merge_clarification_response(plan, response)
        self.assertFalse(updated.confirmed)

    def test_confirm_labels_only_selected_stage_occurrences(self):
        plan = _base_plan(
            stage_names=["assembly"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        self.assertEqual(result.summary["stage_roles"], ["executable"])


class AskPlannerRunConfigTests(unittest.TestCase):
    def test_build_run_config_from_confirmed_plan_with_advanced_flye_overrides(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly", "assembly_review"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(
            sorted(_local_execution_stage_names(payload)),
            ["assembly", "assembly_review", "read_qc"],
        )
        self.assertEqual(
            payload["review_policy"],
            {"max_blocked_seconds": 300},
        )
        self.assertEqual(payload["read_qc"]["length_filter"]["method"], "skip")
        self.assertEqual(payload["shared_parameters"]["read_type"], "ont_raw")
        self.assertNotIn("estimated_genome_size", payload["shared_parameters"])
        self.assertEqual(payload["assembly"]["flye_parameters"]["threads"], 8)

    def test_build_run_config_executes_selected_nextdenovo_method(self):
        plan = _base_plan(
            stage_names=["assembly", "assembly_review"],
            selected_methods={"assembly": "assembly.candidate.nextdenovo"},
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2000000,
            },
            method_parameters={"assembly.candidate.nextdenovo": {"threads": 8}},
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            _local_execution_stage_names(payload), ["assembly", "assembly_review"]
        )
        self.assertEqual(
            payload["assembly"],
            {
                "assembly_method": "nextdenovo",
                "nextdenovo_parameters": {
                    "threads": 8,
                },
            },
        )

    def test_build_run_config_executes_selected_hifiasm_method(self):
        plan = _base_plan(
            stage_names=["assembly", "assembly_review"],
            selected_methods={"assembly": "assembly.candidate.hifiasm"},
            shared_parameters={
                "platform": "pacbio_hifi",
                "read_type": "pacbio_hifi",
                "estimated_genome_size": 2000000,
            },
            confirmed=True,
        )
        plan = _auto_fill_inferred_defaults(plan)

        payload = build_run_config_payload(plan)

        self.assertEqual(
            _local_execution_stage_names(payload), ["assembly", "assembly_review"]
        )
        self.assertEqual(
            payload["assembly"],
            {
                "assembly_method": "hifiasm",
                "assembly_route": "hifi",
                "hifiasm_parameters": {},
            },
        )

    def test_build_run_config_executes_selected_hifiasm_ont_method(self):
        plan = _base_plan(
            stage_names=["assembly", "assembly_review"],
            selected_methods={"assembly": "assembly.candidate.hifiasm_ont"},
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2000000,
            },
            confirmed=True,
        )
        plan = _auto_fill_inferred_defaults(plan)

        payload = build_run_config_payload(plan)

        self.assertEqual(
            _local_execution_stage_names(payload), ["assembly", "assembly_review"]
        )
        self.assertEqual(
            payload["assembly"],
            {
                "assembly_method": "hifiasm_ont",
                "assembly_route": "ont",
                "hifiasm_ont_parameters": {},
            },
        )

    def test_build_run_config_rejects_multiple_executable_assembly_methods(self):
        plan = _base_plan(
            stage_names=["assembly"],
            selected_methods={
                "assembly": [
                    "assembly.candidate.flye",
                    "assembly.candidate.nextdenovo",
                ],
            },
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2000000,
            },
            method_parameters={
                "assembly.candidate.flye": {"threads": 8},
                "assembly.candidate.nextdenovo": {"threads": 8},
            },
            confirmed=True,
        )

        with self.assertRaisesRegex(ValueError, "exactly one executable"):
            build_run_config_payload(plan)

    def test_build_run_config_uses_shared_assembly_parameters(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly", "assembly_review"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "assembly": "assembly.candidate.flye",
            },
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2000000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(payload["shared_parameters"]["platform"], "ont")
        self.assertEqual(payload["shared_parameters"]["read_type"], "ont_raw")
        self.assertNotIn("estimated_genome_size", payload["shared_parameters"])
        self.assertEqual(
            payload["assembly"]["flye_parameters"],
            {"threads": 8},
        )

    def test_build_run_config_includes_assembly_dedup_as_locally_executable(self):
        plan = _base_plan(
            stage_names=["assembly_dedup"],
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(_local_execution_stage_names(payload), ["assembly_dedup"])
        self.assertEqual(_requested_stage_names(payload), ["assembly_dedup"])

    def test_build_run_config_preserves_selected_stage_occurrences_and_order(self):
        plan = _base_plan(
            stage_names=["assembly_dedup", "read_qc", "assembly_dedup"],
            selected_methods={"read_qc": _read_qc_default_methods()},
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            payload["requested_stages"],
            [
                {"stage": "assembly_dedup", "allow_local_execution": True},
                {"stage": "read_qc", "allow_local_execution": True},
                {"stage": "assembly_dedup", "allow_local_execution": True},
            ],
        )

    def test_build_run_config_preserves_per_occurrence_local_execution_consent(self):
        plan = _base_plan(
            selected_stage_occurrences=[
                PlanStageOccurrence(stage="t2t", allow_local_execution=False),
                PlanStageOccurrence(
                    stage="assembly_review", allow_local_execution=True
                ),
                PlanStageOccurrence(stage="t2t", allow_local_execution=True),
                PlanStageOccurrence(
                    stage="assembly_review", allow_local_execution=True
                ),
            ],
            selected_methods={
                "assembly_review": "assembly_review.eval.quast",
                "t2t": "t2t.alignment.minimap2",
            },
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            payload["requested_stages"],
            [
                {"stage": "t2t", "allow_local_execution": False},
                {"stage": "assembly_review", "allow_local_execution": True},
                {"stage": "t2t", "allow_local_execution": True},
                {"stage": "assembly_review", "allow_local_execution": True},
            ],
        )
        self.assertEqual(
            build_run_plan_summary(plan)["stage_roles"],
            ["dependency", "executable", "executable", "executable"],
        )

    def test_only_locally_enabled_occurrences_require_method_selection(self):
        plan = _base_plan(
            selected_stage_occurrences=[
                PlanStageOccurrence(stage="t2t", allow_local_execution=False)
            ]
        )

        clarification = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        self.assertEqual(clarification.kind, "confirm")

    def test_build_run_config_for_assembly_goal_uses_requested_boundary(self):
        plan = _base_plan(
            stage_names=["assembly", "assembly_review"],
            selected_methods={"assembly": "assembly.candidate.flye"},
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2000000,
            },
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            _requested_stage_names(payload), ["assembly", "assembly_review"]
        )
        self.assertEqual(
            _local_execution_stage_names(payload), ["assembly", "assembly_review"]
        )
        self.assertNotIn("read_qc", _requested_stage_names(payload))
        self.assertNotIn("base_polish", _requested_stage_names(payload))

    def test_build_run_config_excludes_t2t_recommendation_parameters(self):
        plan = _base_plan(
            stage_names=["t2t", "assembly_review"],
            selected_methods={"t2t": "t2t.alignment.minimap2"},
            method_parameters={
                "t2t.alignment.minimap2": {
                    "mode": "manual_review",
                    "telomere_motif": "TTAGGG",
                    "threshold_min_motif_copies": 5,
                    "threshold_terminal_window_bp": 12000,
                    "threshold_min_mapq": 30,
                    "threshold_max_target_terminal_distance_bp": 800,
                    "threshold_min_overlap_bp": 700,
                    "threshold_min_supporting_reads": 2,
                }
            },
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            _local_execution_stage_names(payload), ["t2t", "assembly_review"]
        )
        self.assertEqual(payload["t2t"]["mode"], "manual_review")
        self.assertNotIn("telomere_motif", payload["t2t"])
        self.assertNotIn("thresholds", payload["t2t"])

    def test_build_run_config_uses_selected_read_qc_filter(self):
        plan = _base_plan(
            stage_names=["read_qc"],
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    READ_QC_FILTER_SKIP,
                ]
            },
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(payload["read_qc"]["length_filter"]["method"], "skip")

    def test_build_run_config_uses_selected_seqkit_filter_threshold(self):
        plan = _base_plan(
            stage_names=["read_qc"],
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    READ_QC_FILTER_SEQKIT,
                ]
            },
            method_parameters={READ_QC_FILTER_SEQKIT: {"min_len": 1000}},
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(
            payload["read_qc"]["length_filter"],
            {
                "method": READ_QC_FILTER_SEQKIT,
                "approved": True,
                "thresholds": {"min_len": 1000},
            },
        )

    def test_build_run_config_includes_genome_survey_parameters_without_read_length(
        self,
    ):
        plan = _base_plan(
            stage_names=["read_qc", "genome_survey"],
            selected_methods={
                "read_qc": _read_qc_default_methods(),
                "genome_survey": [
                    "genome_survey.kmer_count.jellyfish",
                    "genome_survey.model.genomescope2",
                ],
            },
            method_parameters={
                "genome_survey.kmer_count.jellyfish": {
                    "kmer_size": 31,
                    "hash_size": "2G",
                    "threads": 8,
                }
            },
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            payload["genome_survey"]["survey_parameters"],
            {"kmer_size": 31, "hash_size": "2G", "threads": 8},
        )

    def test_build_run_config_rejects_seqkit_filter_without_threshold(self):
        plan = _base_plan(
            stage_names=["read_qc"],
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    READ_QC_FILTER_SEQKIT,
                ]
            },
            confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "min_len"):
            build_run_config_payload(plan)

    def test_build_run_config_uses_quast_parameters(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={"assembly_review": "assembly_review.eval.quast"},
            method_parameters={"assembly_review.eval.quast": {"threads": 4}},
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(payload["assembly_review"]["quast"], {"threads": 4})

    def test_build_run_config_quast_and_busco_multi_select(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": [
                    "assembly_review.eval.quast",
                    "assembly_review.eval.busco",
                ],
            },
            method_parameters={
                "assembly_review.eval.quast": {"threads": 4},
                "assembly_review.eval.busco": {
                    "lineage_path": "/data/busco/embryophyta_odb10",
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "2024-01-08",
                    "lineage_source": "local",
                    "threads": 16,
                },
            },
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertIn("assembly_review", _local_execution_stage_names(payload))
        self.assertEqual(payload["assembly_review"]["quast"], {"threads": 4})
        self.assertEqual(
            payload["assembly_review"]["busco"],
            {
                "lineage_path": "/data/busco/embryophyta_odb10",
                "lineage_name": "embryophyta_odb10",
                "lineage_version": "2024-01-08",
                "lineage_source": "local",
                "threads": 16,
            },
        )

    def test_build_run_config_raises_valueerror_for_partial_busco_params(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {"threads": 8},
            },
            confirmed=True,
        )
        with self.assertRaisesRegex(
            ValueError, r"assembly_review\.eval\.busco\.lineage_path is required"
        ):
            build_run_config_payload(plan)


class AskPlannerBuscoFlowTests(unittest.TestCase):
    def test_busco_appears_as_executable_in_method_choices(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        ar_methods = {
            m["method_name"]: m
            for m in result.choices["methods_by_stage"]["assembly_review"]
        }
        self.assertIn("assembly_review.eval.busco", ar_methods)
        self.assertIn("assembly_review.eval.quast", ar_methods)
        self.assertNotIn("execution_support", ar_methods["assembly_review.eval.busco"])
        self.assertNotIn("execution_support", ar_methods["assembly_review.eval.quast"])

    def test_busco_parameter_clarification_asks_for_all_fields(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": [
                    "assembly_review.eval.quast",
                    "assembly_review.eval.busco",
                ],
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "parameter_values")
        required = result.required_fields
        for field in (
            "lineage_path",
            "lineage_name",
            "lineage_version",
            "lineage_source",
        ):
            self.assertTrue(
                any(f"assembly_review.eval.busco.{field}" in f for f in required),
                f"Missing required field: assembly_review.eval.busco.{field}",
            )

    def test_missing_busco_fields_block_confirmation(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {"threads": 8},
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "parameter_values")
        self.assertTrue(
            any("lineage_path" in f for f in result.required_fields),
        )

    def test_build_run_config_payload_with_busco_after_auto_fill(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": [
                    "assembly_review.eval.quast",
                    "assembly_review.eval.busco",
                ],
            },
            method_parameters={
                "assembly_review.eval.quast": {"threads": 2},
                "assembly_review.eval.busco": {
                    "lineage_path": "/data/busco/lineage",
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "2024-01-08",
                    "lineage_source": "local",
                    "threads": 12,
                },
            },
            confirmed=True,
        )

        config = build_run_config_payload(_auto_fill_inferred_defaults(plan))

        self.assertIn("assembly_review", _local_execution_stage_names(config))
        self.assertEqual(config["assembly_review"]["quast"]["threads"], 2)
        self.assertEqual(
            config["assembly_review"]["busco"]["lineage_name"], "embryophyta_odb10"
        )

    def test_busco_lineage_path_warning_surfaces_in_plan(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {
                    "lineage_path": "/nonexistent/busco/lineage",
                    "lineage_name": "test_odb10",
                    "lineage_version": "2024-01-01",
                    "lineage_source": "local",
                    "threads": 8,
                },
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        self.assertIn("warnings", result.summary)
        self.assertTrue(
            any("does not exist" in w for w in result.summary["warnings"]),
        )

    def test_busco_defaults_derived_from_lineage_path(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {
                    "lineage_path": "/data/busco/insecta_odb10",
                },
            },
        )
        cap = {
            "required_parameters": [
                "lineage_path",
                "lineage_name",
                "lineage_version",
                "lineage_source",
                "threads",
            ],
            "parameter_defaults": {"threads": 8},
        }
        inferred, _ = _infer_parameter_defaults(plan, "assembly_review.eval.busco", cap)
        self.assertEqual(inferred["lineage_name"], "insecta_odb10")
        self.assertEqual(inferred["lineage_version"], "odb10")
        self.assertEqual(inferred["lineage_source"], "local")
        self.assertEqual(inferred["threads"], 8)

    def test_busco_defaults_no_version_when_undetectable(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {
                    "lineage_path": "/data/busco/custom_lineage",
                },
            },
        )
        cap = {
            "required_parameters": [
                "lineage_path",
                "lineage_name",
                "lineage_version",
                "lineage_source",
                "threads",
            ],
            "parameter_defaults": {"threads": 8},
        }
        inferred, _ = _infer_parameter_defaults(plan, "assembly_review.eval.busco", cap)
        self.assertEqual(inferred["lineage_name"], "custom_lineage")
        self.assertNotIn("lineage_version", inferred)
        self.assertEqual(inferred["lineage_source"], "local")

    def test_extract_busco_version_variants(self):
        self.assertEqual(_extract_busco_version("insecta_odb10"), "odb10")
        self.assertEqual(_extract_busco_version("vertebrata_odb11"), "odb11")
        self.assertEqual(_extract_busco_version("no_version_here"), "")


class AskPlannerStageAvailabilityTests(unittest.TestCase):
    def test_all_stages_available_regardless_of_manifest(self):
        available = _all_selectable_stages()
        expected = [
            "read_qc",
            "genome_survey",
            "assembly",
            "assembly_dedup",
            "assembly_review",
            "hic_scaffolding",
            "gapcloser",
            "t2t",
            "base_polish",
        ]
        self.assertEqual(available, expected)

    def test_all_stages_excludes_inventory(self):
        available = _all_selectable_stages()
        expected = [
            "read_qc",
            "genome_survey",
            "assembly",
            "assembly_dedup",
            "assembly_review",
            "hic_scaffolding",
            "gapcloser",
            "t2t",
            "base_polish",
        ]
        self.assertEqual(available, expected)

    def test_recommended_stages_for_raw_long_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths:\n      - {reads}\n"
                "    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            recommended = _recommended_stages_from_manifest(str(manifest))
            self.assertEqual(
                recommended[:5],
                [
                    "read_qc",
                    "genome_survey",
                    "assembly",
                    "assembly_dedup",
                    "assembly_review",
                ],
            )
            self.assertIn("read_qc", recommended)
            self.assertIn("assembly", recommended)
            self.assertIn("assembly_dedup", recommended)
            self.assertNotIn("gapcloser", recommended)
            self.assertNotIn("base_polish", recommended)

    def test_registered_assembly_recommendation_keeps_manifest_sensitive_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            assembly = Path(tmp) / "assembly.fasta"
            assembly.write_text(">contig\nACGT\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\nassets:\n"
                "  - asset_type: assembly_fasta_registered\n"
                f"    paths:\n      - {assembly}\n"
                "    format: fasta\n    platform: unknown\n",
                encoding="utf-8",
            )

            recommended = _recommended_stages_from_manifest(str(manifest))

            self.assertEqual(
                recommended,
                ["assembly_review", "t2t", "assembly_review"],
            )

    def test_recommended_stages_empty_for_missing_manifest(self):
        recommended = _recommended_stages_from_manifest("/nonexistent/manifest.yaml")
        self.assertEqual(recommended, [])


if __name__ == "__main__":
    unittest.main()
