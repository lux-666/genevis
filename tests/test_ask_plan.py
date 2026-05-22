"""Tests for ask plan schemas and staged planner."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.entrypoints.ask_planner import (
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


def _base_plan(**overrides) -> AskPlanState:
    base = dict(
        session_id="s1",
        data_dir="data",
        entry_request={"command": "run", "manifest": "test.yaml", "data_dir": "data"},
        manifest_ref="test.yaml",
    )
    base.update(overrides)
    return AskPlanState(**base)


class AskPlanSchemaTests(unittest.TestCase):
    def test_stage_selection_requires_selected_stages(self):
        with self.assertRaisesRegex(ValueError, "selected_stages"):
            AskClarificationResponse(kind="stage_selection")

    def test_method_selection_requires_selected_methods(self):
        with self.assertRaisesRegex(ValueError, "selected_methods"):
            AskClarificationResponse(kind="method_selection")

    def test_parameter_values_requires_method_parameters(self):
        with self.assertRaisesRegex(ValueError, "method_parameters"):
            AskClarificationResponse(kind="parameter_values")

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
    def test_method_selection_includes_all_stages(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertIn("assembly", result.choices["methods_by_stage"])
        self.assertIn("read_qc", result.choices["methods_by_stage"])
        flye = [m for m in result.choices["methods_by_stage"]["assembly"] if m["method_name"] == "assembly.candidate.flye"]
        self.assertEqual(flye[0]["execution_support"], "executable")

    def test_method_selection_for_single_stage(self):
        plan = _base_plan(
            selected_stages=["read_qc"],
            resolved_execution_order=["read_qc"],
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "method_selection")
        self.assertIn("read_qc", result.choices["methods_by_stage"])

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


class AskPlannerParameterTests(unittest.TestCase):
    def test_flye_parameters_requested(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"assembly": "assembly.candidate.flye"},
        )
        result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
        self.assertEqual(result.kind, "parameter_values")
        self.assertTrue(any("read_type" in f for f in result.required_fields))
        self.assertTrue(any("estimated_genome_size" in f for f in result.required_fields))

    def test_manifest_platform_infers_defaults_skips_to_confirm(self):
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
                selected_methods={"assembly": "assembly.candidate.flye"},
            )
            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
            self.assertEqual(result.kind, "confirm")
            preview = result.summary.get("run_config_preview", {})
            flye = preview.get("assembly", {}).get("flye_parameters", {})
            self.assertEqual(flye.get("read_type"), "nano-raw")
            self.assertEqual(flye.get("estimated_genome_size"), 2_000_000)

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
                selected_methods={"assembly": "assembly.candidate.flye"},
            )
            result = next_clarification(plan, ask_plan_ref="/tmp/plan.json")
            self.assertEqual(result.kind, "confirm")
            self.assertIn("warnings", result.summary)
            self.assertTrue(any("genome_size conflict" in w for w in result.summary["warnings"]))


class AskPlannerConfirmTests(unittest.TestCase):
    def test_confirm_step_after_parameters(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"assembly": "assembly.candidate.flye"},
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
            selected_methods={"assembly": "assembly.candidate.flye"},
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
            selected_methods={"assembly": "assembly.candidate.flye"},
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
            selected_methods={"assembly": "assembly.candidate.flye"},
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
    def test_build_run_config_from_confirmed_plan(self):
        plan = _base_plan(
            selected_stages=["read_qc", "assembly"],
            resolved_execution_order=["read_qc", "assembly"],
            selected_methods={"assembly": "assembly.candidate.flye"},
            method_parameters={
                "assembly.candidate.flye": {"read_type": "nano-raw", "estimated_genome_size": 2000000, "threads": 8}
            },
            confirmed=True,
        )
        payload = build_run_config_payload(plan)
        self.assertEqual(sorted(payload["local_exec"]), ["assembly", "read_qc"])
        self.assertEqual(payload["assembly"]["flye_parameters"]["read_type"], "nano-raw")
        self.assertEqual(payload["assembly"]["flye_parameters"]["estimated_genome_size"], 2000000)
        self.assertEqual(payload["assembly"]["flye_parameters"]["threads"], 8)


if __name__ == "__main__":
    unittest.main()
