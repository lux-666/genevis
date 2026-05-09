"""Tests for Phase 3 repeat annotation realization."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.repeat_annotation.builder import build_stage


class RepeatAnnotationDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_repeat_annotation_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "repeat_annotation.library.edta": "edta",
            "repeat_annotation.library.repeatmodeler": "repeatmodeler",
            "repeat_annotation.masking.repeatmasker": "repeatmasker",
            "repeat_annotation.helper.trf": "trf",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "repeat_annotation")
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["repeat_annotation"])


class RepeatAnnotationStageTests(unittest.TestCase):
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
        state["current_stage"] = "repeat_annotation"
        state["asset_registry_ref"] = registry_ref
        return state

    def _fake_repeatmasker_bin(self, tmpdir: str, *, fail: bool = False) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "RepeatMasker"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "if " + repr(fail) + ":\n"
            "    print('fake RepeatMasker failed', file=sys.stderr)\n"
            "    sys.exit(9)\n"
            "assembly = pathlib.Path(sys.argv[-1])\n"
            "assembly.with_name(assembly.name + '.masked').write_text('>ctg1\\nacgt\\n', encoding='utf-8')\n"
            "assembly.with_name(assembly.name + '.out.gff').write_text('##gff-version 3\\nctg1\\tRepeatMasker\\trepeat_region\\t1\\t4\\t.\\t+\\t.\\tID=repeat1\\n', encoding='utf-8')\n"
            "print('fake RepeatMasker ok')\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def test_missing_final_assembly_for_annotation_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "repeat_annotation")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("final_assembly_for_annotation", result["last_error"])

    def test_provided_repeat_library_skips_de_novo_discovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            library = Path(tmpdir) / "trusted_repeats.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            library.write_text(">repeat1\nACAC\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:repeat_library:trusted_repeats.fa",
                        "asset_type": "repeat_library",
                        "path": str(library),
                        "metadata": {"source": "provided", "trusted": True},
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "hic_scaffolding": {
                    "status": "skipped",
                    "final_assembly_for_annotation": str(assembly),
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "gene_annotation")
            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["status"], "completed")
            self.assertEqual(repeat["strategy"], "provided")
            self.assertEqual(repeat["repeat_library_source"], "provided")
            self.assertEqual(repeat["repeat_library_ref"], str(library))
            self.assertEqual(repeat["library_candidate_methods"], [])
            self.assertIn("repeat_annotation.masking.repeatmasker", repeat["candidate_methods"])
            self.assertNotIn("repeat_annotation.library.edta", repeat["candidate_methods"])
            self.assertNotIn("repeat_annotation.library.repeatmodeler", repeat["candidate_methods"])
            self.assertTrue(repeat["softmasked_genome_ref"].endswith("softmasked_genome.fasta"))
            self.assertTrue(Path(repeat["plan_artifact_ref"]).exists())
            self.assertTrue(Path(repeat["bundle_artifact_ref"]).exists())
            bundle = json.loads(Path(repeat["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(bundle["repeat_library_source"], "provided")
            self.assertFalse(bundle["metadata"]["execution_enabled"])

    def test_local_mode_without_explicit_repeatmasker_method_remains_planning_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            library = Path(tmpdir) / "trusted_repeats.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            library.write_text(">repeat1\nACAC\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:repeat_library:trusted", "asset_type": "repeat_library", "path": str(library)}],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "hic_scaffolding": {"status": "skipped", "final_assembly_for_annotation": str(assembly)},
                "repeat_annotation": {"execution_mode": "local"},
            }

            result = build_stage().invoke(state)

            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(result["current_stage"], "gene_annotation")
            self.assertFalse(repeat["execution_enabled"])
            self.assertFalse((Path(tmpdir) / "ledger" / "s1" / "events.jsonl").exists())

    def test_local_repeatmasker_runs_with_provided_library_and_registers_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            library = Path(tmpdir) / "trusted_repeats.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            library.write_text(">repeat1\nACAC\n", encoding="utf-8")
            bin_dir = self._fake_repeatmasker_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:repeat_library:trusted", "asset_type": "repeat_library", "path": str(library)}],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "hic_scaffolding": {"status": "skipped", "final_assembly_for_annotation": str(assembly)},
                "repeat_annotation": {
                    "execution_mode": "local",
                    "primary_method": "repeat_annotation.masking.repeatmasker",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "gene_annotation")
            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["status"], "completed")
            self.assertTrue(Path(repeat["softmasked_genome_ref"]).exists())
            self.assertTrue(Path(repeat["repeat_annotation_gff3_ref"]).exists())
            self.assertTrue(repeat["execution_enabled"])
            self.assertEqual(repeat["execution_mode"], "local")
            self.assertIn("repeatmasker_executed_locally", repeat["notes"])
            bundle = json.loads(Path(repeat["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(bundle["status"], "executed")
            self.assertTrue(bundle["metadata"]["execution_enabled"])
            artifact_index = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
            self.assertIn("repeat_annotation.softmasked_genome", artifact_types)
            self.assertIn("repeat_annotation.gff3", artifact_types)
            self.assertIn("command.stdout", artifact_types)
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("attempt_started", [event["event_type"] for event in events])
            self.assertIn("attempt_succeeded", [event["event_type"] for event in events])

    def test_local_repeatmasker_failure_stops_before_gene_annotation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            library = Path(tmpdir) / "trusted_repeats.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            library.write_text(">repeat1\nACAC\n", encoding="utf-8")
            bin_dir = self._fake_repeatmasker_bin(tmpdir, fail=True)
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:repeat_library:trusted", "asset_type": "repeat_library", "path": str(library)}],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "hic_scaffolding": {"status": "skipped", "final_assembly_for_annotation": str(assembly)},
                "repeat_annotation": {
                    "execution_mode": "local",
                    "primary_method": "repeat_annotation.masking.repeatmasker",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "repeat_annotation")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("RepeatMasker", result["last_error"])
            artifact_index = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            event_types = [event["event_type"] for event in events]
            self.assertIn("attempt_failed", event_types)
            self.assertIn("artifact_registered", event_types)

    def test_de_novo_strategy_plans_candidates_without_default_choice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "hic_scaffolding": {
                    "status": "skipped",
                    "final_assembly_for_annotation": str(assembly),
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "gene_annotation")
            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["strategy"], "de_novo")
            self.assertEqual(repeat["repeat_library_source"], "de_novo")
            self.assertIn("repeat_annotation.library.edta", repeat["library_candidate_methods"])
            self.assertIn("repeat_annotation.library.repeatmodeler", repeat["library_candidate_methods"])
            self.assertIn("repeat_annotation.helper.trf", repeat["helper_methods"])
            self.assertIn("repeat_annotation.masking.repeatmasker", repeat["candidate_methods"])
            self.assertTrue(repeat["method_selection_required"])
            self.assertNotIn("primary_method", repeat)
            plan = json.loads(Path(repeat["plan_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertFalse(plan["metadata"]["execution_enabled"])
            self.assertTrue(plan["method_selection_required"])

    def test_local_de_novo_strategy_remains_planning_only_without_materialized_library(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "repeat_annotation": {
                    "execution_mode": "local",
                    "primary_method": "repeat_annotation.masking.repeatmasker",
                },
            }

            result = build_stage().invoke(state)

            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(result["current_stage"], "gene_annotation")
            self.assertEqual(repeat["strategy"], "de_novo")
            self.assertFalse(repeat["execution_enabled"])
            self.assertFalse((Path(tmpdir) / "ledger" / "s1" / "events.jsonl").exists())

    def test_fragmented_assembly_marks_low_confidence_but_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                    "confidence": "low",
                    "low_confidence_reasons": ["fragmented_assembly"],
                    "metadata": {"assembly_fragmentation": "high"},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "gene_annotation")
            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["confidence"], "low")
            self.assertIn("fragmented_assembly", repeat["low_confidence_reasons"])
            self.assertTrue(repeat["softmasked_genome_ref"])
            bundle = json.loads(Path(repeat["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(bundle["confidence"], "low")

    def test_repeat_annotation_routes_to_gene_annotation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "gene_annotation")
            self.assertIn("repeat_annotation_stage", result["route_trace"])


if __name__ == "__main__":
    unittest.main()
