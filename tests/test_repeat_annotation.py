"""Tests for Phase 3 repeat annotation realization."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.execution.artifacts import read_artifact_records
from app.review.gates import get_review_gate
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.repeat_annotation.builder import build_stage
from app.stages.repeat_annotation.nodes import (
    apply_mask_review_decision,
    block_mask_review,
    _parse_tbl_repeat_percentage,
)
from app.review.base import ResumeReviewRequest


class RepeatAnnotationDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_repeat_annotation_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "repeat_annotation.masking.repeatmasker": "repeatmasker",
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
            self.assertIn("repeatmasker_executed", repeat["notes"])
            bundle = json.loads(Path(repeat["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(bundle["status"], "executed")
            self.assertTrue(bundle["metadata"]["execution_enabled"])
            self.assertNotIn("execution_mode", bundle["metadata"])
            artifact_index = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
            self.assertIn("repeat_annotation.softmasked_genome", artifact_types)
            self.assertIn("repeat_annotation.gff3", artifact_types)
            self.assertIn("command.stdout", artifact_types)
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("attempt_started", [event["event_type"] for event in events])
            self.assertIn("attempt_succeeded", [event["event_type"] for event in events])

    def test_local_repeatmasker_handles_relative_data_dir_from_runner_workdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assembly = root / "final.fa"
            library = root / "trusted_repeats.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            library.write_text(">repeat1\nACAC\n", encoding="utf-8")
            bin_dir = self._fake_repeatmasker_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:repeat_library:trusted", "asset_type": "repeat_library", "path": str(library)}],
            )
            data_dir = root / "out"
            state = self._state(tmpdir, str(registry))
            state["data_dir"] = os.path.relpath(data_dir, Path.cwd())
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
            self.assertTrue(Path(repeat["softmasked_genome_ref"]).is_absolute())

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
            self.assertEqual(repeat["library_candidate_methods"], [])
            self.assertEqual(repeat["helper_methods"], [])
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


class MaskReviewGateTests(unittest.TestCase):
    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(json.dumps({"session_id": "s1", "assets": assets}), encoding="utf-8")
        return path

    def _state(self, tmpdir: str, registry_ref: str | None) -> dict:
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        state["current_stage"] = "repeat_annotation"
        state["asset_registry_ref"] = registry_ref
        return state

    def _fake_repeatmasker_bin(self, tmpdir: str) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "RepeatMasker"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "assembly = pathlib.Path(sys.argv[-1])\n"
            "assembly.with_name(assembly.name + '.masked').write_text('>ctg1\\nacgt\\n', encoding='utf-8')\n"
            "assembly.with_name(assembly.name + '.out.gff').write_text('##gff-version 3\\n', encoding='utf-8')\n"
            "assembly.with_name(assembly.name + '.tbl').write_text(\n"
            "    'RepeatMasker Results\\n'\n"
            "    'Bases masked:   1234 bp ( 30.50 %)\\n',\n"
            "    encoding='utf-8'\n"
            ")\n"
            "print('ok')\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def test_gate_is_registered_in_registry(self):
        gate = get_review_gate("repeat_annotation", "mask_review")
        self.assertEqual(gate.stage, "repeat_annotation")
        self.assertEqual(gate.gate_id, "mask_review")
        self.assertIn("accept", gate.decisions_allowed)
        self.assertIn("skip", gate.decisions_allowed)

    def test_parse_tbl_repeat_percentage_extracts_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            tbl = Path(tmp) / "assembly.fasta.tbl"
            tbl.write_text("Bases masked:   1234 bp ( 30.50 %)\n", encoding="utf-8")
            self.assertAlmostEqual(_parse_tbl_repeat_percentage(tbl), 30.50)

    def test_parse_tbl_repeat_percentage_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_parse_tbl_repeat_percentage(Path(tmp) / "missing.tbl"))

    def test_execution_path_auto_accepts_and_registers_gate_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            library = Path(tmpdir) / "lib.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            library.write_text(">repeat1\nACAC\n", encoding="utf-8")
            bin_dir = self._fake_repeatmasker_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:repeat_library:lib", "asset_type": "repeat_library", "path": str(library)}],
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
            self.assertEqual(repeat["decision"], "accept")
            self.assertEqual(repeat["decided_by"], "auto_rule")
            self.assertAlmostEqual(repeat["repeat_percentage"], 30.50)
            self.assertIn("mask_review", result["route_trace"])
            self.assertIn("repeat_annotation_stage", result["route_trace"])
            self.assertLess(
                result["route_trace"].index("mask_review"),
                result["route_trace"].index("repeat_annotation_stage"),
            )

            data_dir = Path(tmpdir)
            artifact_types = {rec["artifact_type"] for rec in read_artifact_records(data_dir, "s1")}
            self.assertIn("repeat_annotation.mask_review_request", artifact_types)
            self.assertIn("repeat_annotation.mask_review_decision", artifact_types)

            events = Ledger(data_dir).read_events("s1")
            event_types = [e["event_type"] for e in events]
            self.assertIn("review_requested", event_types)
            self.assertIn("review_decision", event_types)

    def test_strict_mode_policy_blocks_for_human_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            library = Path(tmpdir) / "lib.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            library.write_text(">repeat1\nACAC\n", encoding="utf-8")
            bin_dir = self._fake_repeatmasker_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:repeat_library:lib", "asset_type": "repeat_library", "path": str(library)}],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "hic_scaffolding": {"status": "skipped", "final_assembly_for_annotation": str(assembly)},
                "repeat_annotation": {
                    "execution_mode": "local",
                    "primary_method": "repeat_annotation.masking.repeatmasker",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                    "review_policy": {"mode": "strict"},
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["current_stage"], "repeat_annotation")
            interrupt = result["active_interrupt"]
            self.assertEqual(interrupt["stage"], "repeat_annotation")
            self.assertEqual(interrupt["gate_id"], "mask_review")
            self.assertIn("accept", interrupt["decisions_allowed"])

    def test_resume_with_accept_completes_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Ledger(tmpdir).create_session("s1", "manifest.yaml")
            softmasked = Path(tmpdir) / "softmasked.fasta"
            softmasked.write_text(">ctg1\nacgt\n", encoding="utf-8")
            state = self._state(tmpdir, None)
            state["supervision_state"] = "blocked_human"
            state["stage_outputs"] = {
                "repeat_annotation": {
                    "status": "masking_executed",
                    "softmasked_genome_ref": str(softmasked),
                    "repeat_annotation_gff3_ref": str(softmasked),
                    "repeat_percentage": 25.0,
                    "strategy": "provided",
                    "repeat_library_source": "provided",
                    "final_assembly_for_annotation": str(softmasked),
                    "repeat_library_ref": str(softmasked),
                    "confidence": "high",
                    "low_confidence_reasons": [],
                    "candidate_methods": [],
                    "notes": [],
                    "execution_enabled": True,
                }
            }
            request = ResumeReviewRequest(
                session_id="s1",
                stage="repeat_annotation",
                gate_id="mask_review",
                decision="accept",
                decided_by="human",
            )

            result = apply_mask_review_decision(state, request)

            self.assertEqual(result["current_stage"], "gene_annotation")
            self.assertEqual(result["supervision_state"], "running_stage")
            self.assertIsNone(result["active_interrupt"])
            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["status"], "completed")
            self.assertEqual(repeat["decision"], "accept")
            self.assertTrue(Path(repeat["bundle_artifact_ref"]).exists())

    def test_resume_with_skip_uses_unmasked_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Ledger(tmpdir).create_session("s1", "manifest.yaml")
            unmasked = Path(tmpdir) / "unmasked.fasta"
            softmasked = Path(tmpdir) / "softmasked.fasta"
            unmasked.write_text(">ctg1\nACGT\n", encoding="utf-8")
            softmasked.write_text(">ctg1\nacgt\n", encoding="utf-8")
            state = self._state(tmpdir, None)
            state["supervision_state"] = "blocked_human"
            state["stage_outputs"] = {
                "repeat_annotation": {
                    "status": "masking_executed",
                    "softmasked_genome_ref": str(softmasked),
                    "repeat_annotation_gff3_ref": str(softmasked),
                    "repeat_percentage": 5.0,
                    "strategy": "provided",
                    "repeat_library_source": "provided",
                    "final_assembly_for_annotation": str(unmasked),
                    "repeat_library_ref": str(unmasked),
                    "confidence": "high",
                    "low_confidence_reasons": [],
                    "candidate_methods": [],
                    "notes": [],
                    "execution_enabled": True,
                }
            }
            request = ResumeReviewRequest(
                session_id="s1",
                stage="repeat_annotation",
                gate_id="mask_review",
                decision="skip",
                decided_by="human",
            )

            result = apply_mask_review_decision(state, request)

            self.assertEqual(result["current_stage"], "gene_annotation")
            repeat = result["stage_outputs"]["repeat_annotation"]
            self.assertEqual(repeat["softmasked_genome_ref"], str(unmasked))
            self.assertEqual(repeat["decision"], "skip")
            self.assertIn("mask_review_skipped", repeat.get("notes", []))
            self.assertIn("mask_review", result["route_trace"])


if __name__ == "__main__":
    unittest.main()
