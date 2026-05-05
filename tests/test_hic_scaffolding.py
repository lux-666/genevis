"""Tests for Phase 2 Hi-C scaffolding or skip behavior."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.hic_scaffolding.builder import build_stage


class HiCScaffoldingDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_hic_scaffolding_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "hic_scaffolding.candidate.yahs": "yahs",
            "hic_scaffolding.candidate.juicer_3ddna_juicebox": "juicer",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "hic_scaffolding")
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["hic_scaffolding"])


class HiCScaffoldingStageTests(unittest.TestCase):
    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str, registry_ref: str | None):
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "hic_scaffolding"
        state["asset_registry_ref"] = registry_ref
        return state

    def test_missing_final_assembly_ref_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "hic_scaffolding")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("final_assembly_ref", result["last_error"])

    def test_no_hic_reads_skips_and_continues_with_contig_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "repeat_annotation")
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "skipped")
            self.assertEqual(hic["mode"], "skipped_no_hic")
            self.assertEqual(hic["skip_reason"], "no_hic_reads")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertEqual(hic["pre_hic_assembly_ref"], str(assembly))
            self.assertEqual(result["skip_reasons"]["hic_scaffolding"], "no_hic_reads")
            skip_ref = Path(hic["skip_record_ref"])
            self.assertTrue(skip_ref.exists())
            payload = json.loads(skip_ref.read_text(encoding="utf-8"))
            self.assertEqual(payload["final_assembly_for_annotation"], str(assembly))

    def test_hic_present_but_not_chromosome_level_skips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            hic_reads = Path(tmpdir) / "sample.hic"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            hic_reads.write_text("hic", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_reads_registered:sample.hic",
                        "asset_type": "hic_reads_registered",
                        "path": str(hic_reads),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "contig"},
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "repeat_annotation")
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "skipped")
            self.assertEqual(hic["mode"], "skipped_not_chromosome_level")
            self.assertEqual(hic["skip_reason"], "not_chromosome_level_requested")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))

    def test_hic_present_and_chromosome_requested_plans_candidates_and_blocks_human(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            hic_r1 = Path(tmpdir) / "hic_R1.fq.gz"
            hic_r2 = Path(tmpdir) / "hic_R2.fq.gz"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            hic_r1.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            hic_r2.write_text("@r2\nTGCA\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_reads_registered:hic_R1.fq.gz",
                        "asset_type": "hic_reads_registered",
                        "path": str(hic_r1),
                    },
                    {
                        "asset_id": "s1:hic_reads_registered:hic_R2.fq.gz",
                        "asset_type": "hic_reads_registered",
                        "path": str(hic_r2),
                    },
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "hic_scaffolding")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["active_interrupt"]["stage"], "hic_scaffolding")
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_human")
            self.assertEqual(hic["mode"], "scaffold")
            self.assertEqual(hic["pre_hic_assembly_ref"], str(assembly))
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertIn("hic_scaffolding.candidate.yahs", hic["candidate_methods"])
            self.assertIn("hic_scaffolding.candidate.juicer_3ddna_juicebox", hic["candidate_methods"])
            self.assertTrue(Path(hic["scaffold_plan_ref"]).exists())
            plan = json.loads(Path(hic["scaffold_plan_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["pre_hic_assembly_ref"], str(assembly))
            self.assertEqual(plan["fallback_assembly_ref"], str(assembly))


if __name__ == "__main__":
    unittest.main()
