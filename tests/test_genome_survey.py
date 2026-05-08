"""Tests for Phase 2 genome_survey full and fallback modes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.genome_survey.builder import build_stage


class GenomeSurveyDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_genome_survey_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        for package_name in ["jellyfish", "meryl", "genomescope2", "smudgeplot"]:
            self.assertIn(package_name, registry.packages)

        expected_methods = {
            "genome_survey.kmer_count.jellyfish",
            "genome_survey.kmer_count.meryl",
            "genome_survey.model.genomescope2",
            "genome_survey.model.smudgeplot",
        }
        self.assertTrue(expected_methods.issubset(set(registry.by_stage["genome_survey"])))


class GenomeSurveyStageTests(unittest.TestCase):
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
        state["current_stage"] = "genome_survey"
        state["asset_registry_ref"] = registry_ref
        return state

    def test_missing_asset_registry_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = build_stage().invoke(self._state(tmpdir, None))

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("asset_registry_ref", result["last_error"])

    def test_full_mode_with_clean_short_reads_writes_high_confidence_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_r1 = Path(tmpdir) / "short_R1.fq.gz"
            short_r2 = Path(tmpdir) / "short_R2.fq.gz"
            long_reads = Path(tmpdir) / "BGI_CycloneSEQ.fastq.gz"
            for path in [short_r1, short_r2, long_reads]:
                path.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")

            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:clean_short_dna_reads:short_R1.fq.gz",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_r1),
                        "platform": "illumina",
                    },
                    {
                        "asset_id": "s1:clean_short_dna_reads:short_R2.fq.gz",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_r2),
                        "platform": "illumina",
                    },
                    {
                        "asset_id": "s1:raw_long_reads:BGI_CycloneSEQ.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(long_reads),
                        "platform": "cycloneseq",
                    },
                ],
            )

            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "full")
            self.assertEqual(survey["confidence"], "high")
            self.assertEqual(survey["skip_reason"], None)
            self.assertIn("genome_survey.kmer_count.jellyfish", survey["candidate_methods"])
            self.assertIn("genome_survey.model.genomescope2", survey["candidate_methods"])
            artifact_ref = Path(survey["result_artifact_ref"])
            self.assertTrue(artifact_ref.exists())
            payload = json.loads(artifact_ref.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "full")
            self.assertEqual(payload["confidence"], "high")

    def test_long_read_only_fallback_is_low_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            long_reads = Path(tmpdir) / "BGI_CycloneSEQ.fastq.gz"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:BGI_CycloneSEQ.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(long_reads),
                        "platform": "cycloneseq",
                        "metadata": {"genome_size_estimate": 2_000_000},
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "completed",
                    "summary": {"total_bases": 10_000_000},
                }
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["mode"], "fallback_low_confidence")
            self.assertEqual(survey["confidence"], "low")
            self.assertNotEqual(survey["confidence"], "high")
            self.assertEqual(survey["estimates"]["coverage"], 5.0)
            self.assertEqual(survey["candidate_methods"], [])
            self.assertIn("not_equivalent_to_kmer_survey", survey["notes"])
            payload = json.loads(Path(survey["result_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["candidate_methods"], [])
            self.assertIn("not_equivalent_to_kmer_survey", payload["notes"])

    def test_no_usable_inputs_writes_skip_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna_R1.fq.gz"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_rnaseq_reads:rna_R1.fq.gz",
                        "asset_type": "raw_rnaseq_reads",
                        "path": str(rnaseq),
                    }
                ],
            )

            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "assembly")
            survey = result["stage_outputs"]["genome_survey"]
            self.assertEqual(survey["status"], "skipped")
            self.assertEqual(survey["mode"], "skipped")
            self.assertEqual(survey["skip_reason"], "no_low_error_short_reads")
            self.assertEqual(result["skip_reasons"]["genome_survey"], "no_low_error_short_reads")
            self.assertTrue(Path(survey["result_artifact_ref"]).exists())


if __name__ == "__main__":
    unittest.main()
