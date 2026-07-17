"""Tests for the Phase 2 read_qc SeqKit stats slice."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import inspect
from pathlib import Path

from app.control.lifecycle import inspect_session
from app.control.ledger import Ledger
from app.graph.builder import build_graph
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir
from app.stages.read_qc.builder import build_stage
from app.stages.read_qc.method_application.make_fofn import build_make_fofn_graph
from app.stages.read_qc.method_application.seqkit_length_filter import build_seqkit_length_filter_graph
from app.stages.read_qc.method_application.seqkit_stats import build_seqkit_stats_graph
from app.stages.read_qc.method_application import make_fofn, seqkit_length_filter, seqkit_stats


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
            "read_qc.length_filter.seqkit_seq": "seqkit",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["read_qc"])

    def test_descriptor_registry_loads_make_fofn_with_runtime_method_name(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        self.assertIn("python", registry.packages)
        self.assertIn("read_qc.make_fofn.local", registry.methods)
        method = registry.methods["read_qc.make_fofn.local"]
        self.assertEqual(method.stage, "read_qc")
        self.assertEqual(method.package_ref, "python")
        self.assertIn("read_qc.make_fofn.local", registry.by_stage["read_qc"])
        self.assertNotIn("read_qc.handoff.make_fofn", registry.methods)


class ReadQCSeqKitStageTests(unittest.TestCase):
    def test_outer_graph_routes_to_method_subgraphs(self):
        outer_nodes = set(build_stage().get_graph().nodes)
        stats_nodes = set(build_seqkit_stats_graph().get_graph().nodes)
        filter_nodes = set(build_seqkit_length_filter_graph().get_graph().nodes)
        fofn_nodes = set(build_make_fofn_graph().get_graph().nodes)

        self.assertIn("seqkit_stats", outer_nodes)
        self.assertIn("seqkit_length_filter", outer_nodes)
        self.assertIn("make_fofn", outer_nodes)
        self.assertNotIn("freeze_seqkit_attempt", outer_nodes)
        self.assertNotIn("maybe_run_length_filter", outer_nodes)
        self.assertIn("freeze", stats_nodes)
        self.assertIn("run", stats_nodes)
        self.assertIn("collect", stats_nodes)
        self.assertIn("freeze", filter_nodes)
        self.assertIn("run", filter_nodes)
        self.assertIn("collect", filter_nodes)
        self.assertIn("make", fofn_nodes)

    def test_method_subgraphs_do_not_import_parent_stage_modules(self):
        for module in [seqkit_stats, seqkit_length_filter, make_fofn]:
            source = inspect.getsource(module)
            self.assertNotIn("app.stages.read_qc.nodes", source)
            self.assertNotIn("app.stages.read_qc.routing", source)

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
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
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
            "  shift 3\n"
            "  for input in \"$@\"; do\n"
            "    printf '%s\\tFASTQ\\tDNA\\t1\\t4\\t4\\t4.0\\t4\\t4\\t75.0\\t50.0\\t18.5\\n' \"$input\"\n"
            "  done\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"seq\" ] && [ \"$2\" = \"-m\" ]; then\n"
            "  shift 3\n"
            "  cat \"$@\"\n"
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
            self.assertIn("raw_long_reads", result["last_error"])

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

    def test_parent_graph_missing_raw_read_writes_operator_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
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
            state = initial_state(session_id="s1", input_manifest_ref="control/manifest.yaml", data_dir=str(v2_run_dir(data_dir, "s1")))
            state["current_stage"] = "read_qc"
            state["asset_registry_ref"] = str(registry)
            Ledger(state["data_dir"]).create_session("s1", "control/manifest.yaml")

            result = build_graph(backend="memory", data_dir=str(data_dir)).invoke(
                state,
                config={"configurable": {"thread_id": "s1"}},
            )

            self.assertEqual(result["supervision_state"], "failed_terminal")
            run_dir = Path(result["data_dir"])
            report_ref = run_dir / "reports" / "current_status.json"
            report = json.loads(report_ref.read_text(encoding="utf-8"))
            self.assertEqual(report["next_action"]["kind"], "missing_input")
            self.assertEqual(report["next_action"]["blocked_input_ref"], str(missing))
            summary = inspect_session("s1", run_dir)
            self.assertEqual(summary["next_action"]["kind"], "failed_terminal")
            artifact_index = run_dir / "artifacts" / "index.jsonl"
            artifact_types = (
                [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
                if artifact_index.exists()
                else []
            )
            self.assertNotIn("operator_report.failed_terminal", artifact_types)

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
            self.assertNotIn("frozen_attempt_ref", result["stage_outputs"]["read_qc"])
            self.assertNotIn("execution_result", result["stage_outputs"]["read_qc"])
            self.assertNotIn("stats_output_path", result["stage_outputs"]["read_qc"])

            artifact_index = Path(result["data_dir"]) / "artifacts" / "index.jsonl"
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
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_succeeded", event_types)
            self.assertIn("artifact_registered", event_types)

    def test_multi_file_quality_summary_weights_by_bases_before_read_count(self):
        from app.stages.read_qc.method_application.lifecycle import parse_seqkit_summary

        with tempfile.TemporaryDirectory() as tmpdir:
            stats_path = Path(tmpdir) / "seqkit_stats.tsv"
            stats_path.write_text(
                "file\tnum_seqs\tsum_len\tmin_len\tavg_len\tmax_len\tN50\tQ20(%)\tQ30(%)\tAvgQual\n"
                "long.fastq\t1\t1000\t1000\t1000\t1000\t1000\t90\t80\t30\n"
                "short.fastq\t9\t90\t10\t10\t10\t10\t10\t5\t12\n",
                encoding="utf-8",
            )

            summary = parse_seqkit_summary(stats_path)

        self.assertAlmostEqual(summary["q20_percent"], ((90 * 1000) + (10 * 90)) / 1090)
        self.assertAlmostEqual(summary["q30_percent"], ((80 * 1000) + (5 * 90)) / 1090)
        self.assertAlmostEqual(summary["avg_qual"], ((30 * 1000) + (12 * 90)) / 1090)

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
            self.assertEqual(read_qc["length_filter_method"], "skip")
            self.assertEqual(read_qc["length_filter_decision"], "skip filtering, use raw reads")
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
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("read_qc.long_reads.fofn", artifact_types)

    def test_multiple_raw_long_reads_write_multi_line_raw_fofn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq1 = Path(tmpdir) / "reads_1.fastq"
            fq2 = Path(tmpdir) / "reads_2.fastq"
            fq1.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fq2.write_text("@r2\nTGCA\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads_1.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq1),
                    },
                    {
                        "asset_id": "s1:raw_long_reads:reads_2.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq2),
                    },
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

            result = build_stage().invoke(state)

            read_qc = result["stage_outputs"]["read_qc"]
            self.assertEqual(read_qc["summary"]["files"], 2)
            self.assertEqual(read_qc["summary"]["num_seqs"], 2)
            self.assertEqual(read_qc["summary"]["sum_len"], 8)
            self.assertEqual(read_qc["input_asset_refs"], [
                "s1:raw_long_reads:reads_1.fastq",
                "s1:raw_long_reads:reads_2.fastq",
            ])
            fofn_ref = Path(read_qc["long_reads_fofn_ref"])
            self.assertEqual(fofn_ref.read_text(encoding="utf-8"), f"{fq1}\n{fq2}\n")
            stats_script = Path(result["data_dir"]) / "work" / "read_qc" / "seqkit_stats" / "run_seqkit_stats.sh"
            stats_script_text = stats_script.read_text(encoding="utf-8")
            self.assertIn(str(fq1), stats_script_text)
            self.assertIn(str(fq2), stats_script_text)

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
            self.assertEqual(read_qc["length_filter_method"], "read_qc.length_filter.seqkit_seq")
            self.assertEqual(read_qc["length_filter_decision"], "seqkit_seq")
            self.assertNotIn("length_filter_request", read_qc)
            filtered_ref = Path(read_qc["filtered_long_reads_ref"])
            self.assertTrue(filtered_ref.exists())
            self.assertEqual(filtered_ref.read_text(encoding="utf-8"), fq.read_text(encoding="utf-8"))
            fofn_ref = Path(read_qc["filtered_long_reads_fofn_ref"])
            self.assertTrue(fofn_ref.exists())
            self.assertEqual(fofn_ref.read_text(encoding="utf-8"), f"{filtered_ref}\n")
            self.assertEqual(read_qc["assembly_input_ref"], str(fofn_ref))

            filter_script = Path(result["data_dir"]) / "work" / "read_qc" / "length_filter" / "run_length_filter.sh"
            filter_script_text = filter_script.read_text(encoding="utf-8")
            self.assertIn("seqkit seq -m 1000", filter_script_text)
            self.assertIn("seqkit stats -a -T", filter_script_text)
            post_filter_summary_ref = Path(read_qc["post_filter_summary_artifact_ref"])
            self.assertTrue(post_filter_summary_ref.exists())
            post_filter_summary = json.loads(post_filter_summary_ref.read_text(encoding="utf-8"))
            self.assertEqual(post_filter_summary["read_set"], "post_filter")
            self.assertEqual(post_filter_summary["thresholds"], {"min_len": 1000})
            self.assertEqual(post_filter_summary["summary"], read_qc["summary"])
            self.assertEqual(read_qc["post_filter_summary"], read_qc["summary"])
            self.assertEqual(read_qc["summary_artifact_ref"], str(post_filter_summary_ref))
            self.assertEqual(read_qc["final_read_set"], "post_filter")
            self.assertIn("raw_summary_artifact_ref", read_qc)
            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("read_qc.filtered_long_reads", artifact_types)
            self.assertIn("read_qc.filtered_long_reads.fofn", artifact_types)

    def test_multiple_raw_long_reads_filter_to_multi_line_filtered_fofn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq1 = Path(tmpdir) / "reads_1.fastq"
            fq2 = Path(tmpdir) / "reads_2.fastq"
            fq1.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            fq2.write_text("@r2\nTGCA\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads_1.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq1),
                    },
                    {
                        "asset_id": "s1:raw_long_reads:reads_2.fastq",
                        "asset_type": "raw_long_reads",
                        "path": str(fq2),
                    },
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

            result = build_stage().invoke(state)

            read_qc = result["stage_outputs"]["read_qc"]
            filtered_refs = [Path(ref) for ref in read_qc["filtered_long_reads_refs"]]
            self.assertEqual([path.name for path in filtered_refs], [
                "filtered_long_reads_1.fastq",
                "filtered_long_reads_2.fastq",
            ])
            self.assertEqual(filtered_refs[0].read_text(encoding="utf-8"), fq1.read_text(encoding="utf-8"))
            self.assertEqual(filtered_refs[1].read_text(encoding="utf-8"), fq2.read_text(encoding="utf-8"))
            fofn_ref = Path(read_qc["filtered_long_reads_fofn_ref"])
            self.assertEqual(
                fofn_ref.read_text(encoding="utf-8"),
                "".join(f"{path}\n" for path in filtered_refs),
            )
            self.assertEqual(read_qc["assembly_input_ref"], str(fofn_ref))
            self.assertEqual(read_qc["substeps"]["length_filter"]["artifact_refs"], [str(path) for path in filtered_refs])

    def test_approved_min_length_filter_names_fasta_output_with_fasta_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fasta = Path(tmpdir) / "reads.fna"
            fasta.write_text(">r1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:reads.fna",
                        "asset_type": "raw_long_reads",
                        "path": str(fasta),
                        "format": "fasta",
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

            result = build_stage().invoke(state)

            filtered_ref = Path(result["stage_outputs"]["read_qc"]["filtered_long_reads_ref"])
            self.assertEqual(filtered_ref.name, "filtered_long_reads.fasta")
            self.assertEqual(filtered_ref.read_text(encoding="utf-8"), fasta.read_text(encoding="utf-8"))

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

    def test_non_seqkit_filter_selection_does_not_run_without_executable_route(self):
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
                        "method_name": "read_qc.length_filter.filtlong",
                        "thresholds": {"min_len": 1000},
                    },
                }
            }

            result = build_stage().invoke(state)

            read_qc = result["stage_outputs"]["read_qc"]
            self.assertEqual(read_qc["substeps"]["length_filter"]["status"], "skipped")
            self.assertEqual(read_qc["substeps"]["length_filter"]["skip_reason"], "unknown_filter_method")
            self.assertEqual(read_qc["length_filter_method"], "read_qc.length_filter.filtlong")
            self.assertEqual(read_qc["assembly_input_ref"], read_qc["long_reads_fofn_ref"])

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
            stats_dir = Path(result["data_dir"]) / "artifacts" / "read_qc"
            self.assertFalse((stats_dir / "seqkit_stats.tsv").exists())
            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
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
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
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
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
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

    def test_relative_data_dir_resolves_to_absolute_paths_in_generated_script(self):
        # Regression: when --data-dir is relative, the generated seqkit_stats
        # script writes its TSV to data_dir/artifacts/.../seqkit_stats.tsv.
        # The runner sets cwd to data_dir/work/.../seqkit_stats, so a raw
        # relative redirect target lands at a non-existent nested path. The
        # script must therefore embed an absolute artifact path.
        from app.stages.read_qc.method_application.seqkit_stats import freeze_seqkit_attempt

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fq = tmp / "BGI_CycloneSEQ_sample.fastq.gz"
            fq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            self._write_registry(
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

            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                run_dir = v2_run_dir(tmp / "out", "s1")
                state = {
                    "session_id": "s1",
                    "data_dir": "out/runs/s1",
                    "stage_outputs": {
                        "read_qc": {
                            "status": "planned",
                            "execution_mode": "local",
                            "raw_long_reads_path": str(fq),
                            "input_asset_ref": "s1:raw_long_reads:BGI_CycloneSEQ_sample.fastq.gz",
                            "substeps": {
                                "long_read_stats": {
                                    "name": "long_read_stats",
                                    "status": "planned",
                                    "method_name": "read_qc.long_read_stats.seqkit_stats",
                                }
                            },
                            # PATH bypass keeps env_dispatch happy without
                            # actually running anything.
                            "env": {"PATH": "/nonexistent"},
                        }
                    },
                }
                freeze_seqkit_attempt(state)
                script_path = run_dir / "work/read_qc/seqkit_stats/run_seqkit_stats.sh"
                self.assertTrue(script_path.exists())
                script_text = script_path.read_text(encoding="utf-8")
                expected_artifact = run_dir / "artifacts/read_qc/seqkit_stats.tsv"
                self.assertIn(str(expected_artifact), script_text)
                # And the script must NOT carry the bare relative form
                # that would resolve from the runner's cwd.
                self.assertNotIn(
                    " > out/runs/s1/artifacts/read_qc/seqkit_stats.tsv",
                    script_text,
                )
            finally:
                os.chdir(old_cwd)


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
            run_config = Path(tmpdir) / "read_qc_only_run_config.json"
            run_config.write_text(
                json.dumps(
                    {
                        "local_exec": ["read_qc"],
                        "requested_stages": ["read_qc"],
                    }
                ),
                encoding="utf-8",
            )
            argv = [
                "run",
                "--session-id", session_id,
                "--manifest", "tests/fixtures/phase4/read_qc_manifest.yaml",
                "--data-dir", tmpdir,
                "--run-config", str(run_config),
            ]
            rc = run_main(argv)
            self.assertEqual(rc, 0)

            artifact_dir = SessionPaths(tmpdir, session_id).run_dir / "artifacts" / "read_qc"
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
