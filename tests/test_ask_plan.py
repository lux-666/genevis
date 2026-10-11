"""Tests for ask plan schemas and staged planner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.entrypoints.ask_planner import (
    READ_QC_FILTER_SEQKIT,
    READ_QC_FILTER_SKIP,
    READ_QC_STATS_METHOD,
    _all_selectable_stages,
    _auto_fill_inferred_defaults,
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

    def test_complete_recommendation_disables_placeholder_local_execution(self):
        stages = [
            "read_qc",
            "genome_survey",
            "assembly",
            "assembly_dedup",
            "assembly_review",
            "hic_scaffolding",
            "gapcloser",
            "t2t",
            "base_polish",
            "assembly_review",
        ]
        plan = _base_plan(entry_request={"requested_stages": stages})

        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")

        consent = {
            item["stage"]: item["allow_local_execution"]
            for item in result.choices["suggested_stage_occurrences"]
        }
        self.assertTrue(consent["gapcloser"])
        self.assertTrue(consent["base_polish"])
        self.assertTrue(consent["t2t"])

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
            self.assertNotIn("assembly", draft.selected_methods or {})

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
                draft.model_copy(
                    update={
                        "confirmed": True,
                    }
                )
            )

            self.assertNotIn("assembly", draft.selected_methods or {})
            self.assertEqual(
                payload["assembly"],
                {
                    "method_route_advisor": "advisor",
                    "parameter_advisor": "advisor",
                },
            )

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
            self.assertNotIn("assembly", draft.selected_methods or {})
            self.assertEqual(
                draft.selected_methods["assembly_review"],
                [
                    "assembly_review.eval.quast",
                    "assembly_review.eval.busco",
                ],
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
            self.assertNotIn("assembly.candidate.flye", draft.method_parameters or {})
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

    def test_mixed_long_read_platforms_defer_shared_read_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ont = root / "ont.fastq"
            hifi = root / "hifi.fastq"
            ont.write_text("@ont\nACGT\n+\n!!!!\n", encoding="utf-8")
            hifi.write_text("@hifi\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = root / "manifest.yaml"
            manifest.write_text(
                "session_id: s1\ngenome_size_estimate: 2000000\nassets:\n"
                "  - asset_type: raw_long_reads\n"
                f"    paths: [{ont}]\n"
                "    format: fastq\n    platform: ont\n"
                "  - asset_type: clean_long_reads\n"
                f"    paths: [{hifi}]\n"
                "    format: fastq\n    platform: pacbio_hifi\n",
                encoding="utf-8",
            )

            draft = build_initial_plan_draft(
                _base_plan(
                    manifest_ref=str(manifest),
                    entry_request={"requested_stages": ["assembly"]},
                )
            )

            self.assertIsNotNone(draft.shared_parameters)
            self.assertIsNone(draft.shared_parameters.platform)
            self.assertIsNone(draft.shared_parameters.read_type)


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
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        self.assertNotIn("method_selection", result.choices)

    def test_assembly_review_method_selection_uses_registered_method_names(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        self.assertFalse(any("lineage_" in field for field in result.required_fields))
        self.assertNotIn("methods_by_stage", result.choices)

    def test_assembly_review_rejects_removed_method_selection_kind(self):
        plan = _base_plan(stage_names=["assembly_review"])

        with self.assertRaises(ValidationError):
            merge_clarification_response(
                plan,
                AskClarificationResponse(
                    kind="method_selection",
                    selected_methods={
                        "assembly_review": ["assembly_review.eval.quast"]
                    },
                ),
            )

    def test_assembly_dedup_fixed_method_needs_no_method_or_parameter_questions(self):
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
        self.assertEqual(result.kind, "confirm")

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
        self.assertEqual(result.kind, "confirm")

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
        self.assertEqual(result.kind, "confirm")

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
        self.assertEqual(result.kind, "confirm")

    def test_unexecuted_survey_fails_run_config_validation(self):
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

        self.assertFalse(
            any("estimated_genome_size" in field for field in result.required_fields)
        )
        self.assertNotIn("deferred_shared_parameters", result.choices)
        with self.assertRaisesRegex(
            ValueError, "assembly method selection is deferred"
        ):
            build_run_config_payload(plan.model_copy(update={"confirmed": True}))

    def test_nonlocal_prior_survey_fails_run_config_validation(self):
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

        self.assertFalse(
            any("estimated_genome_size" in field for field in result.required_fields)
        )
        self.assertNotIn("deferred_shared_parameters", result.choices)
        with self.assertRaisesRegex(
            ValueError, "assembly method selection is deferred"
        ):
            build_run_config_payload(plan.model_copy(update={"confirmed": True}))

    def test_manifest_platform_infers_defaults_without_parameter_step(self):
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
        self.assertEqual(result.kind, "confirm")
        self.assertNotIn("shared_parameters.read_type", result.required_fields)
        self.assertEqual(result.required_fields, ["confirmed"])

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

            self.assertEqual(result.kind, "confirm")
            self.assertNotIn("shared_parameters.read_type", result.required_fields)

    def test_unknown_platform_does_not_fall_back_to_read_type_prompt(self):
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

            self.assertEqual(result.kind, "confirm")
            self.assertNotIn("shared_parameters", result.choices or {})

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

        self.assertEqual(result.kind, "confirm")
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
            self.assertEqual(result.kind, "confirm")


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
    def test_build_run_config_keeps_deferred_assembly_out_of_payload(self):
        plan = _base_plan(
            stage_names=["read_qc", "assembly", "assembly_review"],
            selected_methods={"read_qc": _read_qc_default_methods()},
            shared_parameters={
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
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
        self.assertEqual(
            payload["read_qc"]["length_filter"],
            {"approved": False, "thresholds": {}},
        )
        self.assertEqual(payload["shared_parameters"]["read_type"], "ont_raw")
        self.assertNotIn("estimated_genome_size", payload["shared_parameters"])
        self.assertEqual(
            payload["assembly"],
            {
                "method_route_advisor": "advisor",
                "parameter_advisor": "advisor",
            },
        )

    def test_build_run_config_rejects_legacy_assembly_method_selection(self):
        plan = _base_plan(
            stage_names=["assembly", "assembly_review"],
            selected_methods={"assembly": "assembly.candidate.flye"},
            confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "method selection is deferred"):
            build_run_config_payload(plan)

    def test_build_run_config_projects_recommendation_policy(self):
        plan = _base_plan(
            stage_names=["assembly"],
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2_000_000,
            },
            confirmed=True,
        )
        plan = plan.model_copy(
            update={
                "entry_request": {
                    **plan.entry_request,
                    "recommendation_policy": {
                        "method_route_advisor": "deterministic_baseline",
                        "parameter_advisor": "deterministic_baseline",
                    },
                }
            }
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            payload["assembly"],
            {
                "method_route_advisor": "deterministic_baseline",
                "parameter_advisor": "deterministic_baseline",
            },
        )

    def test_build_run_config_rejects_legacy_assembly_method_parameters(self):
        plan = _base_plan(
            stage_names=["assembly"],
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
            confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "method parameters are deferred"):
            build_run_config_payload(plan)

    def test_build_run_config_includes_assembly_dedup_as_locally_executable(self):
        plan = _base_plan(
            stage_names=["assembly_dedup"],
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(_local_execution_stage_names(payload), ["assembly_dedup"])
        self.assertEqual(_requested_stage_names(payload), ["assembly_dedup"])
        self.assertNotIn("assembly_dedup", payload)

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
            shared_parameters={
                "platform": "ont",
                "read_type": "ont_raw",
                "estimated_genome_size": 2000000,
            },
            confirmed=True,
        )

        payload = build_run_config_payload(plan)

        self.assertEqual(
            _requested_stage_names(payload), ["assembly", "assembly_review"]
        )
        self.assertEqual(
            _local_execution_stage_names(payload), ["assembly", "assembly_review"]
        )
        self.assertEqual(
            payload["assembly"],
            {
                "method_route_advisor": "advisor",
                "parameter_advisor": "advisor",
            },
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
        self.assertEqual(
            payload["read_qc"]["length_filter"],
            {"approved": False, "thresholds": {}},
        )

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
            {"threads": 16},
        )

    def test_build_run_config_uses_host_database_with_default_busco_threads(self):
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
        payload = build_run_config_payload(plan)
        self.assertEqual(payload["assembly_review"]["busco"], {"threads": 8})


class AskPlannerBuscoFlowTests(unittest.TestCase):
    def test_review_pipeline_is_fixed_before_confirmation(self):
        plan = _base_plan(
            stage_names=["assembly_review"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        self.assertNotIn("methods_by_stage", result.choices)
        self.assertFalse(any("lineage_" in field for field in result.required_fields))

    def test_busco_confirmation_never_asks_for_database_fields(self):
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
        self.assertEqual(result.kind, "confirm")
        self.assertFalse(any("lineage_" in field for field in result.required_fields))

    def test_missing_busco_database_fields_do_not_block_confirmation(self):
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
        self.assertEqual(result.kind, "confirm")
        self.assertFalse(any("lineage_" in field for field in result.required_fields))

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
                    "threads": 12,
                },
            },
            confirmed=True,
        )

        config = build_run_config_payload(_auto_fill_inferred_defaults(plan))

        self.assertIn("assembly_review", _local_execution_stage_names(config))
        self.assertEqual(config["assembly_review"]["quast"]["threads"], 2)
        self.assertEqual(config["assembly_review"]["busco"], {"threads": 12})


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
            self.assertEqual(
                recommended,
                [
                    "read_qc",
                    "genome_survey",
                    "assembly",
                    "assembly_dedup",
                    "assembly_review",
                    "hic_scaffolding",
                    "gapcloser",
                    "t2t",
                    "base_polish",
                    "assembly_review",
                ],
            )

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
                [
                    "assembly_review",
                    "hic_scaffolding",
                    "gapcloser",
                    "t2t",
                    "base_polish",
                    "assembly_review",
                ],
            )

    def test_recommended_stages_empty_for_missing_manifest(self):
        recommended = _recommended_stages_from_manifest("/nonexistent/manifest.yaml")
        self.assertEqual(recommended, [])


if __name__ == "__main__":
    unittest.main()
