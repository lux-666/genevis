"""Tests for the AutoHiC-only Hi-C scaffolding stage."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.graph.routing import next_stage_name
from app.review.base import ResumeReviewRequest
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.hic_scaffolding.builder import build_stage
from app.stages.hic_scaffolding.nodes import apply_hic_scaffolding_resume_decision


AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"


class HiCScaffoldingDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_exposes_only_autohic_for_hic_scaffolding(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        self.assertEqual(registry.by_stage["hic_scaffolding"], [AUTOHIC_METHOD])
        self.assertIn("autohic", registry.packages)
        self.assertIn("juicer", registry.packages)
        self.assertIn("3ddna", registry.packages)
        self.assertEqual(registry.methods[AUTOHIC_METHOD].package_ref, "autohic")
        self.assertFalse(any("yahs" in method.lower() for method in registry.methods))


class HiCScaffoldingStageTests(unittest.TestCase):
    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(json.dumps({"session_id": "s1", "assets": assets}, indent=2), encoding="utf-8")
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

    def _inputs(self, tmpdir: str, *, read_count: int = 2) -> tuple[Path, list[Path], Path, Path]:
        root = Path(tmpdir)
        assembly = root / "adopted_contigs.fasta"
        assembly.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        reads: list[Path] = []
        for index in range(read_count):
            mate = "R1" if index % 2 == 0 else "R2"
            read = root / f"lane_{index // 2 + 1}_{mate}.fastq.gz"
            read.write_text(f"@read{index + 1}\nACGT\n+\n!!!!\n", encoding="utf-8")
            reads.append(read)
        error_model = root / "error_model.pth"
        chromosome_model = root / "chr_model.pth"
        error_model.write_text("error model\n", encoding="utf-8")
        chromosome_model.write_text("chromosome model\n", encoding="utf-8")
        return assembly, reads, error_model, chromosome_model

    def _registry_for_inputs(self, tmpdir: str, assembly: Path, reads: list[Path]) -> Path:
        assets = [
            {
                "asset_id": "s1:assembly_fasta_registered:adopted",
                "asset_type": "assembly_fasta_registered",
                "path": str(assembly),
            }
        ]
        assets.extend(
            {
                "asset_id": f"s1:hic_reads_registered:{path.name}",
                "asset_type": "hic_reads_registered",
                "path": str(path),
            }
            for path in reads
        )
        return self._write_registry(tmpdir, assets=assets)

    def _configured_state(
        self,
        tmpdir: str,
        assembly: Path,
        reads: list[Path],
        error_model: Path,
        chromosome_model: Path,
    ) -> dict:
        state = self._state(tmpdir, str(self._registry_for_inputs(tmpdir, assembly, reads)))
        state["execution_mode"] = "local"
        state["stage_outputs"] = {
            "inventory": {"target_assembly_level": "chromosome"},
            "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
            "hic_scaffolding": {
                "execution_mode": "local",
                "selected_methods": [AUTOHIC_METHOD],
                "primary_method": AUTOHIC_METHOD,
                "autohic_parameters": {
                    "restriction_enzyme": "MboI",
                    "error_model_ref": str(error_model),
                    "chromosome_model_ref": str(chromosome_model),
                    "threads": 2,
                    "python_command": "python",
                },
            },
        }
        return state

    def _fake_autohic_environment(self, tmpdir: str, *, fail: bool = False) -> Path:
        root = Path(tmpdir)
        autohic_root = root / "AutoHiC"
        (autohic_root / "src" / "common").mkdir(parents=True)
        (autohic_root / "src" / "models" / "cfgs").mkdir(parents=True)
        (autohic_root / "src" / "common" / "run.sh").write_text("# fake pipeline\n", encoding="utf-8")
        (autohic_root / "src" / "models" / "cfgs" / "error_model.py").write_text("# fake model cfg\n", encoding="utf-8")
        entry = autohic_root / "autohic.py"
        entry.write_text(
            "import pathlib, sys\n"
            "cfg_path = pathlib.Path(sys.argv[sys.argv.index('-c') + 1])\n"
            "cfg = dict(line.strip().split('=', 1) for line in cfg_path.read_text().splitlines() if line.strip())\n"
            "if " + repr(fail) + ":\n"
            "    print('fake AutoHiC failed', file=sys.stderr)\n"
            "    raise SystemExit(17)\n"
            "assert pathlib.Path(cfg['REFERENCE_GENOME']).is_file()\n"
            "fastqs = sorted(path.name for path in (pathlib.Path(cfg['FASTQ_DIR']) / 'fastq').iterdir())\n"
            "assert fastqs == ['hic_001_R1.fastq.gz', 'hic_001_R2.fastq.gz']\n"
            "assert pathlib.Path(cfg['ERROR_PRETRAINED_MODEL']).is_file()\n"
            "assert pathlib.Path(cfg['CHR_PRETRAINED_MODEL']).is_file()\n"
            "top = pathlib.Path(cfg['RESULT_DIR']) / cfg['JOB_NAME']\n"
            "chromosome = top / 'autohic_results' / 'chromosome'\n"
            "chromosome.mkdir(parents=True)\n"
            "(top / 'contigs_autohic.fasta').write_text('>chr1\\nACGTACGT\\n')\n"
            "(chromosome / 'contigs.final.hic').write_text('hic\\n')\n"
            "(chromosome / 'contigs.final.assembly').write_text('assembly\\n')\n"
            "(top / 'result.html').write_text('<html>AutoHiC report</html>\\n')\n"
            "print('fake AutoHiC ok')\n",
            encoding="utf-8",
        )

        juicer_root = root / "juicer"
        (juicer_root / "CPU" / "common").mkdir(parents=True)
        (juicer_root / "misc").mkdir(parents=True)
        (juicer_root / "scripts" / "common").mkdir(parents=True)
        for relative in [
            "CPU/juicer.sh",
            "CPU/common/chimeric_blacklist.awk",
            "misc/generate_site_positions.py",
            "scripts/common/juicer_tools.jar",
        ]:
            (juicer_root / relative).write_text("stub\n", encoding="utf-8")

        three_d_dna_root = root / "3d-dna"
        (three_d_dna_root / "edit").mkdir(parents=True)
        (three_d_dna_root / "visualize").mkdir(parents=True)
        for relative in [
            "run-asm-pipeline.sh",
            "run-asm-pipeline-post-review.sh",
            "edit/apply-edits-prep-for-next-round.sh",
            "visualize/run-assembly-visualizer.sh",
        ]:
            (three_d_dna_root / relative).write_text("stub\n", encoding="utf-8")

        envs_yaml = root / "envs.yaml"
        envs_yaml.write_text(
            "default:\n"
            "  conda_bin: conda\n"
            "  fallback_env: base\n"
            "packages:\n"
            "  autohic:\n"
            f"    install_root: {autohic_root}\n"
            "    canary_paths: [autohic.py, src/common/run.sh, src/models/cfgs/error_model.py]\n"
            "  juicer:\n"
            f"    install_root: {juicer_root}\n"
            "    canary_paths: [CPU/juicer.sh, CPU/common/chimeric_blacklist.awk, misc/generate_site_positions.py, scripts/common/juicer_tools.jar]\n"
            "  three_d_dna:\n"
            f"    install_root: {three_d_dna_root}\n"
            "    canary_paths: [run-asm-pipeline.sh, run-asm-pipeline-post-review.sh, edit/apply-edits-prep-for-next-round.sh, visualize/run-assembly-visualizer.sh]\n"
            "methods:\n"
            f"  {AUTOHIC_METHOD}:\n"
            "    conda_env: base\n"
            "    package_tools: [autohic, juicer, three_d_dna]\n",
            encoding="utf-8",
        )
        return envs_yaml

    def test_graph_contains_only_autohic_method_node(self):
        nodes = set(build_stage().get_graph().nodes)
        self.assertIn("autohic", nodes)
        self.assertNotIn("yahs", nodes)
        self.assertNotIn("juicer_3ddna", nodes)

    def test_missing_final_assembly_ref_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._state(tmpdir, str(registry)))
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("final_assembly_ref", result["last_error"])

    def test_registered_assembly_is_used_when_review_output_is_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._state(tmpdir, str(self._registry_for_inputs(tmpdir, assembly, reads)))
            state["stage_outputs"] = {"inventory": {"target_assembly_level": "chromosome"}}
            result = build_stage().invoke(state)
            self.assertEqual(result["stage_outputs"]["hic_scaffolding"]["pre_hic_assembly_ref"], str(assembly))

    def test_no_hic_reads_skips_and_continues_with_contig_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, _, _, _ = self._inputs(tmpdir, read_count=0)
            registry = self._registry_for_inputs(tmpdir, assembly, [])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
            }
            result = build_stage().invoke(state)
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["current_stage"], next_stage_name("hic_scaffolding"))
            self.assertEqual(hic["mode"], "skipped_no_hic")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))

    def test_hic_present_but_not_chromosome_level_skips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._state(tmpdir, str(self._registry_for_inputs(tmpdir, assembly, reads)))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "contig"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
            }
            result = build_stage().invoke(state)
            self.assertEqual(result["stage_outputs"]["hic_scaffolding"]["mode"], "skipped_not_chromosome_level")

    def test_chromosome_request_plans_only_autohic_and_blocks_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._state(tmpdir, str(self._registry_for_inputs(tmpdir, assembly, reads)))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
            }
            result = build_stage().invoke(state)
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(hic["candidate_methods"], [AUTOHIC_METHOD])
            plan = json.loads(Path(hic["scaffold_plan_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["candidate_methods"], [AUTOHIC_METHOD])
            self.assertEqual(
                set(plan["expected_outputs"]),
                {"hic_qc_report", "chromosome_scaffolded_assembly", "contact_map", "assembly_file", "html_report"},
            )

    def test_local_autohic_registers_four_outputs_then_blocks_geneagent_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads, error_model, chromosome_model)
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            candidate = hic["method_candidates"][AUTOHIC_METHOD]
            self.assertEqual(result["current_stage"], "hic_scaffolding")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(candidate["status"], "executed")
            for key in ["scaffold_fasta", "contact_map", "assembly_file", "html_report"]:
                self.assertTrue(Path(candidate[key]).is_file(), key)
            self.assertEqual(Path(candidate["scaffold_fasta"]).read_text(), ">chr1\nACGTACGT\n")
            self.assertIn("AutoHiC report", Path(candidate["html_report"]).read_text())
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))

            config = Path(tmpdir) / "work" / "hic_scaffolding" / "s1" / "autohic" / "cfg-autohic.txt"
            config_text = config.read_text(encoding="utf-8")
            self.assertIn("REFERENCE_GENOME=", config_text)
            self.assertIn("ENZYME=MboI", config_text)
            self.assertIn(f"ERROR_PRETRAINED_MODEL={error_model.resolve()}", config_text)
            self.assertIn(f"CHR_PRETRAINED_MODEL={chromosome_model.resolve()}", config_text)
            self.assertNotIn(f"REFERENCE_GENOME={assembly}", config_text)

            artifact_types = {
                json.loads(line)["artifact_type"]
                for line in (Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl").read_text().splitlines()
            }
            self.assertIn("hic_scaffolding.chromosome_scaffolded_assembly", artifact_types)
            self.assertIn("hic_scaffolding.contact_map", artifact_types)
            self.assertIn("hic_scaffolding.assembly", artifact_types)
            self.assertIn("hic_scaffolding.html_report", artifact_types)

            adopted = apply_hic_scaffolding_resume_decision(
                result,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="hic_scaffolding",
                    decision="accept_candidate",
                    method_name=AUTOHIC_METHOD,
                    notes=["GeneAgent review accepted AutoHiC chromosome assembly"],
                ),
            )
            adopted_hic = adopted["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(adopted["current_stage"], next_stage_name("hic_scaffolding"))
            self.assertEqual(adopted_hic["final_assembly_for_downstream"], candidate["scaffold_fasta"])

    def test_local_autohic_requires_both_pretrained_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            chromosome_model.unlink()
            envs_yaml = self._fake_autohic_environment(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads, error_model, chromosome_model)
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(state)
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertIn("autohic_parameters.chromosome_model_ref", hic["missing_params"])
            self.assertIsNone(hic.get("autohic_frozen_attempt"))

    def test_local_autohic_requires_paired_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir, read_count=1)
            envs_yaml = self._fake_autohic_environment(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads, error_model, chromosome_model)
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(state)
            self.assertIn("paired hic_read_paths (R1/R2)", result["stage_outputs"]["hic_scaffolding"]["missing_params"])

    def test_local_autohic_rejects_invalid_error_threshold_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(tmpdir)
            state = self._configured_state(tmpdir, assembly, reads, error_model, chromosome_model)
            state["stage_outputs"]["hic_scaffolding"]["autohic_parameters"]["error_filter_score"] = "invalid"
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(state)
            invalid = result["stage_outputs"]["hic_scaffolding"]["invalid_params"]
            self.assertEqual(invalid[0]["param"], "autohic_parameters.error_filter_score")

    def test_local_autohic_failure_preserves_contig_fallback_and_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, error_model, chromosome_model = self._inputs(tmpdir)
            envs_yaml = self._fake_autohic_environment(tmpdir, fail=True)
            state = self._configured_state(tmpdir, assembly, reads, error_model, chromosome_model)
            with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs_yaml)}):
                result = build_stage().invoke(state)
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["method_candidates"][AUTOHIC_METHOD]["status"], "failed")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertIn("autohic_execution_failed", hic["notes"])
            self.assertTrue(hic["autohic_error"])

    def test_local_mode_without_explicit_method_remains_planning_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly, reads, _, _ = self._inputs(tmpdir)
            state = self._state(tmpdir, str(self._registry_for_inputs(tmpdir, assembly, reads)))
            state["execution_mode"] = "local"
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
            }
            result = build_stage().invoke(state)
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNone(hic.get("autohic_frozen_attempt"))


if __name__ == "__main__":
    unittest.main()
