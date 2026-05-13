"""Tests for the Phase 2 read_qc SeqKit stats slice."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
            self.assertNotIn("raw_long_reads_path", result["stage_outputs"]["read_qc"])
            self.assertNotIn("frozen_attempt", result["stage_outputs"]["read_qc"])
            self.assertNotIn("execution_result", result["stage_outputs"]["read_qc"])
            self.assertNotIn("stats_output_path", result["stage_outputs"]["read_qc"])

            artifact_index = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            self.assertTrue(artifact_index.exists())
            artifact_events = [json.loads(line) for line in artifact_index.read_text().splitlines()]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("read_qc.long_read_stats", artifact_types)
            self.assertIn("read_qc.long_read_summary", artifact_types)
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)

            summary_ref = Path(result["stage_outputs"]["read_qc"]["summary_artifact_ref"])
            self.assertTrue(summary_ref.exists())
            summary_payload = json.loads(summary_ref.read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["method_name"], "read_qc.long_read_stats.seqkit_stats")
            self.assertEqual(summary_payload["summary"]["num_seqs"], 1)

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
            self.assertNotIn("filtered_long_reads_fofn_ref", read_qc)

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
            self.assertNotIn("length_filter_request", read_qc)
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
            self.assertNotIn("filtered_long_reads_fofn_ref", read_qc)
            self.assertNotIn("length_filter_request", read_qc)
            self.assertEqual(read_qc["assembly_input_ref"], read_qc["long_reads_fofn_ref"])
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


class Phase4ReadQCSmokeTests(unittest.TestCase):
    """Phase 4 task2 coverage: env resolver wiring and structured blocked states."""

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

    def test_missing_fastq_path_blocks_with_blocked_input_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.fastq"
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:missing.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(missing),
                    }
                ],
            )
            graph = build_stage()
            result = graph.invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "read_qc")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(result["stage_outputs"]["read_qc"]["status"], "blocked_input")
            self.assertIn("missing.fastq", result["last_error"])

    def test_missing_conda_env_blocks_with_blocked_env_status(self):
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
            # Empty PATH removes conda binary, which forces env_resolver.preflight
            # to report blocked_env. Note: no env override is supplied on
            # stage_outputs.read_qc.env, so the resolver path is taken.
            state["stage_outputs"] = {"read_qc": {"execution_mode": "local"}}
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ""
            try:
                graph = build_stage()
                result = graph.invoke(state)
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(result["current_stage"], "read_qc")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(result["stage_outputs"]["read_qc"]["status"], "blocked_env")
            self.assertIn("conda", result["last_error"])
            ledger_events = [
                json.loads(line)
                for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("env_preflight_blocked", event_types)

    def test_missing_tool_in_env_blocks_with_missing_tool_status(self):
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
            # Stub conda that reports env ok but tool probe fails.
            conda_stub_dir = Path(tmpdir) / "conda-stub-bin"
            conda_stub_dir.mkdir()
            conda_stub = conda_stub_dir / "conda"
            conda_stub.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"run\" ]; then\n"
                "  shift\n"
                "  while [ \"$#\" -gt 0 ]; do\n"
                "    case \"$1\" in\n"
                "      --live-stream|--no-capture-output) shift ;;\n"
                "      -n|-p) shift 2 ;;\n"
                "      *) break ;;\n"
                "    esac\n"
                "  done\n"
                "  # Env probe is `true`; pretend it succeeded.\n"
                "  if [ \"$1\" = \"true\" ]; then exit 0; fi\n"
                "  # Tool probe is `sh -c command -v \"$1\" preflight TOOL`.\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            conda_stub.chmod(0o755)
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {"read_qc": {"execution_mode": "local"}}
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{conda_stub_dir}{os.pathsep}{old_path}"
            try:
                graph = build_stage()
                result = graph.invoke(state)
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(result["stage_outputs"]["read_qc"]["status"], "missing_tool")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("seqkit", result["stage_outputs"]["read_qc"]["missing_tools"])

    def test_phase4_fixture_manifest_registers_as_raw_long_reads(self):
        # Sanity: the committed fixture file exists and is non-empty, so both
        # tests and a documented smoke command can rely on it.
        fixture = Path("tests/fixtures/phase4/smoke.fastq")
        manifest = Path("tests/fixtures/phase4/read_qc_manifest.yaml")
        self.assertTrue(fixture.exists())
        self.assertGreater(fixture.stat().st_size, 0)
        self.assertTrue(manifest.exists())
        text = manifest.read_text(encoding="utf-8")
        self.assertIn("raw_long_reads", text)
        self.assertIn("tests/fixtures/phase4/smoke.fastq", text)


def _env_geno_exec_has_seqkit() -> bool:
    """True if the configured env_geno_exec conda env exposes seqkit."""
    if shutil.which("conda") is None:
        return False
    probe = subprocess.run(
        [
            "conda", "run", "--live-stream", "--no-capture-output",
            "-n", "env_geno_exec", "sh", "-c", 'command -v "$1"',
            "preflight", "seqkit",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


@unittest.skipUnless(
    _env_geno_exec_has_seqkit(),
    "env_geno_exec conda env with seqkit not available; skipping real smoke",
)
class Phase4ReadQCRealSmokeTests(unittest.TestCase):
    """Real-data smoke: committed fixture -> env_geno_exec conda env -> SeqKit.

    Skipped automatically when conda or env_geno_exec is not present so the
    broader suite stays green on hosts without the bioinformatics env.
    """

    def test_committed_fixture_smoke_produces_stats_summary_and_fofn(self):
        from app.entrypoints.run import main as run_main

        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "phase4-readqc-smoke"
            argv = [
                "run",
                "--session-id", session_id,
                "--manifest", "tests/fixtures/phase4/read_qc_manifest.yaml",
                "--data-dir", tmpdir,
                "--local-exec", "read_qc",
            ]
            rc = run_main(argv)
            self.assertEqual(rc, 0)

            artifact_dir = Path(tmpdir) / "artifacts" / "read_qc" / session_id
            stats = artifact_dir / "seqkit_stats.tsv"
            summary = artifact_dir / "read_qc_summary.json"
            fofn = artifact_dir / "long_reads.fofn"
            self.assertTrue(stats.exists(), f"seqkit_stats.tsv missing at {stats}")
            self.assertTrue(summary.exists(), f"read_qc_summary.json missing at {summary}")
            self.assertTrue(fofn.exists(), f"long_reads.fofn missing at {fofn}")

            # SeqKit TSV header should be intact and we should see three reads.
            stats_text = stats.read_text(encoding="utf-8")
            self.assertIn("num_seqs", stats_text)
            summary_payload = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["summary"]["num_seqs"], 3)

            # FOFN must point to a real, readable path (absolute after
            # inventory resolution).
            fofn_lines = [line for line in fofn.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(fofn_lines), 1)
            fofn_path = Path(fofn_lines[0])
            self.assertTrue(fofn_path.is_absolute(), f"FOFN entry not absolute: {fofn_path}")
            self.assertTrue(fofn_path.exists(), f"FOFN path does not exist: {fofn_path}")


if __name__ == "__main__":
    unittest.main()
