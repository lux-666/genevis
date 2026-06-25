"""T2T finishing stage contracts and conservative patching tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import resume_review_state
from app.review.base import ResumeReviewRequest
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.stages.repeat_annotation.nodes import resolve_repeat_inputs
from app.stages.t2t.builder import build_stage
from app.stages.t2t.nodes import build_t2t_candidates, scan_read_telomeres


class T2TDescriptorTests(unittest.TestCase):
    def test_registry_loads_t2t_minimap2_method(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        method = registry.methods["t2t.alignment.minimap2"]
        self.assertEqual(method.stage, "t2t")
        self.assertEqual(method.package_ref, "minimap2")
        self.assertIn(method.method_name, registry.by_stage["t2t"])

    def test_run_config_accepts_default_auto_patch(self):
        config = RunConfig.model_validate({"local_exec": ["t2t"], "t2t": {}})
        self.assertEqual(config.t2t.mode, "auto_patch")
        self.assertEqual(config.t2t.thresholds.min_supporting_reads, 2)


class T2TStageTests(unittest.TestCase):
    def _state(self, tmpdir: str, assembly: Path, reads: Path | None = None):
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        state["current_stage"] = "t2t"
        assets = []
        if reads:
            assets.append({"asset_id": "s1:clean_long_reads:reads.fq", "asset_type": "clean_long_reads", "path": str(reads)})
        registry = Path(tmpdir) / "registry.json"
        registry.write_text(json.dumps({"session_id": "s1", "assets": assets}), encoding="utf-8")
        state["asset_registry_ref"] = str(registry)
        state["stage_outputs"] = {"assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)}}
        return state

    def _fake_minimap2(self, tmpdir: str, *, support_count: int = 2) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "minimap2"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "assembly = sys.argv[-2]\n"
            "seq = ''.join(line.strip() for line in open(assembly) if not line.startswith('>'))\n"
            "for index in range(" + repr(support_count) + "):\n"
            "    qlen = len(seq) + 24\n"
            "    print(f'r{index + 1}\\t{qlen}\\t0\\t{len(seq)}\\t+\\tchr1\\t{len(seq)}\\t0\\t{len(seq)}\\t{len(seq)}\\t{len(seq)}\\t60')\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def _write_inputs(self, tmpdir: str) -> tuple[Path, Path]:
        assembly = Path(tmpdir) / "final.fa"
        core = "TTAGGG" * 4 + "A" * 10_050
        assembly.write_text(f">chr1\n{core}\n", encoding="utf-8")
        reads = Path(tmpdir) / "reads.fq"
        reads.write_text(
            "".join(f"@r{i}\n{core}{'TTAGGG' * 4}\n+\n{'I' * (len(core) + 24)}\n" for i in range(1, 3)),
            encoding="utf-8",
        )
        return assembly, reads

    def test_auto_patch_preserves_input_and_hands_patched_assembly_to_annotation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            original = assembly.read_text(encoding="utf-8")
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {"execution_mode": "local", "env": {"PATH": str(self._fake_minimap2(tmpdir)) + os.pathsep + os.environ["PATH"]}}

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "repeat_annotation")
            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed")
            patched = Path(t2t["patched_assembly_ref"])
            self.assertTrue(patched.exists())
            self.assertEqual(assembly.read_text(encoding="utf-8"), original)
            self.assertIn("TTAGGG" * 4, patched.read_text(encoding="utf-8"))
            self.assertEqual(t2t["final_assembly_for_annotation"], str(patched))

            downstream = resolve_repeat_inputs(result)
            self.assertEqual(downstream["stage_outputs"]["repeat_annotation"]["final_assembly_for_annotation"], str(patched))

    def test_execution_is_plan_only_without_local_exec(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            result = build_stage().invoke(self._state(tmpdir, assembly, reads))

            self.assertEqual(result["current_stage"], "repeat_annotation")
            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "skipped")
            self.assertEqual(t2t["final_assembly_for_annotation"], str(assembly))
            self.assertTrue(Path(t2t["t2t_finishing_report_ref"]).exists())

    def test_hic_final_assembly_has_priority_over_assembly_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review_assembly, reads = self._write_inputs(tmpdir)
            hic_assembly = Path(tmpdir) / "hic_final.fa"
            hic_assembly.write_text(">chrH\n" + "TTAGGG" * 4 + "A" * 10_050 + "\n", encoding="utf-8")
            state = self._state(tmpdir, review_assembly, reads)
            state["stage_outputs"]["hic_scaffolding"] = {"final_assembly_for_annotation": str(hic_assembly)}

            result = build_stage().invoke(state)

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["source_stage"], "hic_scaffolding")
            self.assertEqual(t2t["input_assembly_ref"], str(hic_assembly))

    def test_invalid_assembly_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "not_fasta.txt"
            assembly.write_text("not a fasta\n", encoding="utf-8")
            result = build_stage().invoke(self._state(tmpdir, assembly))

            self.assertEqual(result["current_stage"], "t2t")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("valid FASTA", result["last_error"])

    def test_single_read_evidence_does_not_patch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {"PATH": str(self._fake_minimap2(tmpdir, support_count=1)) + os.pathsep + os.environ["PATH"]},
            }
            result = build_stage().invoke(state)

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed_with_warnings")
            self.assertNotIn("patched_assembly_ref", t2t)
            candidate_rows = Path(t2t["telomere_extension_candidates_ref"]).read_text(encoding="utf-8")
            self.assertIn("insufficient_or_conflicting_read_support", candidate_rows)

    def test_build_candidates_reentry_preserves_existing_candidate_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "out"
            out_dir.mkdir()
            candidates = out_dir / "telomere_extension_candidates.tsv"
            original = (
                "candidate_id\ttarget\ttarget_end\tread_id\tread_end\tstrand\tmapq\toverlap_bp\t"
                "target_distance_bp\textension_length\tquery_start\tquery_end\tsupporting_reads\tdecision\treason\n"
                "chr1:right\tchr1\tright\tr1\tright\t+\t60\t1000\t0\t24\t0\t1000\tr1,r2\tproposed\tconservative_multi_read_support\n"
            )
            candidates.write_text(original, encoding="utf-8")
            state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
            state["stage_outputs"] = {
                "t2t": {
                    "status": "candidates_built",
                    "out_dir": str(out_dir),
                    "telomere_extension_candidates_ref": str(candidates),
                    "candidate_count": 1,
                }
            }

            update = build_t2t_candidates(state)

            self.assertEqual(update, {})
            self.assertEqual(candidates.read_text(encoding="utf-8"), original)

    def test_manual_review_blocks_only_when_proposed_candidates_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {"PATH": str(self._fake_minimap2(tmpdir)) + os.pathsep + os.environ["PATH"]},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "t2t")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["stage_outputs"]["t2t"]["status"], "blocked_human")
            self.assertEqual(result["active_interrupt"]["stage"], "t2t")
            self.assertEqual(result["active_interrupt"]["gate_id"], "t2t_review")
            self.assertEqual(
                result["active_interrupt"]["decisions_allowed"],
                ["accept_selected_candidates", "continue_without_patch", "stop_pipeline"],
            )
            self.assertEqual([candidate["candidate_id"] for candidate in result["active_interrupt"]["candidates"]], ["chr1:right"])

    def test_manual_review_without_proposed_candidates_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {"PATH": str(self._fake_minimap2(tmpdir, support_count=1)) + os.pathsep + os.environ["PATH"]},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "repeat_annotation")
            self.assertNotEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["stage_outputs"]["t2t"]["status"], "completed_with_warnings")

    def test_manual_review_resume_accepts_explicit_selected_candidate_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {"PATH": str(self._fake_minimap2(tmpdir)) + os.pathsep + os.environ["PATH"]},
            }
            blocked = build_stage().invoke(state)
            request = ResumeReviewRequest(
                session_id="s1",
                stage="t2t",
                gate_id="t2t_review",
                decision="accept_selected_candidates",
                candidate_ids=["chr1:right"],
            )

            resumed = resume_review_state(blocked, request)

            self.assertEqual(resumed["current_stage"], "repeat_annotation")
            self.assertEqual(resumed["supervision_state"], "running_stage")
            self.assertIsNone(resumed["active_interrupt"])
            t2t = resumed["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed")
            self.assertEqual(t2t["selected_candidate_ids"], ["chr1:right"])
            self.assertTrue(Path(t2t["patched_assembly_ref"]).exists())
            self.assertEqual(t2t["final_assembly_for_annotation"], t2t["patched_assembly_ref"])

    def test_manual_review_resume_continue_without_patch_uses_input_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {"PATH": str(self._fake_minimap2(tmpdir)) + os.pathsep + os.environ["PATH"]},
            }
            blocked = build_stage().invoke(state)
            request = ResumeReviewRequest(
                session_id="s1",
                stage="t2t",
                gate_id="t2t_review",
                decision="continue_without_patch",
            )

            resumed = resume_review_state(blocked, request)

            self.assertEqual(resumed["current_stage"], "repeat_annotation")
            t2t = resumed["stage_outputs"]["t2t"]
            self.assertEqual(t2t["status"], "completed")
            self.assertEqual(t2t["final_assembly_for_annotation"], str(assembly))
            self.assertIsNone(t2t["patched_assembly_ref"])

    def test_manual_review_resume_stop_pipeline_does_not_enter_downstream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "manual_review",
                "env": {"PATH": str(self._fake_minimap2(tmpdir)) + os.pathsep + os.environ["PATH"]},
            }
            blocked = build_stage().invoke(state)
            request = ResumeReviewRequest(
                session_id="s1",
                stage="t2t",
                gate_id="t2t_review",
                decision="stop_pipeline",
            )

            stopped = resume_review_state(blocked, request)

            self.assertEqual(stopped["current_stage"], "t2t")
            self.assertEqual(stopped["supervision_state"], "failed_terminal")
            self.assertIsNone(stopped["active_interrupt"])

    def test_propose_only_with_candidates_is_non_blocking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "mode": "propose_only",
                "env": {"PATH": str(self._fake_minimap2(tmpdir)) + os.pathsep + os.environ["PATH"]},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "repeat_annotation")
            self.assertNotEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["stage_outputs"]["t2t"]["status"], "completed_with_warnings")
            self.assertIsNone(result.get("active_interrupt"))

    def test_alignment_uses_filtered_telomeric_reads_fasta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            with reads.open("a", encoding="utf-8") as handle:
                handle.write("@not_telomeric\n" + "C" * 2000 + "\n+\n" + "I" * 2000 + "\n")
            state = self._state(tmpdir, assembly, reads)
            state["stage_outputs"]["t2t"] = {
                "execution_mode": "local",
                "env": {"PATH": str(self._fake_minimap2(tmpdir)) + os.pathsep + os.environ["PATH"]},
            }

            result = build_stage().invoke(state)

            t2t = result["stage_outputs"]["t2t"]
            self.assertEqual(t2t["read_count"], 3)
            self.assertEqual(t2t["telomeric_read_count"], 2)
            self.assertEqual(t2t["alignment_attempt"]["metadata"]["reads_ref"], t2t["telomeric_reads_fasta_ref"])
            self.assertNotIn("not_telomeric", Path(t2t["telomeric_reads_fasta_ref"]).read_text(encoding="utf-8"))

    def test_scan_read_telomeres_streams_outputs_without_bulk_tsv_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads = self._write_inputs(tmpdir)
            state = self._state(tmpdir, assembly, reads)
            out_dir = Path(tmpdir) / "out"
            work_dir = Path(tmpdir) / "work"
            out_dir.mkdir()
            work_dir.mkdir()
            state["stage_outputs"]["t2t"] = {
                "status": "reads_prepared",
                "ont_reads_path": str(reads),
                "out_dir": str(out_dir),
                "work_dir": str(work_dir),
                "telomere_motif": "TTAGGG",
                "thresholds": {
                    "min_motif_copies": 4,
                    "terminal_window_bp": 10_000,
                    "min_mapq": 20,
                    "max_target_terminal_distance_bp": 1_000,
                    "min_overlap_bp": 500,
                    "min_supporting_reads": 2,
                },
            }

            with patch("app.stages.t2t.nodes._write_tsv", side_effect=AssertionError("bulk TSV write called")):
                update = scan_read_telomeres(state)

            t2t = update["stage_outputs"]["t2t"]
            self.assertEqual(t2t["read_count"], 2)
            self.assertEqual(t2t["telomeric_read_count"], 2)
            self.assertTrue(Path(t2t["reads_telomere_scan_ref"]).exists())
            self.assertTrue(Path(t2t["telomeric_reads_ref"]).exists())
            self.assertTrue(Path(t2t["telomeric_reads_fasta_ref"]).exists())
