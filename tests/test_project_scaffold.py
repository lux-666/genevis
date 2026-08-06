import ast
import json
import unittest
from pathlib import Path


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
                if (
                    isinstance(node.right, ast.Constant)
                    and node.right.value in forbidden
                ):
                    violations.append(
                        f"{path.relative_to(root)}:{node.lineno}:{node.right.value}"
                    )
        self.assertEqual(
            violations, [], "stage code must use SessionPaths/stage path helpers"
        )

    def test_domain_stage_order_matches_long_read_pipeline(self):
        from app.workflow import STAGE_ORDER

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
                "gapcloser",
                "t2t",
                "base_polish",
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

    def test_removed_stage_planning_preflight_has_no_repository_references(self):
        root = Path(__file__).resolve().parents[1]
        stage_module = ".".join(("app", "stages", "preflight"))
        table_names = ("STAGE_" + "CONTRACTS", "STAGE_" + "REQUIREMENTS")

        self.assertFalse((root / "app" / "stages" / "preflight.py").exists())
        self.assertFalse((root / "tests" / "test_preflight.py").exists())

        tracked_sources = [
            *sorted((root / "app").rglob("*.py")),
            *sorted((root / "tests").rglob("*.py")),
            *sorted((root / "docs" / "architecture").rglob("*.md")),
        ]
        tracked_sources = [
            path for path in tracked_sources if path.name != "conformance.md"
        ]
        contents = "\n".join(
            path.read_text(encoding="utf-8") for path in tracked_sources
        )
        self.assertNotIn(stage_module, contents)
        for table_name in table_names:
            self.assertNotIn(table_name, contents)

    def test_scaffold_includes_architecture_graph_and_descriptor_roots(self):
        root = Path(__file__).resolve().parents[1]

        expected_paths = [
            root / "docs" / "architecture" / "README.md",
            root / "docs" / "architecture" / "topics" / "system-map.md",
            root / "docs" / "architecture" / "topics" / "runtime-orchestration.md",
            root / "docs" / "architecture" / "topics" / "tui-and-planning.md",
            root / "docs" / "architecture" / "topics" / "execution.md",
            root / "docs" / "architecture" / "topics" / "data-and-storage.md",
            root / "docs" / "architecture" / "topics" / "review-and-resume.md",
            root / "docs" / "architecture" / "topics" / "parameter-recommendation.md",
            root / "docs" / "architecture" / "topics" / "bootstrap.md",
            root / "docs" / "architecture" / "topics" / "autohic.md",
            root / "docs" / "architecture" / "topics" / "tui-midstage-entry.md",
            root / "docs" / "architecture" / "reference" / "data-contract.md",
            root / "docs" / "architecture" / "reference" / "resource-handoff.md",
            root / "docs" / "architecture" / "reference" / "execution-evidence.md",
            root / "docs" / "architecture" / "reference" / "execution-lifecycle.md",
            root
            / "docs"
            / "architecture"
            / "reference"
            / "parameter-recommendation.md",
            root / "docs" / "architecture" / "reference" / "stage-contracts.md",
            root / "docs" / "architecture" / "reference" / "descriptors.md",
            root / "docs" / "architecture" / "reference" / "governance.md",
            root / "docs" / "architecture" / "reference" / "conformance.md",
            root / "descriptors" / "registry.yaml",
        ]

        missing = [path for path in expected_paths if not path.exists()]
        self.assertEqual(missing, [])
