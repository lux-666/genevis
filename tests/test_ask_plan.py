"""Tests for ask plan schemas and staged planner."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.entrypoints.ask_planner import (
    READ_QC_FILTER_SKIP,
    READ_QC_FILTER_SEQKIT,
    READ_QC_HANDOFF_METHOD,
    READ_QC_STATS_METHOD,
    _STAGE_METHODS,
    _all_selectable_stages,
    _extract_busco_version,
    _infer_parameter_defaults,
    _plan_validation_warnings,
    _recommended_stages_from_manifest,
    build_run_config_payload,
    merge_clarification_response,
    next_clarification,
)
from app.schemas.ask_plan import (
    AskClarificationResponse,
    AskPlanState,
    load_ask_clarification_response,
    load_ask_plan,
    save_ask_plan,
)
from app.schemas.descriptors import DescriptorRegistry


def _base_plan(**overrides) -> AskPlanState:
    base = dict(
        session_id="s1",
        data_dir="data",
        entry_request={"command": "run", "manifest": "test.yaml", "data_dir": "data"},
        manifest_ref="test.yaml",
    )
    base.update(overrides)
    return AskPlanState(**base)


def _read_qc_default_methods() -> list[str]:
    return [READ_QC_STATS_METHOD, READ_QC_FILTER_SKIP, READ_QC_HANDOFF_METHOD]


class AskPlanSchemaTests(unittest.TestCase):
    def test_stage_selection_requires_selected_stages(self):
        with self.assertRaisesRegex(ValueError, "selected_stages"):
            AskClarificationResponse(kind="stage_selection")

    def test_method_selection_requires_selected_methods(self):
        with self.assertRaisesRegex(ValueError, "selected_methods"):
            AskClarificationResponse(kind="method_selection")

    def test_parameter_values_requires_parameter_payload(self):
        with self.assertRaisesRegex(ValueError, "method_parameters or shared_parameters"):
            AskClarificationResponse(kind="parameter_values")

    def test_parameter_values_accepts_shared_parameters_only(self):
        response = AskClarificationResponse(
            kind="parameter_values",
            shared_parameters={"read_type": "ont_raw", "estimated_genome_size": 2_000_000},
        )
        self.assertEqual(response.shared_parameters.read_type, "ont_raw")

    def test_confirm_requires_confirmed(self):
        with self.assertRaisesRegex(ValueError, "confirmed"):
            AskClarificationResponse(kind="confirm")

    def test_valid_stage_selection(self):
        response = AskClarificationResponse(kind="stage_selection", selected_stages=["read_qc", "assembly"])
        self.assertEqual(response.selected_stages, ["read_qc", "assembly"])

    def test_round_trip_save_load(self):
        plan = _base_plan()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            save_ask_plan(plan, path)
            loaded = load_ask_plan(path)
            self.assertEqual(loaded.session_id, "s1")
            self.assertIsNone(loaded.selected_stages)

    def test_load_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_ask_plan("/nonexistent/ask_plan.json")

    def test_load_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                load_ask_plan(path)

    def test_load_response_validates_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "response.json"
            path.write_text(json.dumps({"kind": "stage_selection"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "selected_stages"):
                load_ask_clarification_response(path)


class AskPlannerStageSelectionTests(unittest.TestCase):
    def test_starts_with_stage_selection(self):
        plan = _base_plan()
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertIsNotNone(result)
        self.assertEqual(result.kind, "stage_selection")
        self.assertEqual(result.status, "clarification_required")

    def test_assembly_without_read_qc_adds_read_qc_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            plan = _base_plan(manifest_ref=str(manifest))
            response = AskClarificationResponse(kind="stage_selection", selected_stages=["assembly"])
            updated = merge_clarification_response(plan, response)
            self.assertIn("read_qc", updated.resolved_execution_order)
            self.assertIn("assembly", updated.resolved_execution_order)
            order = updated.resolved_execution_order
            self.assertLess(order.index("read_qc"), order.index("assembly"))


class AskPlannerMethodSelectionTests(unittest.TestCase):
    def test_stage_methods_match_descriptor_registry(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        for stage, methods in _STAGE_METHODS.items():
            with self.subTest(stage=stage):
                planner_methods = {
                    m["method_name"]
                    for m in methods
                    if not str(m["method_name"]).endswith(".skip")
                }
                registered_methods = set(registry.by_stage.get(stage, []))
                self.assertEqual(planner_methods, registered_methods)

    def test_method_selection_includes_non_pipeline_stages(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertIn("read_qc", result.choices["methods_by_stage"])
        self.assertIn("assembly", result.choices["methods_by_stage"])
        read_qc_defaults = result.choices["defaults"]["read_qc"]
        self.assertEqual(read_qc_defaults, _read_qc_default_methods())
        read_qc_methods = {
            m["method_name"]: m
            for m in result.choices["methods_by_stage"]["read_qc"]
        }
        self.assertEqual(read_qc_methods[READ_QC_FILTER_SKIP]["display_name"], "skip filtering, use raw reads")
        flye = [m for m in result.choices["methods_by_stage"]["assembly"] if m["method_name"] == "assembly.candidate.flye"]
        self.assertEqual(flye[0]["execution_support"], "executable")

    def test_assembly_review_method_selection_uses_registered_method_names(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        method_names = {
            m["method_name"]
            for m in result.choices["methods_by_stage"]["assembly_review"]
        }
        self.assertIn("assembly_review.eval.merqury", method_names)
        self.assertIn("assembly_review.eval.mapping_coverage", method_names)
        self.assertNotIn("assembly_review.check.merqury", method_names)

    def test_read_qc_only_asks_for_filter_choice(self):
        plan = _base_plan(
            selected_stages=["read_qc"],
            resolved_execution_order=["read_qc"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertEqual(result.choices["defaults"]["read_qc"], _read_qc_default_methods())

    def test_empty_method_name_rejected(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
        )
        response = AskClarificationResponse(kind="method_selection", selected_methods={"assembly": ""})
        with self.assertRaisesRegex(ValueError, "empty method name"):
            merge_clarification_response(plan, response)

    def test_unknown_method_name_rejected(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
        )
        response = AskClarificationResponse(kind="method_selection", selected_methods={"assembly": "assembly.candidate.unknown"})
        with self.assertRaisesRegex(ValueError, "unknown method"):
            merge_clarification_response(plan, response)

    def test_read_qc_rejects_multiple_filter_choices(self):
        plan = _base_plan(
            selected_stages=["read_qc"],
            resolved_execution_order=["read_qc"],
        )
        response = AskClarificationResponse(
            kind="method_selection",
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    READ_QC_FILTER_SKIP,
                    "read_qc.length_filter.seqkit_seq",
                    READ_QC_HANDOFF_METHOD,
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "exactly one length filter"):
            merge_clarification_response(plan, response)


class AskPlannerParameterTests(unittest.TestCase):
    def test_flye_parameters_requested(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "parameter_values")
        self.assertTrue(any("read_type" in f for f in result.required_fields))
        self.assertTrue(any("estimated_genome_size" in f for f in result.required_fields))

    def test_manifest_platform_infers_defaults_shows_parameter_step(self):
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
                selected_stages=["read_qc", "assembly"],
                resolved_execution_order=["read_qc", "assembly"],
                selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            )
            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
            self.assertEqual(result.kind, "parameter_values")
            defaults = result.choices.get("defaults", {})
            shared_defaults = result.choices.get("shared_defaults", {})
            flye_defaults = defaults.get("assembly.candidate.flye", {})
            self.assertEqual(shared_defaults.get("read_type"), "ont_raw")
            self.assertEqual(shared_defaults.get("estimated_genome_size"), 2_000_000)
            self.assertEqual(flye_defaults.get("threads"), 4)

    def test_genome_size_conflict_surfaces_warning(self):
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
                entry_request={"command": "run", "manifest": str(manifest), "data_dir": "data", "genome_size_estimate": 5000000},
                selected_stages=["read_qc", "assembly"],
                resolved_execution_order=["read_qc", "assembly"],
                selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            )
            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
            self.assertEqual(result.kind, "parameter_values")
            self.assertIn("warnings", result.choices)
            self.assertTrue(any("genome_size conflict" in w for w in result.choices["warnings"]))


class AskPlannerConfirmTests(unittest.TestCase):
    def test_confirm_step_after_parameters(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            method_parameters={
                "assembly.candidate.flye": {"read_type": "nano-raw", "estimated_genome_size": 2000000, "threads": 8}
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        self.assertEqual(result.status, "confirm_required")
        self.assertIn("resolved_execution_order", result.summary)
        self.assertIn("run_config_preview", result.summary)
        self.assertIn("stage_roles", result.summary)
        self.assertEqual(result.summary["stage_roles"]["read_qc"], "executable")
        self.assertEqual(result.summary["stage_roles"]["assembly"], "executable")

    def test_confirm_response_completes_plan(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            method_parameters={
                "assembly.candidate.flye": {"read_type": "nano-raw", "estimated_genome_size": 2000000, "threads": 8}
            },
        )
        response = AskClarificationResponse(kind="confirm", confirmed=True)
        updated = merge_clarification_response(plan, response)
        self.assertTrue(updated.confirmed)
        result = next_clarification(updated, ask_plan_ref="/tmp/plan.json")
        self.assertIsNone(result)

    def test_confirm_rejected_stays_unconfirmed(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            method_parameters={
                "assembly.candidate.flye": {"read_type": "nano-raw", "estimated_genome_size": 2000000, "threads": 8}
            },
        )
        response = AskClarificationResponse(kind="confirm", confirmed=False)
        updated = merge_clarification_response(plan, response)
        self.assertFalse(updated.confirmed)

    def test_confirm_labels_dependency_stage(self):
        plan = _base_plan(
            selected_stages=["assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            method_parameters={
                "assembly.candidate.flye": {"read_type": "nano-raw", "estimated_genome_size": 2000000, "threads": 8}
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "confirm")
        roles = result.summary["stage_roles"]
        self.assertEqual(roles["read_qc"], "dependency")
        self.assertEqual(roles["assembly"], "executable")


class AskPlannerRunConfigTests(unittest.TestCase):
    def test_build_run_config_from_confirmed_plan_with_advanced_flye_overrides(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            method_parameters={
                "assembly.candidate.flye": {"read_type": "nano-raw", "estimated_genome_size": 2000000, "threads": 8}
            },
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(sorted(payload["local_exec"]), ["assembly", "read_qc"])
        self.assertEqual(payload["read_qc"]["length_filter"]["method"], "skip")
        self.assertEqual(payload["assembly"]["flye_parameters"]["read_type"], "nano-raw")
        self.assertEqual(payload["assembly"]["flye_parameters"]["estimated_genome_size"], 2000000)
        self.assertEqual(payload["assembly"]["flye_parameters"]["threads"], 8)

    def test_build_run_config_uses_shared_assembly_parameters(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"read_qc": _read_qc_default_methods(), "assembly": "assembly.candidate.flye"},
            shared_parameters={"read_type": "ont_raw", "estimated_genome_size": 2000000},
            method_parameters={"assembly.candidate.flye": {"threads": 8}},
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(payload["shared_parameters"]["read_type"], "ont_raw")
        self.assertEqual(payload["shared_parameters"]["estimated_genome_size"], 2000000)
        self.assertEqual(payload["assembly"]["flye_parameters"], {"threads": 8, "read_type": "nano-raw", "estimated_genome_size": 2000000})

    def test_build_run_config_uses_selected_read_qc_filter(self):
        plan = _base_plan(
            selected_stages=["read_qc"],
            resolved_execution_order=["read_qc"],
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    "read_qc.length_filter.filtlong",
                    READ_QC_HANDOFF_METHOD,
                ]
            },
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(payload["read_qc"]["length_filter"]["method"], "read_qc.length_filter.filtlong")

    def test_build_run_config_uses_selected_seqkit_filter_threshold(self):
        plan = _base_plan(
            selected_stages=["read_qc"],
            resolved_execution_order=["read_qc"],
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    READ_QC_FILTER_SEQKIT,
                    READ_QC_HANDOFF_METHOD,
                ]
            },
            method_parameters={READ_QC_FILTER_SEQKIT: {"min_len": 1000}},
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(
            payload["read_qc"]["length_filter"],
            {"method": READ_QC_FILTER_SEQKIT, "approved": True, "thresholds": {"min_len": 1000}},
        )

    def test_build_run_config_rejects_seqkit_filter_without_threshold(self):
        plan = _base_plan(
            selected_stages=["read_qc"],
            resolved_execution_order=["read_qc"],
            selected_methods={
                "read_qc": [
                    READ_QC_STATS_METHOD,
                    READ_QC_FILTER_SEQKIT,
                    READ_QC_HANDOFF_METHOD,
                ]
            },
            confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, "min_len"):
            build_run_config_payload(plan)

    def test_build_run_config_uses_quast_parameters(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
            selected_methods={"assembly_review": "assembly_review.eval.quast"},
            method_parameters={"assembly_review.eval.quast": {"threads": 4}},
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(payload["assembly_review"]["quast"], {"threads": 4})

    def test_build_run_config_quast_and_busco_multi_select(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.quast", "assembly_review.eval.busco"],
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
        self.assertIn("assembly_review", payload["local_exec"])
        self.assertEqual(payload["assembly_review"]["quast"], {"threads": 4})
        self.assertEqual(payload["assembly_review"]["busco"], {
            "lineage_path": "/data/busco/embryophyta_odb10",
            "lineage_name": "embryophyta_odb10",
            "lineage_version": "2024-01-08",
            "lineage_source": "local",
            "threads": 16,
        })

    def test_build_run_config_raises_valueerror_for_partial_busco_params(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {"threads": 8},
            },
            confirmed=True,
        )
        with self.assertRaisesRegex(ValueError, r"assembly_review\.eval\.busco\.lineage_path is required"):
            build_run_config_payload(plan)


class AskPlannerBuscoFlowTests(unittest.TestCase):
    def test_busco_appears_as_executable_in_method_choices(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        ar_methods = {
            m["method_name"]: m
            for m in result.choices["methods_by_stage"]["assembly_review"]
        }
        self.assertEqual(ar_methods["assembly_review.eval.busco"]["execution_support"], "executable")
        self.assertEqual(ar_methods["assembly_review.eval.quast"]["execution_support"], "executable")

    def test_busco_parameter_clarification_asks_for_all_fields(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.quast", "assembly_review.eval.busco"],
            },
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "parameter_values")
        required = result.required_fields
        for field in ("lineage_path", "lineage_name", "lineage_version", "lineage_source"):
            self.assertTrue(
                any(f"assembly_review.eval.busco.{field}" in f for f in required),
                f"Missing required field: assembly_review.eval.busco.{field}",
            )

    def test_missing_busco_fields_block_confirmation(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
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

    def test_write_ask_run_config_with_busco_after_auto_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = _base_plan(
                data_dir=tmp,
                selected_stages=["assembly_review"],
                resolved_execution_order=["assembly_review"],
                selected_methods={
                    "assembly_review": ["assembly_review.eval.quast", "assembly_review.eval.busco"],
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
            from app.entrypoints.ask_planner import write_ask_run_config
            path = write_ask_run_config(plan)
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("assembly_review", config["local_exec"])
            self.assertEqual(config["assembly_review"]["quast"]["threads"], 2)
            self.assertEqual(config["assembly_review"]["busco"]["lineage_name"], "embryophyta_odb10")

    def test_busco_lineage_path_warning_surfaces_in_plan(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
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
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {
                    "lineage_path": "/data/busco/insecta_odb10",
                },
            },
        )
        cap = {"required_parameters": ["lineage_path", "lineage_name", "lineage_version", "lineage_source", "threads"], "parameter_defaults": {"threads": 8}, "execution_support": "executable"}
        inferred, _ = _infer_parameter_defaults(plan, "assembly_review.eval.busco", cap)
        self.assertEqual(inferred["lineage_name"], "insecta_odb10")
        self.assertEqual(inferred["lineage_version"], "odb10")
        self.assertEqual(inferred["lineage_source"], "local")
        self.assertEqual(inferred["threads"], 8)

    def test_busco_defaults_no_version_when_undetectable(self):
        plan = _base_plan(
            selected_stages=["assembly_review"],
            resolved_execution_order=["assembly_review"],
            selected_methods={
                "assembly_review": ["assembly_review.eval.busco"],
            },
            method_parameters={
                "assembly_review.eval.busco": {
                    "lineage_path": "/data/busco/custom_lineage",
                },
            },
        )
        cap = {"required_parameters": ["lineage_path", "lineage_name", "lineage_version", "lineage_source", "threads"], "parameter_defaults": {"threads": 8}, "execution_support": "executable"}
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
            "read_qc", "genome_survey", "assembly", "assembly_review",
            "hic_scaffolding", "repeat_annotation", "gene_annotation", "function_annotation",
        ]
        self.assertEqual(available, expected)

    def test_all_stages_excludes_inventory(self):
        available = _all_selectable_stages()
        expected = [
            "read_qc", "genome_survey", "assembly", "assembly_review",
            "hic_scaffolding", "repeat_annotation", "gene_annotation", "function_annotation",
        ]
        self.assertEqual(available, expected)

    def test_recommended_stages_for_raw_long_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            reads = Path(tmp) / "reads.fq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: raw_long_reads\n    paths:\n      - {reads}\n    format: fastq\n    platform: ont\n",
                encoding="utf-8",
            )
            recommended = _recommended_stages_from_manifest(str(manifest))
            self.assertIn("read_qc", recommended)
            self.assertIn("assembly", recommended)
            self.assertIn("repeat_annotation", recommended)
            self.assertIn("gene_annotation", recommended)
            self.assertIn("function_annotation", recommended)

    def test_recommended_stages_for_gene_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            gff = Path(tmp) / "genes.gff3"
            gff.write_text("##gff-version 3\n", encoding="utf-8")
            manifest = Path(tmp) / "manifest.yaml"
            manifest.write_text(
                f"session_id: s1\nassets:\n  - asset_type: gene_models\n    paths:\n      - {gff}\n    format: gff3\n",
                encoding="utf-8",
            )
            recommended = _recommended_stages_from_manifest(str(manifest))
            self.assertIn("gene_annotation", recommended)
            self.assertIn("function_annotation", recommended)

    def test_recommended_stages_empty_for_missing_manifest(self):
        recommended = _recommended_stages_from_manifest("/nonexistent/manifest.yaml")
        self.assertEqual(recommended, [])


class AskPlannerAnnotationMethodTests(unittest.TestCase):
    def test_gene_annotation_method_selection(self):
        plan = _base_plan(
            selected_stages=["gene_annotation"],
            resolved_execution_order=["gene_annotation"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertIn("gene_annotation", result.choices["methods_by_stage"])
        methods = {m["method_name"] for m in result.choices["methods_by_stage"]["gene_annotation"]}
        self.assertIn("gene_annotation.candidate.braker3", methods)
        self.assertIn("gene_annotation.helper.hisat2", methods)
        for m in result.choices["methods_by_stage"]["gene_annotation"]:
            if m["method_name"] == "gene_annotation.candidate.miniprot":
                self.assertEqual(m["execution_support"], "executable")
            else:
                self.assertEqual(m["execution_support"], "planned_only")

    def test_function_annotation_method_selection(self):
        plan = _base_plan(
            selected_stages=["function_annotation"],
            resolved_execution_order=["function_annotation"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertIn("function_annotation", result.choices["methods_by_stage"])
        defaults = result.choices["defaults"]["function_annotation"]
        self.assertIn("function_annotation.candidate.interproscan", defaults)
        self.assertIn("function_annotation.candidate.eggnog_mapper", defaults)

    def test_hic_scaffolding_method_selection(self):
        plan = _base_plan(
            selected_stages=["hic_scaffolding"],
            resolved_execution_order=["hic_scaffolding"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        methods = result.choices["methods_by_stage"]["hic_scaffolding"]
        yahs = next(m for m in methods if m["method_name"] == "hic_scaffolding.candidate.yahs")
        self.assertEqual(yahs["execution_support"], "executable")
        self.assertEqual(result.choices["defaults"]["hic_scaffolding"], "hic_scaffolding.candidate.yahs")

    def test_repeat_annotation_method_selection(self):
        plan = _base_plan(
            selected_stages=["repeat_annotation"],
            resolved_execution_order=["repeat_annotation"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        methods = {m["method_name"] for m in result.choices["methods_by_stage"]["repeat_annotation"]}
        self.assertIn("repeat_annotation.masking.repeatmasker", methods)
        self.assertIn("repeat_annotation.helper.trf", methods)

    def test_no_function_annotation_in_run_config(self):
        plan = _base_plan(
            selected_stages=["function_annotation"],
            resolved_execution_order=["function_annotation"],
            selected_methods={
                "function_annotation": ["function_annotation.candidate.interproscan"],
            },
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertNotIn("function_annotation", payload)
        self.assertNotIn("function_annotation", payload.get("local_exec", []))


class AskPlannerValidationWarningTests(unittest.TestCase):
    def test_function_annotation_without_protein_input_warns(self):
        plan = _base_plan(
            selected_stages=["function_annotation"],
            resolved_execution_order=["function_annotation"],
            selected_methods={
                "function_annotation": ["function_annotation.candidate.interproscan"],
            },
        )
        warnings = _plan_validation_warnings(plan)
        self.assertTrue(any("function_annotation" in w and "protein input" in w for w in warnings))

    def test_function_annotation_with_gene_annotation_no_warning(self):
        plan = _base_plan(
            selected_stages=["gene_annotation", "function_annotation"],
            resolved_execution_order=["gene_annotation", "function_annotation"],
            selected_methods={
                "gene_annotation": ["gene_annotation.candidate.braker3"],
                "function_annotation": ["function_annotation.candidate.interproscan"],
            },
        )
        warnings = _plan_validation_warnings(plan)
        self.assertFalse(any("protein input" in w for w in warnings))

    def test_planned_only_stages_warned(self):
        plan = _base_plan(
            selected_stages=["gene_annotation"],
            resolved_execution_order=["gene_annotation"],
            selected_methods={
                "gene_annotation": ["gene_annotation.candidate.braker3"],
            },
        )
        warnings = _plan_validation_warnings(plan)
        self.assertTrue(any("planned_only" in w and "gene_annotation" in w for w in warnings))

    def test_hic_without_reads_warns(self):
        plan = _base_plan(
            selected_stages=["hic_scaffolding"],
            resolved_execution_order=["hic_scaffolding"],
            selected_methods={
                "hic_scaffolding": "hic_scaffolding.candidate.yahs",
            },
        )
        warnings = _plan_validation_warnings(plan)
        self.assertTrue(any("hic_scaffolding" in w and "Hi-C reads" in w for w in warnings))

    def test_method_execution_status_in_summary(self):
        plan = _base_plan(
            selected_stages=["hic_scaffolding", "gene_annotation"],
            resolved_execution_order=["hic_scaffolding", "gene_annotation"],
            selected_methods={
                "hic_scaffolding": "hic_scaffolding.candidate.yahs",
                "gene_annotation": ["gene_annotation.candidate.braker3"],
            },
            method_parameters={
                "hic_scaffolding.candidate.yahs": {"threads": 8},
            },
            confirmed=True,
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertIsNone(result)
        from app.entrypoints.ask_planner import build_run_plan_summary
        summary = build_run_plan_summary(plan)
        status = summary["method_execution_status"]
        self.assertEqual(status["hic_scaffolding.candidate.yahs"], "executable")
        self.assertEqual(status["gene_annotation.candidate.braker3"], "planned_only")


if __name__ == "__main__":
    unittest.main()
