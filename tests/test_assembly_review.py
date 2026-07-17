"""Tests for the assembly review stage.

Assembly review is the main human gate.  These tests verify that:
- Candidate disagreements do not auto-advance without policy.
- Rejected candidates remain traceable.
- Existing assembly inputs route through deterministically.
- Missing assemblies fail terminally.
- Human gates set blocked_human and active_interrupt.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.execution.evidence import load_frozen_attempt
from app.review.persistence import load_review_request, persist_review_request
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_COMPARISON,
    ASSEMBLY_REVIEW_PACKET,
    ReviewDecisionArtifact,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.assembly_review.builder import build_stage
from app.stages.assembly_review.method_application.busco import build_busco_graph
from app.stages.assembly_review.method_application.quast import build_quast_graph
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


def _request_record(result: dict) -> dict:
    return load_review_request(
        result["data_dir"], result["active_interrupt"]["request_ref"]
    )


def _request_payload(result: dict) -> dict:
    return _request_record(result)["payload"]


class AssemblyReviewDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_assembly_review_methods(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        expected = {
            "assembly_review.eval.quast": "quast",
            "assembly_review.eval.busco": "busco",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "assembly_review")
            self.assertIn(method_name, registry.by_stage["assembly_review"])


class AssemblyReviewStageTests(unittest.TestCase):
    def test_outer_graph_routes_to_metric_subgraphs(self):
        outer_nodes = set(build_stage().get_graph().nodes)
        quast_nodes = set(build_quast_graph().get_graph().nodes)
        busco_nodes = set(build_busco_graph().get_graph().nodes)

        self.assertIn("quast", outer_nodes)
        self.assertIn("busco", outer_nodes)
        self.assertIn("related_species_sanity_check", outer_nodes)
        self.assertNotIn("freeze_quast", outer_nodes)
        self.assertNotIn("run_busco", outer_nodes)
        for nodes in [quast_nodes, busco_nodes]:
            self.assertIn("freeze", nodes)
            self.assertIn("run", nodes)
            self.assertIn("collect", nodes)

    def test_metric_method_modules_do_not_depend_on_parent_stage_modules(self):
        for module_path in [
            Path("app/stages/assembly_review/method_application/quast.py"),
            Path("app/stages/assembly_review/method_application/busco.py"),
        ]:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            imports = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ]
            self.assertNotIn("app.stages.assembly_review.nodes", imports)
            self.assertNotIn("app.stages.assembly_review.routing", imports)

    def _build_state(
        self,
        *,
        session_id: str = "test-session",
        entry_mode: str | None = None,
        candidate_bundles: list[dict] | None = None,
        registry_ref: str | None = None,
        data_dir: str | None = None,
    ) -> dict:
        stage_outputs = {
            "inventory": {
                "status": "completed",
                "entry_mode": entry_mode,
            },
        }
        if candidate_bundles:
            stage_outputs["assembly"] = {
                "status": "completed",
                "candidate_bundles": candidate_bundles,
            }
        data_root = Path(data_dir) if data_dir is not None else Path(
            tempfile.mkdtemp(prefix="gene-agent-assembly-review-")
        )
        if data_dir is None:
            self.addCleanup(shutil.rmtree, data_root, True)
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(data_root, session_id)),
        )
        state["stage_outputs"] = stage_outputs
        if registry_ref:
            state["asset_registry_ref"] = registry_ref
        return state

    def _write_candidate_bundle(
        self,
        tmpdir: str,
        *,
        candidate_id: str,
        method_name: str,
        draft_assembly_fasta: str | None = None,
    ) -> Path:
        path = Path(tmpdir) / f"{candidate_id}.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": "test-session",
                    "candidate_id": candidate_id,
                    "method_name": method_name,
                    "status": "planned",
                    "artifact_type": "draft_assembly.candidate",
                    "input_ref": str(Path(tmpdir) / "long_reads.fofn"),
                    "input_type": "long_reads.fofn",
                    "expected_outputs": {
                        "draft_assembly_fasta": draft_assembly_fasta
                        or str(Path(tmpdir) / f"{candidate_id}.fasta")
                    },
                    "logs": {"primary_log": str(Path(tmpdir) / f"{candidate_id}.log")},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "test-session", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _fake_quast_bin(self, tmpdir: str, *, fail: bool = False) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        quast = bin_dir / "quast.py"
        if fail:
            quast.write_text(
                "#!/bin/sh\n"
                "echo fake quast failed >&2\n"
                "exit 9\n",
                encoding="utf-8",
            )
        else:
            quast.write_text(
                "#!/bin/sh\n"
                "outdir=\"\"\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"-o\" ]; then\n"
                "    shift\n"
                "    outdir=\"$1\"\n"
                "  fi\n"
                "  shift\n"
                "done\n"
                "mkdir -p \"$outdir\"\n"
                "printf 'Assembly\tassembly\n# contigs\t1\nLargest contig\t8\nTotal length\t8\nN50\t8\nL50\t1\nGC (%)\t50.00\n' > \"$outdir/report.tsv\"\n"
                "printf 'fake quast report\n' > \"$outdir/report.txt\"\n"
                "printf 'fake quast stdout\n'\n",
                encoding="utf-8",
            )
        quast.chmod(0o755)
        return bin_dir

    def _fake_busco_bin(self, tmpdir: str, *, fail: bool = False) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        busco = bin_dir / "busco"
        if fail:
            busco.write_text(
                "#!/bin/sh\n"
                "echo fake busco failed >&2\n"
                "exit 7\n",
                encoding="utf-8",
            )
        else:
            busco.write_text(
                "#!/bin/sh\n"
                "out=\"\"\n"
                "lineage=\"\"\n"
                "while [ $# -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    -o) shift; out=\"$1\" ;;\n"
                "    -l) shift; lineage=\"$1\" ;;\n"
                "  esac\n"
                "  shift\n"
                "done\n"
                "lineage_name=$(basename \"$lineage\")\n"
                "mkdir -p \"$out\"\n"
                "summary=\"$out/short_summary.specific.$lineage_name.$out.txt\"\n"
                "printf 'C:95.0%%[S:93.0%%,D:2.0%%],F:3.0%%,M:2.0%%,n:100\n10\tComplete BUSCOs (C)\n8\tComplete and single-copy BUSCOs (S)\n2\tComplete and duplicated BUSCOs (D)\n1\tFragmented BUSCOs (F)\n1\tMissing BUSCOs (M)\n100\tTotal BUSCO groups searched\n' > \"$summary\"\n"
                "printf 'fake busco stdout\n'\n",
                encoding="utf-8",
            )
        busco.chmod(0o755)
        return bin_dir

    def test_existing_assembly_blocks_for_human_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_fasta = Path(tmpdir) / "existing.fa"
            assembly_fasta.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "test-session:assembly_fasta_registered:existing.fa",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_fasta),
                    }
                ],
            )
            state = self._build_state(
                entry_mode="existing_assembly",
                registry_ref=str(registry),
                data_dir=tmpdir,
            )
            state["current_stage"] = "assembly_review"
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNotNone(result.get("active_interrupt"))
            self.assertEqual(result["active_interrupt"]["stage"], "assembly_review")
            request_payload = _request_payload(result)
            self.assertIn("accept", request_payload["decisions_allowed"])
            self.assertIn("rerun", request_payload["decisions_allowed"])
            self.assertIn("stop_human_review", request_payload["decisions_allowed"])
            self.assertNotIn("pass", request_payload["decisions_allowed"])
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["status"], "blocked_human")
            self.assertEqual(ar_out["candidates"][0]["candidate_id"], "existing_assembly")
            self.assertEqual(ar_out["candidates"][0]["final_assembly_ref"], str(assembly_fasta))
            self.assertFalse(ar_out["auto_decidable"])
            request_ref = SessionPaths.from_run_dir(result["data_dir"]).resolve_ref(
                result["active_interrupt"]["request_ref"]["path_ref"]
            )
            self.assertTrue(request_ref.exists())
            request_record = json.loads(request_ref.read_text(encoding="utf-8"))
            self.assertEqual(request_record["stage"], "assembly_review")
            self.assertEqual(request_record["payload"]["candidates"][0]["candidate_id"], "existing_assembly")
            self.assertFalse(request_record["payload"]["auto_decidable"])

    def test_review_decision_artifact_allows_post_review_actions(self):
        for decision in ["pass", "polish"]:
            artifact = ReviewDecisionArtifact(session_id="s1", stage="assembly_review", decision=decision)
            self.assertEqual(artifact.decision, decision)

    def test_no_candidates_fails_terminally(self):
        state = self._build_state(entry_mode="raw_inputs")
        graph = build_stage()
        result = graph.invoke(state)
        self.assertEqual(result["supervision_state"], "failed_terminal")
        self.assertIn("no_assembly_candidates_found", result.get("last_error", ""))

    def test_single_assembly_candidate_blocks_human(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            state["current_stage"] = "assembly_review"
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNotNone(result.get("active_interrupt"))
            self.assertEqual(result["active_interrupt"]["stage"], "assembly_review")
            self.assertIn("blocked_at", result["active_interrupt"])
            self.assertEqual(
                _request_payload(result)["decisions_allowed"],
                ["accept", "rerun", "stop_human_review"],
            )
            # current_stage stays at assembly_review while blocked
            self.assertEqual(result["current_stage"], "assembly_review")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["candidates"][0]["bundle_ref"], str(candidate_ref))
            self.assertEqual(ar_out["candidates"][0]["final_assembly_ref"], str(Path(tmpdir) / "flye_01.fasta"))
            self.assertTrue(Path(ar_out["eval_bundle_ref"]).exists())

    def test_default_review_planning_does_not_run_quast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            quast_signals = [
                signal
                for signal in result["stage_outputs"]["assembly_review"]["signals"]
                if signal["signal_name"] == "quast"
            ]
            self.assertEqual(quast_signals[0]["status"], "pending")
            events_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            event_types = [json.loads(line)["event_type"] for line in events_path.read_text().splitlines()]
            self.assertIn("review_requested", event_types)
            self.assertNotIn("attempt_started", event_types)

    def test_default_review_report_shows_missing_or_pending_signals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}

            result = build_stage().invoke(state)

            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertTrue(Path(ar_out["assembly_review_report_ref"]).exists())
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(encoding="utf-8")
            self.assertIn("quast (pending)", report_text)
            self.assertIn("busco (pending)", report_text)
            self.assertIn("No automatic biological thresholds are applied", report_text)
            report_data = json.loads(Path(ar_out["assembly_review_packet_ref"]).read_text(encoding="utf-8"))
            self.assertNotIn("recommended_candidate_id", report_data)
            self.assertNotIn("pass_fail", json.dumps(report_data))

    def test_local_quast_success_updates_signals_and_review_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["current_stage"] = "assembly_review"
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            quast_signal = next(signal for signal in ar_out["signals"] if signal["signal_name"] == "quast")
            self.assertEqual(quast_signal["status"], "available")
            self.assertTrue(quast_signal["metric_refs"])
            self.assertEqual(quast_signal["metadata"]["metrics"]["n50"], 8)
            self.assertEqual(quast_signal["metadata"]["metrics"]["total_length"], 8)
            request_payload = _request_payload(result)
            request_quast = next(signal for signal in request_payload["signals"] if signal["signal_name"] == "quast")
            self.assertEqual(request_quast["status"], "available")
            self.assertEqual(request_quast["candidate_id"], "flye_01")
            self.assertTrue(all(signal.get("candidate_id") for signal in request_payload["signals"]))
            self.assertRegex(
                ar_out["eval_bundle_ref"],
                r"assembly_eval_bundle\.evaluating\.[0-9a-f]{16}\.json$",
            )
            self.assertEqual(
                len(
                    list(
                        (Path(result["data_dir"]) / "artifacts" / "assembly_review").glob(
                            "assembly_eval_bundle.planned.*.json"
                        )
                    )
                ),
                1,
            )
            self.assertTrue(Path(ar_out["assembly_review_report_ref"]).exists())
            self.assertTrue(Path(ar_out["assembly_review_packet_ref"]).exists())
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(encoding="utf-8")
            self.assertIn("flye_01", report_text)
            self.assertIn("N50", report_text)
            self.assertIn("Total length", report_text)
            self.assertIn("Largest contig", report_text)
            self.assertIn("busco", report_text)
            self.assertIn("thresholds_not_applied_without_human_approval", report_text)
            report_data = json.loads(Path(ar_out["assembly_review_packet_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(report_data["candidates"][0]["metrics"]["quast"]["n50"], 8)
            self.assertEqual(report_data["candidates"][0]["signals"]["missing"], ["busco"])
            paths = SessionPaths.from_run_dir(result["data_dir"])
            self.assertEqual(
                request_payload["report_refs"]["assembly_review_report"],
                paths.to_ref(ar_out["assembly_review_report_ref"]),
            )

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("assembly_review.quast_report_tsv", artifact_types)
            self.assertIn("assembly_review.quast_report_txt", artifact_types)
            self.assertNotIn("assembly_review.report", artifact_types)
            self.assertIn(ASSEMBLY_REVIEW_PACKET, artifact_types)
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_succeeded", event_types)
            self.assertIn("artifact_registered", event_types)
            self.assertIn("review_requested", event_types)

    def test_local_quast_success_writes_quast_summary_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            quast_signal = next(signal for signal in ar_out["signals"] if signal["signal_name"] == "quast")
            summary_ref = quast_signal["metadata"].get("quast_summary_ref")
            self.assertTrue(summary_ref, "QUAST success must expose quast_summary_ref on signal metadata")
            summary_path = Path(summary_ref)
            self.assertTrue(summary_path.exists())
            summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["candidate_id"], "flye_01")
            self.assertEqual(summary_payload["method_name"], "assembly_review.eval.quast")
            self.assertEqual(summary_payload["metrics"]["n50"], 8)
            self.assertEqual(summary_payload["metrics"]["total_length"], 8)
            self.assertIn(
                "thresholds_not_applied_without_human_approval",
                summary_payload["notes"],
            )
            # The summary path must also appear on the signal's metric_refs
            # so review consumers see it alongside the raw report.
            self.assertIn(str(summary_path), quast_signal["metric_refs"])

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("assembly_review.quast_summary", artifact_types)
            self.assertIn("assembly_review.quast_report_tsv", artifact_types)

    def test_local_busco_success_updates_signals_and_review_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            lineage = Path(tmpdir) / "embryophyta_odb10"
            lineage.mkdir()
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["current_stage"] = "assembly_review"
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
                "busco_parameters": {
                    "lineage_path": str(lineage),
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "operator_offline_cache",
                    "threads": 1,
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            busco_signal = next(signal for signal in ar_out["signals"] if signal["signal_name"] == "busco")
            self.assertEqual(busco_signal["status"], "available")
            self.assertTrue(busco_signal["metric_refs"])
            self.assertEqual(busco_signal["metadata"]["metrics"]["complete_percent"], 95.0)
            self.assertEqual(busco_signal["metadata"]["metrics"]["total_busco_groups_searched"], 100)
            self.assertEqual(busco_signal["metadata"]["provenance"]["lineage_name"], "embryophyta_odb10")
            request_payload = _request_payload(result)
            request_busco = next(signal for signal in request_payload["signals"] if signal["signal_name"] == "busco")
            self.assertEqual(request_busco["status"], "available")
            self.assertEqual(request_busco["candidate_id"], "flye_01")
            self.assertTrue(request_payload["auto_decidable"])
            report_data = json.loads(Path(ar_out["assembly_review_packet_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(report_data["candidates"][0]["metrics"]["busco"]["complete_percent"], 95.0)
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(encoding="utf-8")
            self.assertIn("95.0", report_text)
            self.assertNotIn("fake busco stdout", report_text)

            summary_ref = busco_signal["metadata"].get("busco_summary_ref")
            self.assertTrue(summary_ref)
            summary_payload = json.loads(Path(summary_ref).read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["lineage"]["lineage_source"], "operator_offline_cache")
            self.assertIn(str(summary_ref), busco_signal["metric_refs"])

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("assembly_review.busco_short_summary", artifact_types)
            self.assertIn("assembly_review.busco_summary", artifact_types)

    def test_local_busco_missing_lineage_marks_signal_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            busco_signal = next(signal for signal in ar_out["signals"] if signal["signal_name"] == "busco")
            self.assertEqual(busco_signal["status"], "missing")
            self.assertIn("busco_lineage_not_supplied", busco_signal["notes"])
            self.assertFalse(ar_out["auto_decidable"])
            request_payload = _request_payload(result)
            self.assertFalse(request_payload["auto_decidable"])

    def test_local_busco_failure_marks_signal_failed_and_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            lineage = Path(tmpdir) / "embryophyta_odb10"
            lineage.mkdir()
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir, fail=True)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
                "busco_parameters": {
                    "lineage_path": str(lineage),
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "operator_offline_cache",
                    "threads": 1,
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            busco_signal = next(
                signal for signal in result["stage_outputs"]["assembly_review"]["signals"] if signal["signal_name"] == "busco"
            )
            self.assertEqual(busco_signal["status"], "failed")
            self.assertIn("candidate_not_rejected_from_metrics", busco_signal["notes"])
            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)

    def test_auto_decidable_requires_quast_and_busco(self):
        from app.stages.assembly_review.nodes import _assembly_review_auto_decidable

        quast_only = {
            "candidates": [{"candidate_id": "flye_01"}],
            "signals": [
                {"signal_name": "quast", "candidate_id": "flye_01", "status": "available", "metric_refs": ["/q.tsv"]},
                {"signal_name": "busco", "candidate_id": "flye_01", "status": "missing", "metric_refs": []},
            ],
        }
        quast_busco = {
            "candidates": [{"candidate_id": "flye_01"}],
            "signals": [
                {"signal_name": "quast", "candidate_id": "flye_01", "status": "available", "metric_refs": ["/q.tsv"]},
                {"signal_name": "busco", "candidate_id": "flye_01", "status": "available", "metric_refs": ["/b.json"]},
            ],
        }
        self.assertFalse(_assembly_review_auto_decidable(quast_only))
        self.assertTrue(_assembly_review_auto_decidable(quast_busco))

    def test_local_quast_failure_marks_signal_failed_and_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir, fail=True)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "draft_ready", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            quast_signal = next(
                signal for signal in result["stage_outputs"]["assembly_review"]["signals"] if signal["signal_name"] == "quast"
            )
            self.assertEqual(quast_signal["status"], "failed")
            self.assertIn("candidate_not_rejected_from_metrics", quast_signal["notes"])
            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_failed", event_types)
            self.assertIn("review_requested", event_types)
            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            self.assertEqual(
                sum(event["artifact_type"] == "command.stdout" for event in artifact_events),
                1,
            )
            self.assertEqual(
                sum(event["artifact_type"] == "command.stderr" for event in artifact_events),
                1,
            )

    def test_local_quast_missing_draft_marks_signal_missing_without_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(Path(tmpdir) / "missing.fasta"),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "planned", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            quast_signal = next(
                signal for signal in result["stage_outputs"]["assembly_review"]["signals"] if signal["signal_name"] == "quast"
            )
            self.assertEqual(quast_signal["status"], "missing")
            events_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            event_types = [json.loads(line)["event_type"] for line in events_path.read_text().splitlines()]
            self.assertIn("review_requested", event_types)
            self.assertNotIn("attempt_started", event_types)

    def test_multiple_candidates_block_human(self):
        state = self._build_state(
            entry_mode="raw_inputs",
            candidate_bundles=[
                {
                    "candidate_id": "flye_01",
                    "method_name": "flye",
                    "status": "planned",
                    "candidate_ref": "ref/flye",
                },
                {
                    "candidate_id": "hifiasm_01",
                    "method_name": "hifiasm",
                    "status": "planned",
                    "candidate_ref": "ref/hifiasm",
                },
            ],
        )
        graph = build_stage()
        result = graph.invoke(state)
        self.assertEqual(result["supervision_state"], "blocked_human")
        ar_out = result["stage_outputs"]["assembly_review"]
        self.assertEqual(ar_out["candidate_count"], 2)

    def test_rejected_candidates_remain_traceable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_state(
                entry_mode="raw_inputs",
                candidate_bundles=[
                    {
                        "candidate_id": "flye_01",
                        "method_name": "flye",
                        "status": "planned",
                        "candidate_ref": "ref/flye",
                    },
                    {
                        "candidate_id": "canu_01",
                        "method_name": "canu",
                        "status": "planned",
                        "candidate_ref": "ref/canu",
                    },
                ],
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            candidate_ids = [c["candidate_id"] for c in ar_out["candidates"]]
            self.assertIn("flye_01", candidate_ids)
            self.assertIn("canu_01", candidate_ids)

    def test_does_not_auto_advance_from_single_metric(self):
        """A single candidate must not be auto-accepted without human policy."""
        state = self._build_state(
            entry_mode="raw_inputs",
            candidate_bundles=[
                {
                    "candidate_id": "flye_01",
                    "method_name": "flye",
                    "status": "planned",
                    "candidate_ref": "ref/flye",
                },
            ],
        )
        state["current_stage"] = "assembly_review"
        graph = build_stage()
        result = graph.invoke(state)
        # Must NOT advance to next stage automatically
        self.assertEqual(result["current_stage"], "assembly_review")
        self.assertEqual(result["supervision_state"], "blocked_human")

    def test_planned_signals_include_quast_and_busco(self):
        state = self._build_state(
            entry_mode="raw_inputs",
            candidate_bundles=[
                {
                    "candidate_id": "flye_01",
                    "method_name": "flye",
                    "status": "planned",
                },
            ],
        )
        graph = build_stage()
        result = graph.invoke(state)
        ar_out = result["stage_outputs"]["assembly_review"]
        planned = ar_out.get("planned_signals", [])
        self.assertIn("quast", planned)
        self.assertIn("busco", planned)

    def test_planned_signals_include_merqury_when_short_reads_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            short_reads = Path(tmpdir) / "short.fq"
            long_reads = Path(tmpdir) / "long.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "test-session:clean_short_dna_reads:short.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_reads),
                    },
                    {
                        "asset_id": "test-session:clean_long_reads:long.fq",
                        "asset_type": "clean_long_reads",
                        "path": str(long_reads),
                    },
                ],
            )
            state = self._build_state(entry_mode="raw_inputs", registry_ref=str(registry), data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            graph = build_stage()
            result = graph.invoke(state)
            ar_out = result["stage_outputs"]["assembly_review"]
            planned = ar_out.get("planned_signals", [])
            self.assertEqual(planned, ["quast", "busco"])
            self.assertNotIn("merqury", planned)
            self.assertNotIn("mapping_coverage", planned)
            self.assertNotIn("purge_dups_check", planned)
            eval_payload = json.loads(Path(ar_out["eval_bundle_ref"]).read_text(encoding="utf-8"))
            self.assertIn("clean_short_dna_reads", eval_payload["available_inputs"])

    def test_existing_assembly_writes_review_requested_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_fasta = Path(tmpdir) / "existing.fa"
            assembly_fasta.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "test-session:assembly_fasta_registered:existing.fa",
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_fasta),
                    }
                ],
            )
            state = self._build_state(
                entry_mode="existing_assembly",
                registry_ref=str(registry),
                data_dir=tmpdir,
            )
            state["current_stage"] = "assembly_review"
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "blocked_human")
            events_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            requested_events = [e for e in events if e["event_type"] == "review_requested"]
            decision_events = [e for e in events if e["event_type"] == "review_decision"]
            self.assertEqual(len(requested_events), 1)
            self.assertEqual(requested_events[0]["stage"], "assembly_review")
            self.assertEqual(_request_payload(result)["candidates"][0]["candidate_id"], "existing_assembly")
            self.assertFalse(requested_events[0]["summary"]["auto_decidable"])
            self.assertEqual(decision_events, [])

    def test_human_gate_writes_review_requested_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_state(
                entry_mode="raw_inputs",
                candidate_bundles=[
                    {
                        "candidate_id": "flye_01",
                        "method_name": "flye",
                        "status": "planned",
                    },
                ],
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            events_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            request_events = [e for e in events if e["event_type"] == "review_requested"]
            self.assertEqual(len(request_events), 1)
            self.assertEqual(request_events[0]["stage"], "assembly_review")
            self.assertFalse(request_events[0]["summary"]["auto_decidable"])
            self.assertEqual(request_events[0]["summary"]["policy_mode"], "strict")
            self.assertTrue(
                any(ref.get("role") == "review_request_artifact_ref" for ref in request_events[0]["refs"])
            )
            request_record = _request_record(result)
            self.assertEqual(request_record["review_request_id"], result["active_interrupt"]["review_request_id"])

    def test_auto_policy_recommender_failure_falls_back_to_blocked_human(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "planned", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {"review_policy": {"mode": "auto"}}
            with patch("app.stages.assembly_review.nodes._assembly_review_auto_decidable", return_value=True), \
                 patch(
                     "app.stages.assembly_review.nodes.recommend_review_decision",
                     side_effect=ValueError("bad llm"),
                 ) as mock_recommend:
                result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNotNone(result.get("active_interrupt"))
            recommend_kwargs = mock_recommend.call_args.kwargs
            self.assertNotIn("request", recommend_kwargs)
            self.assertEqual(
                recommend_kwargs["review_request_id"],
                result["active_interrupt"]["review_request_id"],
            )
            self.assertEqual(
                recommend_kwargs["request_hash"],
                result["active_interrupt"]["request_hash"],
            )
            self.assertEqual(
                recommend_kwargs["request_ref"].model_dump(mode="json"),
                result["active_interrupt"]["request_ref"],
            )
            events = [json.loads(line) for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()]
            self.assertIn("auto_recommendation_failed", [event["event_type"] for event in events])

    def test_pending_contamination_signal_does_not_disable_auto_decidable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "planned", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {"review_policy": {"mode": "auto"}}
            ar_out = {
                "candidates": [{"candidate_id": "flye_01"}],
                "signals": [
                    {
                        "signal_name": "quast",
                        "candidate_id": "flye_01",
                        "status": "available",
                        "metric_refs": ["/tmp/quast/report.tsv"],
                    },
                    {
                        "signal_name": "busco",
                        "candidate_id": "flye_01",
                        "status": "available",
                        "metric_refs": ["/tmp/busco/summary.json"],
                    },
                    {
                        "signal_name": "contamination_check",
                        "candidate_id": "flye_01",
                        "status": "pending",
                        "metric_refs": [],
                    },
                ],
            }
            from app.stages.assembly_review.nodes import _assembly_review_auto_decidable
            self.assertTrue(_assembly_review_auto_decidable(ar_out))

    def test_auto_decidable_false_when_no_available_eval_signal(self):
        from app.stages.assembly_review.nodes import _assembly_review_auto_decidable

        ar_out_pending = {
            "candidates": [{"candidate_id": "flye_01"}],
            "signals": [
                {"signal_name": "quast", "candidate_id": "flye_01", "status": "pending", "metric_refs": []},
            ],
        }
        ar_out_no_metrics = {
            "candidates": [{"candidate_id": "flye_01"}],
            "signals": [
                {"signal_name": "quast", "candidate_id": "flye_01", "status": "available", "metric_refs": []},
            ],
        }
        ar_out_no_candidates = {
            "candidates": [],
            "signals": [
                {"signal_name": "quast", "candidate_id": "flye_01", "status": "available", "metric_refs": ["/x.tsv"]},
            ],
        }
        self.assertFalse(_assembly_review_auto_decidable(ar_out_pending))
        self.assertFalse(_assembly_review_auto_decidable(ar_out_no_metrics))
        self.assertFalse(_assembly_review_auto_decidable(ar_out_no_candidates))

    def test_auto_decidable_false_does_not_call_recommender(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "planned", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {"review_policy": {"mode": "auto"}}
            with patch("app.stages.assembly_review.nodes._assembly_review_auto_decidable", return_value=False), \
                 patch("app.stages.assembly_review.nodes.recommend_review_decision") as mock_rec:
                result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertFalse(_request_payload(result)["auto_decidable"])
            self.assertTrue(result["stage_outputs"]["assembly_review"]["pending_human_confirmation"])
            mock_rec.assert_not_called()

    def test_auto_resume_validation_failure_falls_back_to_blocked_human(self):
        from app.control.lifecycle import ResumeValidationError
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {"status": "planned", "selected_candidate_refs": [str(candidate_ref)]}
            state["stage_outputs"]["assembly_review"] = {"review_policy": {"mode": "auto"}}
            rec = ResumeReviewRequest(
                session_id="test-session",
                stage="assembly_review",
                decision="accept",
                candidate_id="flye_01",
                decided_by="llm_provisional",
                rationale_artifact_ref="/tmp/rationale.json",
            )
            with patch("app.stages.assembly_review.nodes._assembly_review_auto_decidable", return_value=True), \
                 patch("app.stages.assembly_review.nodes.recommend_review_decision", return_value=rec), \
                 patch("app.control.lifecycle.resume_review_state", side_effect=ResumeValidationError("synthetic")):
                result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            events = [json.loads(line) for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl").read_text().splitlines()]
            failed_events = [e for e in events if e["event_type"] == "auto_recommendation_failed"]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["summary"]["error_type"], "ResumeValidationError")

    def test_assembly_stage_executed_candidate_ref_points_to_real_fasta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "flye_draft.fasta"
            draft.write_text(">contig1\nACGT\n", encoding="utf-8")
            flye_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="test-session:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(flye_ref)],
                "draft_assembly_ref": str(draft),
                "executed_candidate_refs": [str(flye_ref)],
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            candidate = result["stage_outputs"]["assembly_review"]["candidates"][0]
            self.assertEqual(candidate["candidate_id"], "test-session:assembly.candidate.flye")
            self.assertEqual(candidate["final_assembly_ref"], str(draft))
            self.assertTrue(Path(candidate["final_assembly_ref"]).exists())

    def test_consumes_assembly_stage_selected_candidate_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            flye_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="test-session:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
            )
            hifiasm_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="test-session:assembly.candidate.hifiasm",
                method_name="assembly.candidate.hifiasm",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(flye_ref), str(hifiasm_ref)],
                "rejected_candidates": {"assembly.candidate.canu": "genome_size_required"},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["candidate_count"], 2)
            candidate_ids = {candidate["candidate_id"] for candidate in ar_out["candidates"]}
            self.assertEqual(
                candidate_ids,
                {"test-session:assembly.candidate.flye", "test-session:assembly.candidate.hifiasm"},
            )
            self.assertEqual(
                ar_out["rejected_candidates"],
                {"assembly.candidate.canu": "genome_size_required"},
            )

    def test_relative_data_dir_resolves_to_absolute_paths_in_quast_script(self):
        # Regression: freeze_quast_attempts embeds data_dir-derived paths into
        # the generated shell script. When data_dir is relative the runner's
        # cwd (nested under data_dir) would make those paths resolve to a
        # non-existent nested location. The script must embed absolute paths.
        import os
        from unittest.mock import patch

        from app.execution.env_resolver import EnvSpec
        from app.stages.assembly_review.method_application.quast import (
            freeze_quast_attempts,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            draft = tmp / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="s1:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                run_dir = v2_run_dir(tmp / "out", "s1")
                state = self._build_state(
                    session_id="s1",
                    entry_mode="raw_inputs",
                    data_dir=str(tmp / "out"),
                )
                state["data_dir"] = "out/runs/s1"
                state["stage_outputs"]["assembly"] = {
                    "status": "draft_ready",
                    "selected_candidate_refs": [str(candidate_ref)],
                }
                state["stage_outputs"]["assembly_review"] = {
                    "execution_mode": "local",
                    "env": {"PATH": "/nonexistent"},
                    "quast_parameters": {"threads": 1},
                    "candidates": [
                        {
                            "candidate_id": "s1:assembly.candidate.flye",
                            "method_name": "assembly.candidate.flye",
                            "status": "planned",
                            "final_assembly_ref": str(draft),
                        }
                    ],
                }
                with patch(
                    "app.stages.assembly_review.method_application.quast.review_methods_error",
                    return_value=None,
                ), patch(
                    "app.stages.assembly_review.method_application.quast.resolve_review_method_env",
                    return_value=(EnvSpec(conda_env="env_geno_exec"), [], None),
                ):
                    result = freeze_quast_attempts(state)
                attempts = result.get("stage_outputs", {}).get("assembly_review", {}).get("quast_frozen_attempt_refs", [])
                self.assertTrue(attempts, "expected at least one frozen attempt")
                attempt = load_frozen_attempt(
                    {**state, **result},
                    attempts[0],
                    expected_stage="assembly_review",
                    expected_method_name="assembly_review.eval.quast",
                )
                script_path = attempt.script_path
                self.assertTrue(script_path.exists(), f"script not found: {script_path}")
                script_text = script_path.read_text(encoding="utf-8")
                expected_out_dir = run_dir / "artifacts" / "assembly_review" / "quast"
                self.assertIn(str(expected_out_dir), script_text)
                self.assertNotIn(" out/runs/s1/artifacts/", script_text)
            finally:
                os.chdir(old_cwd)


class AssemblyReviewSchemaTests(unittest.TestCase):
    def test_assembly_review_plan_has_no_threshold_defaults(self):
        from app.schemas.plan import AssemblyReviewPlan

        plan = AssemblyReviewPlan(status="planned")
        self.assertEqual(plan.decision, "pending")
        self.assertEqual(plan.planned_signals, [])
        # Thresholds are intentionally not part of the plan schema;
        # they require human approval before use.

    def test_review_decision_artifact_allows_all_decision_types(self):
        from app.schemas.artifacts import ReviewDecisionArtifact

        for decision in [
            "accept",
            "pass",
            "rerun",
            "purge",
            "polish",
            "continue_low_confidence",
            "stop_human_review",
        ]:
            with self.subTest(decision=decision):
                artifact = ReviewDecisionArtifact(
                    session_id="s1",
                    stage="assembly_review",
                    decision=decision,
                )
                self.assertEqual(artifact.decision, decision)

    def test_review_payload_includes_all_allowed_decisions(self):
        from app.review.base import ReviewPayload

        payload = ReviewPayload(
            stage="assembly_review",
            reason="test",
            decisions_allowed=["pass", "accept", "purge", "polish", "rerun", "stop_human_review"],
        )
        self.assertIn("pass", payload.decisions_allowed)
        self.assertIn("accept", payload.decisions_allowed)
        self.assertIn("purge", payload.decisions_allowed)
        self.assertIn("polish", payload.decisions_allowed)
        self.assertIn("rerun", payload.decisions_allowed)
        self.assertIn("stop_human_review", payload.decisions_allowed)


class AssemblyRerunTests(unittest.TestCase):
    """Tests for the assembly parameter loop (rerun from review gate)."""

    def _build_blocked_state(
        self,
        tmpdir: str,
        *,
        rerun_count: int = 0,
        persist_request: bool = True,
    ) -> dict:
        """Build a state that is blocked at the assembly_review human gate with a Flye candidate."""
        from app.stages.assembly_review.nodes import human_review_gate

        session_id = "test-rerun-session"
        assembly_fasta = Path(tmpdir) / "draft.fasta"
        assembly_fasta.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        reads = Path(tmpdir) / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
        fofn = Path(tmpdir) / "long_reads.fofn"
        fofn.write_text(str(reads) + "\n", encoding="utf-8")

        candidate_id = f"{session_id}:assembly.candidate.flye"
        bundle_path = Path(tmpdir) / "flye_candidate.json"
        bundle_path.write_text(json.dumps({
            "session_id": session_id,
            "candidate_id": candidate_id,
            "method_name": "assembly.candidate.flye",
            "status": "executed",
            "artifact_type": "draft_assembly.candidate",
            "input_ref": str(fofn),
            "input_type": "long_reads.fofn",
            "expected_outputs": {"draft_assembly_fasta": str(assembly_fasta)},
            "logs": {},
            "metadata": {
                "execution_enabled": True,
                "read_profile": "nano-raw",
                "estimated_genome_size": 100000000,
                "threads": 4,
            },
        }, indent=2), encoding="utf-8")

        selected_refs = [str(bundle_path)]
        for i in range(rerun_count):
            rerun_bundle = Path(tmpdir) / f"flye_rerun_{i+1}_candidate.json"
            rerun_bundle.write_text(json.dumps({
                "session_id": session_id,
                "candidate_id": f"{session_id}:assembly.candidate.flye:rerun_{i+1}",
                "method_name": "assembly.candidate.flye",
                "status": "executed",
                "artifact_type": "draft_assembly.candidate",
                "input_ref": str(fofn),
                "input_type": "long_reads.fofn",
                "expected_outputs": {"draft_assembly_fasta": str(assembly_fasta)},
                "logs": {},
                "metadata": {"rerun_index": i + 1},
            }, indent=2), encoding="utf-8")
            selected_refs.append(str(rerun_bundle))

        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, session_id)),
        )
        state["stage_outputs"] = {
            "assembly": {
                "status": "completed",
                "input_ref": str(fofn),
                "input_type": "long_reads.fofn",
                "selected_candidate_refs": selected_refs,
                "flye_parameters": {
                    "read_type": "nano-raw",
                    "estimated_genome_size": 100000000,
                    "threads": 4,
                },
            },
            "assembly_review": {
                "status": "blocked_human",
                "execution_mode": "local",
                "env": {},
                "quast_parameters": {"threads": 1},
                "busco_parameters": {},
                "candidates": [{
                    "candidate_id": candidate_id,
                    "method_name": "assembly.candidate.flye",
                    "source": "assembly_stage",
                    "bundle_ref": str(bundle_path),
                    "final_assembly_ref": str(assembly_fasta),
                }],
            },
        }
        if not persist_request:
            return state
        blocked = human_review_gate(state)
        return {**state, **blocked}

    @staticmethod
    def _review_binding(state: dict) -> dict:
        return {
            "gate_id": state["active_interrupt"]["gate_id"],
            "review_request_id": state["active_interrupt"]["review_request_id"],
            "review_request_hash": state["active_interrupt"]["request_hash"],
        }

    def _fake_flye_bin(self, tmpdir: str, *, fail: bool = False, name: str = "bin") -> Path:
        bin_dir = Path(tmpdir) / name
        bin_dir.mkdir(exist_ok=True)
        flye = bin_dir / "flye"
        if fail:
            flye.write_text("#!/bin/sh\necho flye failed >&2\nexit 1\n", encoding="utf-8")
        else:
            flye.write_text(
                "#!/bin/sh\n"
                "outdir=\"\"\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = \"--out-dir\" ]; then shift; outdir=\"$1\"; fi\n"
                "  shift\n"
                "done\n"
                "mkdir -p \"$outdir\"\n"
                "printf '>ctg1\\nACGTACGT\\n' > \"$outdir/assembly.fasta\"\n",
                encoding="utf-8",
            )
        flye.chmod(0o755)
        return bin_dir

    def _rerun_request(self, state: dict, *, genome_size: int = 200000000, threads: int = 2):
        from app.review.base import ResumeReviewRequest

        return ResumeReviewRequest(
            session_id=state["session_id"],
            stage="assembly_review",
            decision="rerun",
            candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
            metadata={
                "rerun_parameters": {
                    "read_type": "nano-raw",
                    "estimated_genome_size": genome_size,
                    "threads": threads,
                },
                "rerun_reason": "try larger genome size",
            },
            **self._review_binding(state),
        )

    def _renew_review_request(self, state: dict, marker: str) -> dict:
        """Model a new human-gate cycle after the prior request was decided."""
        from app.control.ledger import Ledger

        current = load_review_request(state["data_dir"], state["active_interrupt"]["request_ref"])
        payload = dict(current["payload"])
        payload["notes"] = [*list(payload.get("notes") or []), marker]
        persisted = persist_review_request(
            state,
            stage="assembly_review",
            gate_id="assembly_review",
            reason=current["reason"],
            payload=payload,
            method_name="assembly_review.contract",
            artifact_type="assembly_review.request",
        )
        updated = {
            **state,
            "supervision_state": "blocked_human",
            "active_interrupt": persisted.compact_interrupt(
                stage="assembly_review",
                gate_id="assembly_review",
                reason=current["reason"],
            ),
        }
        Ledger(state["data_dir"]).record_review_requested(
            state["session_id"],
            "assembly_review",
            reason=current["reason"],
            candidates=payload.get("candidates") or [],
            review_request_artifact_ref=persisted.request_ref.model_dump(mode="json"),
            review_request_id=persisted.review_request_id,
        )
        return updated

    def test_rerun_decision_validates_flye_parameters(self):
        """Rerun with missing or invalid parameters is rejected."""
        from app.control.lifecycle import ResumeValidationError, resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir)
            bad_cases = [
                {},
                {"read_type": "invalid", "estimated_genome_size": 100, "threads": 4},
                {"read_type": "nano-raw", "estimated_genome_size": -1, "threads": 4},
                {"read_type": "nano-raw", "estimated_genome_size": 100, "threads": 0},
            ]
            for params in bad_cases:
                request = ResumeReviewRequest(
                    session_id=state["session_id"],
                    stage="assembly_review",
                    decision="rerun",
                    candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
                    metadata={"rerun_parameters": params},
                    **self._review_binding(state),
                )
                with self.assertRaises(ResumeValidationError):
                    resume_review_state(state, request)

    def test_rerun_requires_rerun_parameters_in_metadata(self):
        """Rerun without metadata.rerun_parameters is rejected."""
        from app.control.lifecycle import ResumeValidationError, resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir)
            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="rerun",
                candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
                metadata={},
                **self._review_binding(state),
            )
            with self.assertRaises(ResumeValidationError):
                resume_review_state(state, request)

    def test_rerun_creates_distinct_candidate_with_provenance(self):
        """Rerun produces a new candidate; original candidate is preserved."""
        from app.control.lifecycle import resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = self._fake_flye_bin(tmpdir)
            state = self._build_blocked_state(tmpdir)
            original_refs = list(state["stage_outputs"]["assembly"]["selected_candidate_refs"])

            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="rerun",
                candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
                metadata={
                    "rerun_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 200000000,
                        "threads": 2,
                    },
                    "rerun_reason": "try larger genome size",
                },
                **self._review_binding(state),
            )
            with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                update = resume_review_state(state, request)

            self.assertEqual(update["current_stage"], "assembly_review")
            self.assertEqual(update["supervision_state"], "running_stage")
            self.assertIsNone(update.get("active_interrupt"))

            new_refs = update["stage_outputs"]["assembly"]["selected_candidate_refs"]
            self.assertEqual(len(new_refs), len(original_refs) + 1)
            for ref in original_refs:
                self.assertIn(ref, new_refs)

            new_bundle_path = Path(new_refs[-1])
            self.assertTrue(new_bundle_path.exists())
            bundle = json.loads(new_bundle_path.read_text(encoding="utf-8"))
            self.assertIn("rerun_1", bundle["candidate_id"])
            self.assertEqual(bundle["metadata"]["estimated_genome_size"], 200000000)
            self.assertEqual(bundle["metadata"]["rerun_index"], 1)
            self.assertNotIn("execution_mode", bundle["metadata"])
            self.assertEqual(bundle["status"], "executed")

    def test_rerun_replay_reuses_existing_completed_bundle_without_side_effects(self):
        """Replaying stale checkpoint state reuses the completed rerun bundle."""
        from app.control.ledger import Ledger
        from app.control.lifecycle import resume_review_state
        from app.execution.artifacts import read_artifact_records

        with tempfile.TemporaryDirectory() as tmpdir:
            success_bin = self._fake_flye_bin(tmpdir, name="bin_success")
            fail_bin = self._fake_flye_bin(tmpdir, fail=True, name="bin_fail")
            state = self._build_blocked_state(tmpdir)
            request = self._rerun_request(state)

            with patch.dict(os.environ, {"PATH": f"{success_bin}:{os.environ['PATH']}"}):
                first_update = resume_review_state(state, request)

            first_refs = first_update["stage_outputs"]["assembly"]["selected_candidate_refs"]
            rerun_ref = first_refs[-1]
            self.assertTrue(Path(rerun_ref).exists())
            artifacts_before = read_artifact_records(state["data_dir"], state["session_id"])
            events_before = Ledger(state["data_dir"]).read_events(state["session_id"])

            with patch.dict(os.environ, {"PATH": f"{fail_bin}:{os.environ['PATH']}"}):
                replay_update = resume_review_state(state, request)

            replay_refs = replay_update["stage_outputs"]["assembly"]["selected_candidate_refs"]
            self.assertEqual(replay_refs, first_refs)
            self.assertEqual(replay_refs.count(rerun_ref), 1)
            self.assertEqual(read_artifact_records(state["data_dir"], state["session_id"]), artifacts_before)
            self.assertEqual(Ledger(state["data_dir"]).read_events(state["session_id"]), events_before)

    def test_rerun_request_cannot_be_redecided_with_different_parameters(self):
        """One immutable review request cannot acquire a second decision."""
        from app.control.lifecycle import resume_review_state

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = self._fake_flye_bin(tmpdir)
            state = self._build_blocked_state(tmpdir)
            request = self._rerun_request(state, genome_size=200000000)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                resume_review_state(state, request)

            conflicting_request = self._rerun_request(state, genome_size=150000000)
            with self.assertRaises(FileExistsError) as ctx:
                resume_review_state(state, conflicting_request)
            self.assertIn("decision.json", str(ctx.exception))

    def test_rerun_after_failed_attempt_uses_next_index(self):
        """Failed rerun output does not block the next parameter adjustment."""
        from app.control.lifecycle import ResumeValidationError, resume_review_state

        with tempfile.TemporaryDirectory() as tmpdir:
            fail_bin = self._fake_flye_bin(tmpdir, fail=True, name="bin_fail")
            success_bin = self._fake_flye_bin(tmpdir, name="bin_success")
            state = self._build_blocked_state(tmpdir)

            with patch.dict(os.environ, {"PATH": f"{fail_bin}:{os.environ['PATH']}"}):
                with self.assertRaises(ResumeValidationError):
                    resume_review_state(state, self._rerun_request(state, genome_size=150000000))

            state = self._renew_review_request(state, "rerun_1_failed")
            with patch.dict(os.environ, {"PATH": f"{success_bin}:{os.environ['PATH']}"}):
                update = resume_review_state(state, self._rerun_request(state, genome_size=200000000))

            rerun_ref = update["stage_outputs"]["assembly"]["selected_candidate_refs"][-1]
            self.assertIn("flye_rerun_2_candidate.json", rerun_ref)
            bundle = json.loads(Path(rerun_ref).read_text(encoding="utf-8"))
            self.assertIn("rerun_2", bundle["candidate_id"])
            self.assertEqual(bundle["metadata"]["rerun_index"], 2)

    def test_rerun_after_failed_then_successful_attempt_uses_next_free_index(self):
        """A selected rerun after a failed attempt does not get mistaken for stale replay."""
        from app.control.lifecycle import ResumeValidationError, resume_review_state

        with tempfile.TemporaryDirectory() as tmpdir:
            fail_bin = self._fake_flye_bin(tmpdir, fail=True, name="bin_fail")
            success_bin = self._fake_flye_bin(tmpdir, name="bin_success")
            state = self._build_blocked_state(tmpdir)

            with patch.dict(os.environ, {"PATH": f"{fail_bin}:{os.environ['PATH']}"}):
                with self.assertRaises(ResumeValidationError):
                    resume_review_state(state, self._rerun_request(state, genome_size=150000000))

            state = self._renew_review_request(state, "rerun_1_failed")
            with patch.dict(os.environ, {"PATH": f"{success_bin}:{os.environ['PATH']}"}):
                first_success = resume_review_state(state, self._rerun_request(state, genome_size=200000000))

            state["stage_outputs"]["assembly"]["selected_candidate_refs"] = first_success["stage_outputs"]["assembly"]["selected_candidate_refs"]
            state = self._renew_review_request(state, "rerun_2_completed")
            with patch.dict(os.environ, {"PATH": f"{success_bin}:{os.environ['PATH']}"}):
                second_success = resume_review_state(state, self._rerun_request(state, genome_size=250000000))

            rerun_ref = second_success["stage_outputs"]["assembly"]["selected_candidate_refs"][-1]
            self.assertIn("flye_rerun_3_candidate.json", rerun_ref)
            bundle = json.loads(Path(rerun_ref).read_text(encoding="utf-8"))
            self.assertIn("rerun_3", bundle["candidate_id"])
            self.assertEqual(bundle["metadata"]["estimated_genome_size"], 250000000)

    def test_rerun_rejects_unknown_source_candidate(self):
        """Rerun provenance must point to a known review candidate."""
        from app.control.lifecycle import ResumeValidationError, resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir)
            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="rerun",
                candidate_id="unknown",
                metadata={"rerun_parameters": {"read_type": "nano-raw", "estimated_genome_size": 100, "threads": 1}},
                **self._review_binding(state),
            )

            with self.assertRaisesRegex(ResumeValidationError, "unknown assembly_review candidate_id"):
                resume_review_state(state, request)

    def test_rerun_does_not_restart_from_inventory(self):
        """Rerun reuses assembly input_ref; does not touch inventory."""
        from app.control.lifecycle import resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = self._fake_flye_bin(tmpdir)
            state = self._build_blocked_state(tmpdir)
            original_input_ref = state["stage_outputs"]["assembly"]["input_ref"]

            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="rerun",
                candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
                metadata={"rerun_parameters": {"read_type": "nano-raw", "estimated_genome_size": 150000000, "threads": 1}},
                **self._review_binding(state),
            )
            with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                update = resume_review_state(state, request)

            self.assertEqual(update["stage_outputs"]["assembly"]["input_ref"], original_input_ref)
            self.assertNotIn("inventory", update.get("route_trace", []))

    def test_rerun_bounded_by_max_reruns(self):
        """Exceeding max_reruns raises validation error."""
        from app.control.lifecycle import ResumeValidationError, resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir, rerun_count=3)
            state["stage_outputs"]["assembly_review"]["max_reruns"] = 3

            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="rerun",
                candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
                metadata={"rerun_parameters": {"read_type": "nano-raw", "estimated_genome_size": 100, "threads": 1}},
                **self._review_binding(state),
            )
            with self.assertRaises(ResumeValidationError) as ctx:
                resume_review_state(state, request)
            self.assertIn("rerun limit", str(ctx.exception))

    def test_rerun_failed_assembly_does_not_discard_prior_candidates(self):
        """A failed rerun raises but does not modify prior candidate refs."""
        from app.control.lifecycle import ResumeValidationError, resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = self._fake_flye_bin(tmpdir, fail=True)
            state = self._build_blocked_state(tmpdir)
            original_refs = list(state["stage_outputs"]["assembly"]["selected_candidate_refs"])

            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="rerun",
                candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
                metadata={"rerun_parameters": {"read_type": "nano-raw", "estimated_genome_size": 100, "threads": 1}},
                **self._review_binding(state),
            )
            with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                with self.assertRaises(ResumeValidationError):
                    resume_review_state(state, request)

            self.assertEqual(
                state["stage_outputs"]["assembly"]["selected_candidate_refs"],
                original_refs,
            )

    def test_candidate_comparison_replay_reuses_existing_artifact(self):
        """Comparison generation is idempotent when candidates and signals are unchanged."""
        from app.control.ledger import Ledger
        from app.execution.artifacts import read_artifact_records
        from app.stages.assembly_review.nodes import generate_candidate_comparison

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir, rerun_count=1)
            candidates = []
            for ref in state["stage_outputs"]["assembly"]["selected_candidate_refs"]:
                bundle = json.loads(Path(ref).read_text(encoding="utf-8"))
                bundle["bundle_ref"] = ref
                bundle["source"] = "assembly_stage"
                bundle["final_assembly_ref"] = bundle["expected_outputs"]["draft_assembly_fasta"]
                candidates.append(bundle)
            state["stage_outputs"]["assembly_review"] = {
                **state["stage_outputs"]["assembly_review"],
                "candidates": candidates,
                "signals": [],
            }

            first_update = generate_candidate_comparison(state)
            first_ref = first_update["stage_outputs"]["assembly_review"]["candidate_comparison_ref"]
            merged = {**state, "stage_outputs": first_update["stage_outputs"]}
            artifacts_before = read_artifact_records(state["data_dir"], state["session_id"])
            events_before = Ledger(state["data_dir"]).read_events(state["session_id"])
            mtime_before = Path(first_ref).stat().st_mtime_ns

            replay_update = generate_candidate_comparison(merged)

            self.assertEqual(replay_update, {})
            self.assertEqual(Path(first_ref).stat().st_mtime_ns, mtime_before)
            self.assertEqual(read_artifact_records(state["data_dir"], state["session_id"]), artifacts_before)
            self.assertEqual(Ledger(state["data_dir"]).read_events(state["session_id"]), events_before)

    def test_review_report_replay_does_not_duplicate_artifact_records(self):
        from app.execution.artifacts import read_artifact_records
        from app.stages.assembly_review.nodes import human_review_gate

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir, persist_request=False)
            state["stage_outputs"]["assembly_review"] = {
                **state["stage_outputs"]["assembly_review"],
                "status": "evaluated",
                "signals": [],
                "review_plan": {"planned_signals": ["quast"], "available_signals": [], "missing_signals": ["quast"]},
            }

            first = human_review_gate(state)
            merged = {**state, **first}
            artifacts_before = [
                record for record in read_artifact_records(state["data_dir"], state["session_id"])
                if record["artifact_type"] in {"assembly_review.report", ASSEMBLY_REVIEW_PACKET}
            ]
            report_ref = first["stage_outputs"]["assembly_review"]["assembly_review_report_ref"]
            report_before = Path(report_ref).read_text(encoding="utf-8")

            second = human_review_gate(merged)

            artifacts_after = [
                record for record in read_artifact_records(state["data_dir"], state["session_id"])
                if record["artifact_type"] in {"assembly_review.report", ASSEMBLY_REVIEW_PACKET}
            ]
            self.assertEqual(Path(report_ref).read_text(encoding="utf-8"), report_before)
            self.assertEqual(artifacts_after, artifacts_before)
            second_result = {**merged, **second}
            self.assertEqual(
                _request_payload(second_result)["report_refs"]["assembly_review_report"],
                SessionPaths.from_run_dir(state["data_dir"]).to_ref(report_ref),
            )

    def test_rerun_generates_candidate_comparison_artifact(self):
        """After rerun re-enters assembly_review, comparison artifact is generated for 2+ candidates."""
        from app.control.lifecycle import resume_review_state
        from app.review.base import ResumeReviewRequest

        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = self._fake_flye_bin(tmpdir)
            state = self._build_blocked_state(tmpdir)

            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="rerun",
                candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0]["candidate_id"],
                metadata={"rerun_parameters": {"read_type": "nano-raw", "estimated_genome_size": 200000000, "threads": 2}},
                **self._review_binding(state),
            )
            with patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"}):
                update = resume_review_state(state, request)

            merged = {**state, **update}
            # Clear execution_mode so graph skips QUAST/BUSCO (no tools in test env)
            merged["stage_outputs"]["assembly_review"].pop("execution_mode", None)
            graph = build_stage()
            result = graph.invoke(merged)

            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertIn("candidate_comparison_ref", ar_out)
            comparison_path = Path(ar_out["candidate_comparison_ref"])
            self.assertTrue(comparison_path.exists())
            comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
            self.assertEqual(len(comparison["candidates"]), 2)
            self.assertIn("thresholds_not_applied_without_human_approval", comparison["notes"])
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(encoding="utf-8")
            self.assertIn(comparison["candidates"][0]["candidate_id"], report_text)
            self.assertIn(comparison["candidates"][1]["candidate_id"], report_text)
            request_payload = _request_payload(result)
            paths = SessionPaths.from_run_dir(result["data_dir"])
            self.assertEqual(
                request_payload["report_refs"]["candidate_comparison"],
                paths.to_ref(ar_out["candidate_comparison_ref"]),
            )
            self.assertNotIn("assembly_candidate_comparison_report", request_payload["report_refs"])
            self.assertEqual(
                sorted(request_payload["candidate_comparison_context"]["candidate_ids"]),
                sorted(candidate["candidate_id"] for candidate in comparison["candidates"]),
            )
            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl").read_text().splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn(ASSEMBLY_CANDIDATE_COMPARISON, artifact_types)


if __name__ == "__main__":
    unittest.main()
