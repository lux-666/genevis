"""Tests for Phase 3 Hi-C scaffolding realization."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.graph.routing import next_stage_name
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.hic_scaffolding.builder import build_stage


class HiCScaffoldingDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_hic_scaffolding_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "hic_scaffolding.candidate.yahs": "yahs",
            "hic_scaffolding.candidate.juicer_3ddna_juicebox": "juicer",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "hic_scaffolding")
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["hic_scaffolding"])


class HiCScaffoldingStageTests(unittest.TestCase):
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
        state["current_stage"] = "hic_scaffolding"
        state["asset_registry_ref"] = registry_ref
        return state

    def _fake_yahs_bin(self, tmpdir: str, *, fail: bool = False) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "yahs"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "if " + repr(fail) + ":\n"
            "    print('fake yahs failed', file=sys.stderr)\n"
            "    sys.exit(8)\n"
            "args = sys.argv[1:]\n"
            "prefix = args[args.index('-o') + 1]\n"
            "p = pathlib.Path(prefix)\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "pathlib.Path(str(p) + '_scaffolds_final.fa').write_text('>scaffold1\\nACGT\\n', encoding='utf-8')\n"
            "pathlib.Path(str(p) + '_scaffolds_final.agp').write_text('scaffold1\\t1\\t4\\t1\\tW\\tctg1\\t1\\t4\\t+\\n', encoding='utf-8')\n"
            "print('fake yahs ok')\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return bin_dir

    def _fake_juicer_3ddna_bin(
        self,
        tmpdir: str,
        *,
        fail_3ddna: bool = False,
        with_site_generator: bool = True,
        with_helper_scripts: bool = True,
    ) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        juicer = bin_dir / "juicer.sh"
        juicer.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "top = pathlib.Path(args[args.index('-d') + 1])\n"
            "scripts_dir = pathlib.Path(args[args.index('-D') + 1]) if '-D' in args else None\n"
            "if scripts_dir is None or not (scripts_dir / 'scripts' / 'common' / 'chimeric_sam.awk').is_file():\n"
            "    print('Juicer helper scripts directory must be passed with -D', file=sys.stderr)\n"
            "    sys.exit(14)\n"
            "fastqs = {path.name for path in (top / 'fastq').iterdir()}\n"
            "if not {'hic_001_R1.fastq.gz', 'hic_001_R2.fastq.gz'} <= fastqs:\n"
            "    print('Juicer-compatible R1/R2 FASTQ links are required', file=sys.stderr)\n"
            "    sys.exit(13)\n"
            "if (top / 'aligned').exists():\n"
            "    print('Juicer requires a fresh aligned directory', file=sys.stderr)\n"
            "    sys.exit(12)\n"
            "(top / 'aligned').mkdir(parents=True, exist_ok=True)\n"
            "(top / 'aligned' / 'merged_nodups.txt').write_text('merged\\n', encoding='utf-8')\n"
            "print('fake juicer ok')\n",
            encoding="utf-8",
        )
        three_dna = bin_dir / "run-asm-pipeline.sh"
        three_dna.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "if " + repr(fail_3ddna) + ":\n"
            "    print('fake 3ddna failed', file=sys.stderr)\n"
            "    sys.exit(9)\n"
            "pathlib.Path('input_assembly.FINAL.fasta').write_text('>scaffold1\\nACGT\\n', encoding='utf-8')\n"
            "pathlib.Path('input_assembly.FINAL.assembly').write_text('assembly\\n', encoding='utf-8')\n"
            "pathlib.Path('review.hic').write_text('hic\\n', encoding='utf-8')\n"
            "print('fake 3ddna ok')\n",
            encoding="utf-8",
        )
        juicer.chmod(0o755)
        three_dna.chmod(0o755)
        if with_helper_scripts:
            helper = Path(tmpdir) / "scripts" / "common" / "chimeric_sam.awk"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("# fake Juicer helper\n", encoding="utf-8")
        if with_site_generator:
            generator = bin_dir / "generate_site_positions.py"
            generator.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "enzyme, genome, reference = sys.argv[1:]\n"
                "pathlib.Path(genome + '_' + enzyme + '.txt').write_text('ctg1 1 4\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            generator.chmod(0o755)
        bwa = bin_dir / "bwa"
        bwa.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "if sys.argv[1:] and sys.argv[1] == 'index':\n"
            "    pathlib.Path(sys.argv[2] + '.bwt').write_text('index\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        bwa.chmod(0o755)
        return bin_dir

    def test_missing_final_assembly_ref_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "hic_scaffolding")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("final_assembly_ref", result["last_error"])

    def test_registered_assembly_is_used_when_review_output_is_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            hic_reads = Path(tmpdir) / "hic_R1.fq.gz"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            hic_reads.write_text("reads\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {"asset_id": "s1:assembly_fasta_registered:final", "asset_type": "assembly_fasta_registered", "path": str(assembly)},
                    {"asset_id": "s1:hic_reads_registered:r1", "asset_type": "hic_reads_registered", "path": str(hic_reads)},
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {"inventory": {"target_assembly_level": "chromosome"}}

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["pre_hic_assembly_ref"], str(assembly))

    def test_no_hic_reads_skips_and_continues_with_contig_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], next_stage_name("hic_scaffolding"))
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "skipped")
            self.assertEqual(hic["mode"], "skipped_no_hic")
            self.assertEqual(hic["skip_reason"], "no_hic_reads")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertEqual(hic["pre_hic_assembly_ref"], str(assembly))
            self.assertEqual(result["skip_reasons"]["hic_scaffolding"], "no_hic_reads")
            skip_ref = Path(hic["skip_record_ref"])
            self.assertTrue(skip_ref.exists())
            payload = json.loads(skip_ref.read_text(encoding="utf-8"))
            self.assertEqual(payload["final_assembly_for_annotation"], str(assembly))

    def test_hic_present_but_not_chromosome_level_skips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            hic_reads = Path(tmpdir) / "sample.hic"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            hic_reads.write_text("hic", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_reads_registered:sample.hic",
                        "asset_type": "hic_reads_registered",
                        "path": str(hic_reads),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "contig"},
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], next_stage_name("hic_scaffolding"))
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "skipped")
            self.assertEqual(hic["mode"], "skipped_not_chromosome_level")
            self.assertEqual(hic["skip_reason"], "not_chromosome_level_requested")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))

    def test_hic_present_and_chromosome_requested_plans_candidates_and_blocks_human(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            hic_r1 = Path(tmpdir) / "hic_R1.fq.gz"
            hic_r2 = Path(tmpdir) / "hic_R2.fq.gz"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            hic_r1.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            hic_r2.write_text("@r2\nTGCA\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_reads_registered:hic_R1.fq.gz",
                        "asset_type": "hic_reads_registered",
                        "path": str(hic_r1),
                    },
                    {
                        "asset_id": "s1:hic_reads_registered:hic_R2.fq.gz",
                        "asset_type": "hic_reads_registered",
                        "path": str(hic_r2),
                    },
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "hic_scaffolding")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["active_interrupt"]["stage"], "hic_scaffolding")
            self.assertIn("blocked_at", result["active_interrupt"])
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_human")
            self.assertEqual(hic["mode"], "scaffold")
            self.assertEqual(hic["pre_hic_assembly_ref"], str(assembly))
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertIn("hic_scaffolding.candidate.yahs", hic["candidate_methods"])
            self.assertIn("hic_scaffolding.candidate.juicer_3ddna_juicebox", hic["candidate_methods"])
            self.assertTrue(Path(hic["scaffold_plan_ref"]).exists())
            plan = json.loads(Path(hic["scaffold_plan_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "planned")
            self.assertEqual(plan["pre_hic_assembly_ref"], str(assembly))
            self.assertEqual(plan["fallback_assembly_ref"], str(assembly))
            self.assertEqual(plan["hic_read_refs"], ["s1:hic_reads_registered:hic_R1.fq.gz", "s1:hic_reads_registered:hic_R2.fq.gz"])
            self.assertEqual(plan["hic_alignment_refs"], [])
            self.assertNotIn("yahs_frozen_attempt", hic)

    def test_hic_alignment_plan_keeps_alignment_refs_separate_from_read_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            alignment = Path(tmpdir) / "hic.bam"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            alignment.write_text("bam", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_alignment_bam:hic.bam",
                        "asset_type": "hic_alignment_bam",
                        "path": str(alignment),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
            }

            result = build_stage().invoke(state)

            plan = json.loads(Path(result["stage_outputs"]["hic_scaffolding"]["scaffold_plan_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["hic_read_refs"], [])
            self.assertEqual(plan["hic_alignment_refs"], ["s1:hic_alignment_bam:hic.bam"])

    def test_local_yahs_success_registers_candidate_artifacts_and_blocks_human(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            alignment = Path(tmpdir) / "hic.bam"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            alignment.write_text("bam", encoding="utf-8")
            bin_dir = self._fake_yahs_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_alignment_bam:hic.bam",
                        "asset_type": "hic_alignment_bam",
                        "path": str(alignment),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.yahs",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "hic_scaffolding")
            self.assertEqual(result["supervision_state"], "blocked_human")
            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "blocked_human")
            self.assertEqual(hic["selected_method_name"], "hic_scaffolding.candidate.yahs")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertTrue(Path(hic["chromosome_scaffolded_assembly_candidate_ref"]).exists())
            self.assertTrue(Path(hic["hic_review_bundle_ref"]).exists())
            self.assertTrue(Path(hic["hic_qc_report_ref"]).exists())
            self.assertIn("yahs_executed", hic["notes"])
            self.assertIn("hic_review_bundle_ref", result["active_interrupt"])
            artifact_index = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
            self.assertIn("hic_scaffolding.chromosome_scaffolded_assembly", artifact_types)
            self.assertIn("hic_scaffolding.scaffold_agp", artifact_types)
            self.assertIn("hic_scaffolding.review_bundle", artifact_types)
            self.assertIn("hic_scaffolding.qc_report", artifact_types)
            self.assertIn("command.stdout", artifact_types)
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("attempt_started", [event["event_type"] for event in events])
            self.assertIn("attempt_succeeded", [event["event_type"] for event in events])

    def test_local_mode_without_explicit_method_remains_planning_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            alignment = Path(tmpdir) / "hic.bam"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            alignment.write_text("bam", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_alignment_bam:hic.bam",
                        "asset_type": "hic_alignment_bam",
                        "path": str(alignment),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {"execution_mode": "local"},
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIn("execution_deferred_to_method_specific_task", hic["notes"])
            self.assertNotIn("selected_method_name", hic)
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "review_requested")
            self.assertFalse(events[-1]["auto_decidable"])

    def test_local_yahs_without_alignment_blocks_without_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            hic_r1 = Path(tmpdir) / "hic_R1.fq.gz"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            hic_r1.write_text("reads", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_reads_registered:hic_R1.fq.gz",
                        "asset_type": "hic_reads_registered",
                        "path": str(hic_r1),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.yahs",
                },
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIn("alignment_required_before_yahs", hic["notes"])
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "review_requested")
            self.assertFalse(events[-1]["auto_decidable"])

    def test_local_yahs_failure_registers_logs_and_preserves_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            alignment = Path(tmpdir) / "hic.bam"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            alignment.write_text("bam", encoding="utf-8")
            bin_dir = self._fake_yahs_bin(tmpdir, fail=True)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_alignment_bam:hic.bam",
                        "asset_type": "hic_alignment_bam",
                        "path": str(alignment),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.yahs",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                },
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(hic["final_assembly_for_annotation"], str(assembly))
            self.assertIn("yahs_execution_failed", hic["notes"])
            self.assertNotIn("chromosome_scaffolded_assembly_candidate_ref", hic)
            artifact_index = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifact_index.read_text(encoding="utf-8").splitlines()]
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn("attempt_failed", [event["event_type"] for event in events])

    def test_local_mode_with_juicer_selection_remains_planning_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            alignment = Path(tmpdir) / "hic.bam"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            alignment.write_text("bam", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_alignment_bam:hic.bam",
                        "asset_type": "hic_alignment_bam",
                        "path": str(alignment),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.juicer_3ddna_juicebox",
                },
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(hic["status"], "blocked_human")
            self.assertNotIn("selected_method_name", hic)
            events = [json.loads(line) for line in (Path(tmpdir) / "ledger" / "s1" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event_type"], "review_requested")
            self.assertFalse(events[-1]["auto_decidable"])

    def test_local_3ddna_uses_precomputed_merged_nodups_and_registers_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            merged_nodups = Path(tmpdir) / "merged_nodups.txt"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            merged_nodups.write_text("merged\n", encoding="utf-8")
            bin_dir = self._fake_juicer_3ddna_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:hic_merged_nodups:merged_nodups.txt",
                        "asset_type": "hic_merged_nodups",
                        "path": str(merged_nodups),
                    }
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.juicer_3ddna_juicebox",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                    "juicer_3ddna_parameters": {"chromosome_count": 22},
                },
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(hic["selected_method_name"], "hic_scaffolding.candidate.juicer_3ddna_juicebox")
            self.assertTrue(Path(hic["chromosome_scaffolded_assembly_candidate_ref"]).exists())
            self.assertIn("juicer_3ddna_executed", hic["notes"])
            script = Path(tmpdir) / "work" / "hic_scaffolding" / "s1" / "juicer_3ddna" / "run_juicer_3ddna.sh"
            script_text = script.read_text(encoding="utf-8")
            self.assertIn(str(merged_nodups), script_text)
            self.assertNotIn("juicer.sh -d", script_text)
            artifact_types = {
                json.loads(line)["artifact_type"]
                for line in (Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl").read_text(encoding="utf-8").splitlines()
            }
            self.assertIn("hic_scaffolding.contact_map", artifact_types)
            self.assertIn("hic_scaffolding.3ddna_assembly", artifact_types)

    def test_local_3ddna_raw_reads_runs_juicer_before_3ddna(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            read1 = Path(tmpdir) / "hic_R1.fq.gz"
            read2 = Path(tmpdir) / "hic_R2.fq.gz"
            chrom_sizes = Path(tmpdir) / "chrom.sizes"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            read1.write_text("r1\n", encoding="utf-8")
            read2.write_text("r2\n", encoding="utf-8")
            chrom_sizes.write_text("ctg1\t4\n", encoding="utf-8")
            bin_dir = self._fake_juicer_3ddna_bin(tmpdir)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {"asset_id": "s1:hic_reads_registered:r1", "asset_type": "hic_reads_registered", "path": str(read1)},
                    {"asset_id": "s1:hic_reads_registered:r2", "asset_type": "hic_reads_registered", "path": str(read2)},
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.juicer_3ddna_juicebox",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                    "juicer_3ddna_parameters": {
                        "input_mode": "raw_reads",
                        "chromosome_count": 22,
                        "restriction_enzyme": "MboI",
                        "chrom_sizes_ref": str(chrom_sizes),
                        "threads": 2,
                    },
                },
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["selected_method_name"], "hic_scaffolding.candidate.juicer_3ddna_juicebox")
            script = Path(tmpdir) / "work" / "hic_scaffolding" / "s1" / "juicer_3ddna" / "run_juicer_3ddna.sh"
            script_text = script.read_text(encoding="utf-8")
            self.assertIn("juicer.sh -d", script_text)
            self.assertIn("-s MboI", script_text)
            self.assertIn("generate_site_positions.py", script_text)
            self.assertIn("MboI reference", script_text)
            self.assertIn("reference.fasta", script_text)
            self.assertIn("reference_MboI.txt", script_text)
            self.assertIn("-y \"$site_file\"", script_text)
            self.assertIn("-D ", script_text)
            self.assertIn("hic_001_R1.fastq.gz", script_text)
            self.assertIn("hic_001_R2.fastq.gz", script_text)
            self.assertNotIn("/aidenlab/restriction_sites", script_text)
            self.assertIn("bwa index", script_text)
            self.assertIn("juicer/reference.fasta", script_text)
            self.assertIn("run-asm-pipeline.sh", script_text)
            self.assertTrue((Path(tmpdir) / "work" / "hic_scaffolding" / "s1" / "juicer_3ddna" / "juicer" / "aligned" / "merged_nodups.txt").exists())

    def test_local_3ddna_raw_reads_blocks_before_execution_without_site_generator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            read1 = Path(tmpdir) / "hic_R1.fq.gz"
            read2 = Path(tmpdir) / "hic_R2.fq.gz"
            chrom_sizes = Path(tmpdir) / "chrom.sizes"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            read1.write_text("r1\n", encoding="utf-8")
            read2.write_text("r2\n", encoding="utf-8")
            chrom_sizes.write_text("ctg1\t4\n", encoding="utf-8")
            bin_dir = self._fake_juicer_3ddna_bin(tmpdir, with_site_generator=False)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {"asset_id": "s1:hic_reads_registered:r1", "asset_type": "hic_reads_registered", "path": str(read1)},
                    {"asset_id": "s1:hic_reads_registered:r2", "asset_type": "hic_reads_registered", "path": str(read2)},
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.juicer_3ddna_juicebox",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                    "juicer_3ddna_parameters": {
                        "input_mode": "raw_reads",
                        "chromosome_count": 22,
                        "restriction_enzyme": "DpnII",
                        "chrom_sizes_ref": str(chrom_sizes),
                        "threads": 2,
                    },
                },
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "missing_tool")
            self.assertEqual(hic["missing_tools"], ["generate_site_positions.py"])
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertFalse((Path(tmpdir) / "work" / "hic_scaffolding" / "s1" / "juicer_3ddna" / "run_juicer_3ddna.sh").exists())

    def test_local_3ddna_raw_reads_blocks_before_execution_without_juicer_helper_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            read1 = Path(tmpdir) / "hic_R1.fq.gz"
            read2 = Path(tmpdir) / "hic_R2.fq.gz"
            chrom_sizes = Path(tmpdir) / "chrom.sizes"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            read1.write_text("r1\n", encoding="utf-8")
            read2.write_text("r2\n", encoding="utf-8")
            chrom_sizes.write_text("ctg1\t4\n", encoding="utf-8")
            bin_dir = self._fake_juicer_3ddna_bin(tmpdir, with_helper_scripts=False)
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {"asset_id": "s1:hic_reads_registered:r1", "asset_type": "hic_reads_registered", "path": str(read1)},
                    {"asset_id": "s1:hic_reads_registered:r2", "asset_type": "hic_reads_registered", "path": str(read2)},
                ],
            )
            state = self._state(tmpdir, str(registry))
            state["stage_outputs"] = {
                "inventory": {"target_assembly_level": "chromosome"},
                "assembly_review": {"status": "completed", "final_assembly_ref": str(assembly)},
                "hic_scaffolding": {
                    "execution_mode": "local",
                    "primary_method": "hic_scaffolding.candidate.juicer_3ddna_juicebox",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                    "juicer_3ddna_parameters": {
                        "input_mode": "raw_reads",
                        "chromosome_count": 22,
                        "restriction_enzyme": "DpnII",
                        "chrom_sizes_ref": str(chrom_sizes),
                        "threads": 2,
                    },
                },
            }

            result = build_stage().invoke(state)

            hic = result["stage_outputs"]["hic_scaffolding"]
            self.assertEqual(hic["status"], "missing_tool")
            self.assertEqual(hic["missing_tools"], ["scripts/common/chimeric_sam.awk"])
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertFalse((Path(tmpdir) / "work" / "hic_scaffolding" / "s1" / "juicer_3ddna" / "run_juicer_3ddna.sh").exists())


if __name__ == "__main__":
    unittest.main()
