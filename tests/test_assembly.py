"""Tests for Phase 2 assembly candidate planning."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.assembly.builder import build_stage


class AssemblyDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_assembly_candidates(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "assembly.candidate.flye": "flye",
            "assembly.candidate.hifiasm": "hifiasm",
            "assembly.candidate.nextdenovo": "nextdenovo",
            "assembly.candidate.canu": "canu",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "assembly")
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["assembly"])
        hifiasm = registry.methods["assembly.candidate.hifiasm"]
        self.assertIn("compatibility candidate", hifiasm.goal)
        self.assertIn("compatibility_route_explicitly_requested", hifiasm.review_rules)
        self.assertIn("explicit_hifi_or_non_ont_route_required", hifiasm.human_review_triggers)


class AssemblyStageTests(unittest.TestCase):
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
        state["current_stage"] = "assembly"
        state["asset_registry_ref"] = registry_ref
        return state

    def test_missing_assembly_inputs_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna.fq"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_rnaseq_reads:rna.fq",
                        "asset_type": "raw_rnaseq_reads",
                        "path": str(rnaseq),
                    }
                ],
            )

            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("clean_long_reads or long_reads.fofn", result["last_error"])

    def test_plans_primary_and_one_fallback_from_fofn_and_genome_size_prior(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fofn = Path(tmpdir) / "long_reads.fofn"
            fofn.write_text(f"{Path(tmpdir) / 'reads.fastq'}\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "assembly_input_ref": str(fofn),
                },
                "genome_survey": {
                    "status": "completed",
                    "mode": "fallback_low_confidence",
                    "estimates": {"genome_size_prior": 2_000_000},
                    "result_artifact_ref": str(Path(tmpdir) / "genome_survey_result.json"),
                },
                "assembly": {
                    "method_decision": {
                        "primary": "assembly.candidate.flye",
                        "fallback": "assembly.candidate.nextdenovo",
                    }
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly_review")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "planned")
            self.assertEqual(assembly["primary_method"], "assembly.candidate.flye")
            self.assertEqual(assembly["fallback_method"], "assembly.candidate.nextdenovo")
            self.assertEqual(len(assembly["selected_candidate_refs"]), 2)
            self.assertLessEqual(len(assembly["selected_candidate_refs"]), 2)
            for candidate_ref in assembly["selected_candidate_refs"]:
                candidate = json.loads(Path(candidate_ref).read_text(encoding="utf-8"))
                self.assertEqual(candidate["status"], "planned")
                self.assertEqual(candidate["input_ref"], str(fofn))
                self.assertEqual(candidate["genome_size_prior"], 2_000_000)
                self.assertEqual(candidate["artifact_type"], "draft_assembly.candidate")
                self.assertTrue(candidate["expected_outputs"]["draft_assembly_fasta"].endswith(".fasta"))

    def test_missing_genome_size_rejects_methods_that_require_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fofn = Path(tmpdir) / "long_reads.fofn"
            fofn.write_text(f"{Path(tmpdir) / 'reads.fastq'}\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "assembly_input_ref": str(fofn),
                },
                "assembly": {
                    "method_decision": {
                        "primary": "assembly.candidate.nextdenovo",
                    }
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            self.assertEqual(result["supervision_state"], "blocked_human")
            interrupt = result["active_interrupt"]
            self.assertEqual(interrupt["stage"], "assembly")
            self.assertIn("genome_size_required", interrupt["reason"])
            rejected = result["stage_outputs"]["assembly"]["rejected_candidates"]
            self.assertEqual(rejected["assembly.candidate.nextdenovo"], "genome_size_required")

    def test_auto_planning_without_human_decision_selects_no_more_than_primary_and_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "genome_survey": {
                    "status": "completed",
                    "estimates": {"genome_size_prior": 2_000_000},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly_review")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(len(assembly["selected_candidate_refs"]), 2)
            self.assertEqual(assembly["primary_method"], "assembly.candidate.flye")
            self.assertEqual(assembly["fallback_method"], "assembly.candidate.nextdenovo")
            self.assertNotIn("assembly.candidate.hifiasm", assembly["candidate_methods"])
            self.assertEqual(set(assembly["candidate_methods"]), {
                "assembly.candidate.flye",
                "assembly.candidate.nextdenovo",
                "assembly.candidate.canu",
            })

    def test_explicit_hifiasm_request_remains_allowed_as_compatibility_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "pacbio_hifi",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "assembly": {
                    "method_decision": {
                        "primary": "assembly.candidate.hifiasm",
                    }
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly_review")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["primary_method"], "assembly.candidate.hifiasm")
            self.assertIn("assembly.candidate.hifiasm", assembly["selected_methods"])
            self.assertIn("assembly.candidate.hifiasm", assembly["candidate_methods"])

    def test_auto_planning_without_genome_size_selects_only_flye(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_reads = Path(tmpdir) / "clean.fastq"
            clean_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_long_reads:clean.fastq",
                        "asset_type": "clean_long_reads",
                        "path": str(clean_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly_review")
            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["primary_method"], "assembly.candidate.flye")
            self.assertEqual(assembly["fallback_method"], None)
            self.assertEqual(assembly["selected_methods"], ["assembly.candidate.flye"])
            self.assertEqual(
                assembly["rejected_candidates"],
                {
                    "assembly.candidate.nextdenovo": "genome_size_required",
                    "assembly.candidate.canu": "genome_size_required",
                },
            )
            self.assertNotIn("assembly.candidate.hifiasm", assembly["candidate_methods"])


if __name__ == "__main__":
    unittest.main()
