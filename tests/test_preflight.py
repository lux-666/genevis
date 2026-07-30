"""Tests for Phase 8.5 Preflight API.

Tests the validation API used by TUI for stage planning.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.schemas.artifacts import ArtifactMeta
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import initial_state
from app.stages.preflight import (
    check_stage_prerequisites,
    validate_execution_plan,
)
from app.storage.artifacts import publish_artifact_record
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class PreflightCheckTests(unittest.TestCase):
    """Tests for check_stage_prerequisites()."""

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        """Create a minimal asset registry JSON file."""
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _registry_ref(self, data_dir: str, session_id: str, registry_path: Path) -> dict:
        paths = SessionPaths.from_run_dir(data_dir)
        source = paths.work_path("inventory", "asset_registry.json")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(registry_path.read_text(encoding="utf-8"), encoding="utf-8")
        artifact = ArtifactMeta.from_file(
            source,
            stage="inventory",
            method_name="inventory.register_assets",
            artifact_type="asset_registry",
        )
        published = publish_artifact_record(data_dir, session_id, artifact)
        return published.ref.model_dump(mode="json")

    def test_check_stage_prerequisites_missing_assembly(self):
        """测试缺少 assembly 时的检查"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create registry with no assembly asset
            registry = self._write_registry(tmpdir, assets=[])

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}  # No upstream outputs

            check = check_stage_prerequisites(state, "hic_scaffolding")

            self.assertFalse(check.can_run)
            self.assertEqual(len(check.missing), 1)
            self.assertEqual(check.missing[0].resource_name, "assembly")
            self.assertGreater(len(check.suggestions), 0)
            self.assertIn("assembly_fasta_registered", check.suggestions[0])

    def test_check_stage_prerequisites_with_assembly_from_registry(self):
        """Test stage check passes when assembly is in asset registry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an actual assembly file
            assembly_path = Path(tmpdir) / "assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            # Create registry with assembly asset
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:assembly:1",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_path),
                    }
                ],
            )

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            check = check_stage_prerequisites(state, "hic_scaffolding")

            self.assertTrue(check.can_run)
            self.assertEqual(len(check.missing), 0)
            self.assertEqual(len(check.suggestions), 0)

    def test_check_stage_prerequisites_with_assembly_from_upstream_stage(self):
        """Test stage check passes when assembly comes from upstream stage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an actual assembly file
            assembly_path = Path(tmpdir) / "final_assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            # Create registry without assembly (will use stage output)
            registry = self._write_registry(tmpdir, assets=[])

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {"assembly_review": {"status": "completed"}}
            state["resource_refs"]["assembly"] = ResourceRef(
                resource_type="assembly",
                paths=[str(assembly_path)],
                source_stage="assembly_review",
            ).to_dict()

            check = check_stage_prerequisites(state, "hic_scaffolding")

            self.assertTrue(check.can_run)
            self.assertEqual(len(check.missing), 0)

    def test_gene_annotation_requires_softmask_without_fallback_reason(self):
        """Gene annotation should not pass with only an unmasked registered assembly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_path = Path(tmpdir) / "assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:assembly:1",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_path),
                    }
                ],
            )

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            check = check_stage_prerequisites(state, "gene_annotation")

            self.assertFalse(check.can_run)
            self.assertEqual(
                [res.resource_name for res in check.missing], ["masked assembly"]
            )
            self.assertIn("repeat_annotation", check.suggestions[0])

    def test_gene_annotation_allows_explicit_unmasked_fallback_reason(self):
        """Allow unmasked assembly only with an explicit fallback reason."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_path = Path(tmpdir) / "assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:assembly:1",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_path),
                    }
                ],
            )

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}
            state["skip_reasons"] = {
                "repeat_annotation": "repeat_annotation_skipped_by_user"
            }

            check = check_stage_prerequisites(state, "gene_annotation")

            self.assertTrue(check.can_run)
            self.assertEqual(len(check.missing), 0)
            self.assertIn("Optional masked assembly not found", check.warnings[0])


class PreflightValidationTests(unittest.TestCase):
    """Tests for validate_execution_plan()."""

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        """Create a minimal asset registry JSON file."""
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _registry_ref(self, data_dir: str, session_id: str, registry_path: Path) -> dict:
        paths = SessionPaths.from_run_dir(data_dir)
        source = paths.work_path("inventory", "asset_registry.json")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(registry_path.read_text(encoding="utf-8"), encoding="utf-8")
        artifact = ArtifactMeta.from_file(
            source,
            stage="inventory",
            method_name="inventory.register_assets",
            artifact_type="asset_registry",
        )
        published = publish_artifact_record(data_dir, session_id, artifact)
        return published.ref.model_dump(mode="json")

    def test_validate_execution_plan_valid(self):
        """测试有效的执行计划"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create actual resource files
            assembly_path = Path(tmpdir) / "assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            # Create registry with all required resources
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:assembly:1",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_path),
                    }
                ],
            )

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            validation = validate_execution_plan(
                state,
                requested_stages=["hic_scaffolding", "repeat_annotation"],
            )

            self.assertTrue(validation.valid)
            self.assertIsNone(validation.first_blocker)
            self.assertEqual(len(validation.checks), 2)
            self.assertIn("hic_scaffolding", validation.checks)
            self.assertIn("repeat_annotation", validation.checks)
            self.assertTrue(validation.checks["hic_scaffolding"].can_run)
            self.assertTrue(validation.checks["repeat_annotation"].can_run)

    def test_validate_execution_plan_blocker(self):
        """测试有阻塞的执行计划"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create registry without assembly
            registry = self._write_registry(tmpdir, assets=[])

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            validation = validate_execution_plan(
                state,
                requested_stages=["hic_scaffolding", "repeat_annotation"],
            )

            self.assertFalse(validation.valid)
            self.assertEqual(validation.first_blocker, "hic_scaffolding")
            self.assertIn("assembly", validation.summary)
            self.assertGreater(len(validation.suggestions), 0)

    def test_validate_execution_plan_second_stage_blocker(self):
        """Test that first_blocker identifies the first blocking stage."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create registry without assembly
            registry = self._write_registry(tmpdir, assets=[])

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            # Both stages require assembly, should block at first one
            validation = validate_execution_plan(
                state,
                requested_stages=["repeat_annotation", "gene_annotation"],
            )

            self.assertFalse(validation.valid)
            # First in STAGE_ORDER that blocks
            self.assertEqual(validation.first_blocker, "repeat_annotation")
            self.assertFalse(validation.checks["repeat_annotation"].can_run)
            self.assertFalse(validation.checks["gene_annotation"].can_run)

    def test_validate_execution_plan_with_mixed_results(self):
        """Test plan where some stages pass and some fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create assembly but no reads
            assembly_path = Path(tmpdir) / "assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:assembly:1",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_path),
                    }
                ],
            )

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            # read_qc requires reads, hic_scaffolding requires assembly
            validation = validate_execution_plan(
                state,
                requested_stages=["read_qc", "hic_scaffolding"],
            )

            self.assertFalse(validation.valid)
            # read_qc comes first in STAGE_ORDER and blocks
            self.assertEqual(validation.first_blocker, "read_qc")
            self.assertFalse(validation.checks["read_qc"].can_run)
            # hic_scaffolding would pass but plan is invalid due to earlier blocker
            self.assertTrue(validation.checks["hic_scaffolding"].can_run)

    def test_validate_execution_plan_repeat_satisfies_gene_softmask(self):
        """A planned repeat stage satisfies the gene softmask requirement."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_path = Path(tmpdir) / "assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:assembly:1",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_path),
                    }
                ],
            )

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            validation = validate_execution_plan(
                state,
                requested_stages=["repeat_annotation", "gene_annotation"],
            )

            self.assertTrue(validation.valid)
            self.assertTrue(validation.checks["repeat_annotation"].can_run)
            self.assertTrue(validation.checks["gene_annotation"].can_run)

    def test_validate_execution_plan_function_annotation_requires_gene_proteins(self):
        """Fail before runtime when function-stage proteins are missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            validation = validate_execution_plan(
                state,
                requested_stages=["function_annotation"],
            )

            self.assertFalse(validation.valid)
            self.assertEqual(validation.first_blocker, "function_annotation")
            self.assertEqual(
                validation.checks["function_annotation"].missing[0].resource_name,
                "predicted proteins",
            )

    def test_validate_execution_plan_gene_satisfies_function_proteins(self):
        """A planned gene stage satisfies the function protein input."""
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_path = Path(tmpdir) / "assembly.fa"
            assembly_path.write_text(">contig1\nACGT\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:assembly:1",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_path),
                    }
                ],
            )

            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = self._registry_ref(state["data_dir"], state["session_id"], registry)
            state["stage_outputs"] = {}

            validation = validate_execution_plan(
                state,
                requested_stages=[
                    "repeat_annotation",
                    "gene_annotation",
                    "function_annotation",
                ],
            )

            self.assertTrue(validation.valid)
            self.assertTrue(validation.checks["function_annotation"].can_run)


if __name__ == "__main__":
    unittest.main()
