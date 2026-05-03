"""Tests for the Phase 2 read_qc SeqKit stats slice."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.read_qc.builder import build_stage


class ReadQCSeqKitDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_seqkit_stats_method(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        self.assertIn("seqkit", registry.packages)
        self.assertIn("read_qc.long_read_stats.seqkit_stats", registry.methods)
        method = registry.methods["read_qc.long_read_stats.seqkit_stats"]
        self.assertEqual(method.stage, "read_qc")
        self.assertEqual(method.package_ref, "seqkit")
        self.assertIn("read_qc.long_read_stats.seqkit_stats", registry.by_stage["read_qc"])

    def test_descriptor_registry_loads_length_filter_candidates(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "read_qc.length_filter.chopper": "chopper",
            "read_qc.length_filter.fastplong": "fastplong",
            "read_qc.length_filter.filtlong": "filtlong",
            "read_qc.length_filter.seqkit_seq": "seqkit",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["read_qc"])


class ReadQCSeqKitStageTests(unittest.TestCase):
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
        state["current_stage"] = "read_qc"
        state["asset_registry_ref"] = registry_ref
        return state

    def _fake_seqkit_bin(self, tmpdir: str) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir()
        seqkit = bin_dir / "seqkit"
        seqkit.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"stats\" ] && [ \"$2\" = \"-a\" ] && [ \"$3\" = \"-T\" ]; then\n"
            "  printf 'file\\tformat\\ttype\\tnum_seqs\\tsum_len\\tmin_len\\tavg_len\\tmax_len\\tN50\\tQ20(%%)\\tQ30(%%)\\tAvgQual\\n'\n"
            "  printf '%s\\tFASTQ\\tDNA\\t1\\t4\\t4\\t4.0\\t4\\t4\\t75.0\\t50.0\\t18.5\\n' \"$4\"\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"seq\" ] && [ \"$2\" = \"-m\" ]; then\n"
            "  cat \"$4\"\n"
            "  exit 0\n"
            "fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        seqkit.chmod(0o755)
        return bin_dir

    def test_missing_asset_registry_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            graph = build_stage()
            result = graph.invoke(self._state(tmpdir, None))

            self.assertEqual(result["current_stage"], "read_qc")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("asset_registry_ref", result["last_error"])

    def test_registry_without_raw_long_reads_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_short_dna_reads:R1.fq",
                        "asset_type": "raw_short_dna_reads",
                        "path": str(Path(tmpdir) / "R1.fq"),
                    }
                ],
            )
            graph = build_stage()
            result = graph.invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "read_qc")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("raw_long_reads", result["last_error"])

    def test_seqkit_stats_success_writes_artifacts_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "BGI_CycloneSEQ_sample.fastq.gz"
            fq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:BGI_CycloneSEQ_sample.fastq.gz",
                        "asset_type": "raw_long_reads",
                        "path": str(fq),
                        "platform": "cycloneseq",
                        "format": "fastq",
                    }
                ],
            )
            fake_bin = self._fake_seqkit_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                }
            }

            graph = build_stage()
            result = graph.invoke(state)

            self.assertEqual(result["current_stage"], "genome_survey")
            self.assertEqual(result["stage_outputs"]["read_qc"]["status"], "completed")
            self.assertEqual(
                result["stage_outputs"]["read_qc"]["method_name"],
                "read_qc.long_read_stats.seqkit_stats",
            )
            stats_ref = Path(result["stage_outputs"]["read_qc"]["stats_artifact_ref"])
            self.assertTrue(stats_ref.exists())
            self.assertIn("num_seqs", stats_ref.read_text(encoding="utf-8"))
            summary = result["stage_outputs"]["read_qc"]["summary"]
            self.assertEqual(summary["num_seqs"], 1)
            self.assertEqual(summary["sum_len"], 4)
            self.assertEqual(summary["n50"], 4)
            self.assertEqual(summary["q20_percent"], 75.0)
            self.assertEqual(summary["q30_percent"], 50.0)
            self.assertEqual(summary["avg_qual"], 18.5)
            self.assertNotIn("stats_table", result["stage_outputs"]["read_qc"])

            artifact_index = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            self.assertTrue(artifact_index.exists())
            artifact_events = [json.loads(line) for line in artifact_index.read_text().splitlines()]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("read_qc.long_read_stats", artifact_types)
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)

            ledger_events = [
                json.loads(line)
                for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_succeeded", event_types)
            self.assertIn("artifact_registered", event_types)

    def test_without_filter_thresholds_skips_filter_and_writes_raw_fofn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "reads.fastq"
            fq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq),
                    }
                ],
            )
            fake_bin = self._fake_seqkit_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                }
            }

            graph = build_stage()
            result = graph.invoke(state)

            read_qc = result["stage_outputs"]["read_qc"]
            self.assertEqual(read_qc["substeps"]["long_read_stats"]["status"], "executed")
            self.assertEqual(read_qc["substeps"]["length_filter"]["status"], "skipped")
            self.assertEqual(
                read_qc["substeps"]["length_filter"]["skip_reason"],
                "no_filtering_requested",
            )
            fofn_ref = Path(read_qc["long_reads_fofn_ref"])
            self.assertTrue(fofn_ref.exists())
            self.assertEqual(fofn_ref.read_text(encoding="utf-8"), f"{fq}\n")
            self.assertEqual(read_qc["assembly_input_ref"], str(fofn_ref))
            self.assertNotIn("filtered_long_reads_ref", read_qc)

            artifact_events = [
                json.loads(line)
                for line in (Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("read_qc.long_reads.fofn", artifact_types)

    def test_approved_min_length_filter_runs_and_writes_filtered_fofn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "reads.fastq"
            fq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq),
                    }
                ],
            )
            fake_bin = self._fake_seqkit_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "length_filter_request": {
                        "approved": True,
                        "method_name": "read_qc.length_filter.seqkit_seq",
                        "thresholds": {"min_len": 1000},
                    },
                }
            }

            graph = build_stage()
            result = graph.invoke(state)

            read_qc = result["stage_outputs"]["read_qc"]
            self.assertEqual(read_qc["substeps"]["length_filter"]["status"], "executed")
            self.assertEqual(
                read_qc["substeps"]["length_filter"]["method_name"],
                "read_qc.length_filter.seqkit_seq",
            )
            self.assertEqual(read_qc["substeps"]["length_filter"]["thresholds"], {"min_len": 1000})
            filtered_ref = Path(read_qc["filtered_long_reads_ref"])
            self.assertTrue(filtered_ref.exists())
            self.assertEqual(filtered_ref.read_text(encoding="utf-8"), fq.read_text(encoding="utf-8"))
            fofn_ref = Path(read_qc["filtered_long_reads_fofn_ref"])
            self.assertTrue(fofn_ref.exists())
            self.assertEqual(fofn_ref.read_text(encoding="utf-8"), f"{filtered_ref}\n")
            self.assertEqual(read_qc["assembly_input_ref"], str(fofn_ref))

            filter_script = Path(tmpdir) / "work" / "read_qc" / "s1" / "length_filter" / "run_length_filter.sh"
            self.assertIn("seqkit seq -m 1000", filter_script.read_text(encoding="utf-8"))
            artifact_events = [
                json.loads(line)
                for line in (Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("read_qc.filtered_long_reads", artifact_types)
            self.assertIn("read_qc.filtered_long_reads.fofn", artifact_types)

    def test_unapproved_filter_thresholds_skip_filter_with_decision_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "reads.fastq"
            fq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq),
                    }
                ],
            )
            fake_bin = self._fake_seqkit_bin(tmpdir)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                    "length_filter_request": {
                        "method_name": "read_qc.length_filter.seqkit_seq",
                        "thresholds": {"min_len": 1000},
                    },
                }
            }

            graph = build_stage()
            result = graph.invoke(state)

            read_qc = result["stage_outputs"]["read_qc"]
            self.assertEqual(read_qc["substeps"]["length_filter"]["status"], "skipped")
            self.assertEqual(
                read_qc["substeps"]["length_filter"]["skip_reason"],
                "threshold_decision_missing",
            )
            self.assertNotIn("filtered_long_reads_ref", read_qc)
            self.assertEqual(Path(read_qc["long_reads_fofn_ref"]).read_text(encoding="utf-8"), f"{fq}\n")

    def test_missing_seqkit_records_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "reads.fastq"
            fq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "read_qc": {
                    "execution_mode": "local",
                    "env": {"PATH": str(Path(tmpdir) / "empty-bin")},
                }
            }

            graph = build_stage()
            result = graph.invoke(state)

            self.assertEqual(result["current_stage"], "read_qc")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("seqkit", result["last_error"])
            stats_dir = Path(tmpdir) / "artifacts" / "read_qc" / "s1"
            self.assertFalse((stats_dir / "seqkit_stats.tsv").exists())
            ledger_events = [
                json.loads(line)
                for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text().splitlines()
            ]
            self.assertIn("attempt_failed", [event["event_type"] for event in ledger_events])


if __name__ == "__main__":
    unittest.main()
