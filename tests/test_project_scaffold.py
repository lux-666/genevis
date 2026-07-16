import ast
import json
from pathlib import Path
import unittest


class ProjectScaffoldTests(unittest.TestCase):
    def test_committed_data_root_declares_v2_layout(self):
        root = Path(__file__).resolve().parents[1]
        marker = json.loads((root / "data" / "layout.json").read_text(encoding="utf-8"))
        self.assertEqual(marker, {"schema_version": 2})

    def test_stage_modules_do_not_reconstruct_storage_layer_roots(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = {"artifacts", "work", "reports", "ledger", "checkpoints"}
        violations: list[str] = []
        for path in sorted((root / "app" / "stages").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                    continue
                if isinstance(node.right, ast.Constant) and node.right.value in forbidden:
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:{node.right.value}")
        self.assertEqual(violations, [], "stage code must use SessionPaths/stage path helpers")

    def test_domain_stage_order_matches_long_read_pipeline(self):
        from app.stages import STAGE_ORDER

        self.assertEqual(
            STAGE_ORDER,
            [
                "inventory",
                "read_qc",
                "genome_survey",
                "assembly",
                "assembly_dedup",
                "assembly_review",
                "hic_scaffolding",
                "t2t",
                "repeat_annotation",
                "gene_annotation",
                "function_annotation",
            ],
        )

    def test_initial_state_uses_small_control_fields_and_asset_refs(self):
        from app.schemas.state import initial_state

        state = initial_state(
            session_id="smoke",
            input_manifest_ref="inputs/manifest.yaml",
            data_dir="data",
        )

        self.assertEqual(state["session_id"], "smoke")
        self.assertEqual(state["thread_id"], "smoke")
        self.assertEqual(state["current_stage"], "inventory")
        self.assertEqual(state["input_manifest_ref"], "inputs/manifest.yaml")
        self.assertIsNone(state["asset_registry_ref"])
        self.assertEqual(state["route_trace"], [])

    def test_scaffold_includes_contract_docs_and_descriptor_roots(self):
        root = Path(__file__).resolve().parents[1]

        expected_paths = [
            root / "docs" / "architecture.md",
            root / "docs" / "architecture" / "README.md",
            root / "docs" / "architecture" / "data-v2.md",
            root / "docs" / "descriptor_spec.md",
            root / "docs" / "stage_contracts.md",
            root / "docs" / "governance.md",
            root / "docs" / "handoff.md",
            root / "docs" / "phases" / "phase1" / "phase1.md",
            root / "docs" / "phases" / "phase1" / "task1.md",
            root / "docs" / "phases" / "phase1" / "task2.md",
            root / "docs" / "phases" / "phase1" / "task3.md",
            root / "docs" / "phases" / "phase1" / "task4.md",
            root / "docs" / "phases" / "phase1" / "task5.md",
            root / "docs" / "phases" / "phase_completion_plan.md",
            root / "descriptors" / "registry.yaml",
            root / "descriptors" / "assets" / "ont_clr_long_reads.yaml",
            root / "config" / "defaults.yaml",
            root / "config" / "resource_profiles.yaml",
        ]

        missing = [path for path in expected_paths if not path.exists()]
        self.assertEqual(missing, [])
