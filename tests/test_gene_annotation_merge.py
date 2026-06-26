"""Tests for Phase 7 task 05: EVM merge + BUSCO execution."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import initial_state
from app.stages.gene_annotation.nodes import (
    collect_evm_artifacts,
    collect_gene_busco_artifacts,
    freeze_evm_attempt,
    freeze_gene_busco_attempt,
    run_evm_attempt,
    run_gene_busco_attempt,
)


def _write_intake_plan(tmpdir: str) -> str:
    path = Path(tmpdir) / "annotation_intake_plan.json"
    path.write_text(json.dumps({
        "artifact_type": "annotation.intake_plan",
        "recommended_plan": {},
        "final_plan": {},
        "final_requested_stages": ["gene_annotation", "function_annotation"],
        "changed": False,
    }), encoding="utf-8")
    return str(path)


def _fake_gffread_bin(tmpdir: str) -> Path:
    """Fake gffread that materializes proteins.fa (-y) and cds.fa (-x)."""
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    gffread = bin_dir / "gffread"
    gffread.write_text(
        "#!/bin/sh\n"
        "prot=\"\"; cds=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -y) prot=\"$2\"; shift 2;;\n"
        "    -x) cds=\"$2\"; shift 2;;\n"
        "    -g) shift 2;;\n"
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        "[ -n \"$prot\" ] && printf '>g1\\nMKT\\n' > \"$prot\"\n"
        "[ -n \"$cds\" ] && printf '>g1\\nATGAAAACC\\n' > \"$cds\"\n",
        encoding="utf-8",
    )
    gffread.chmod(0o755)
    return bin_dir


def _merge_accepted_state(tmpdir: str, **gene_updates):
    """State as it stands after gate3 (merge_review) auto-decides."""
    softmasked = Path(tmpdir) / "softmasked.fa"
    softmasked.write_text(">ctg1\nACGT\n", encoding="utf-8")
    state = initial_state(
        session_id="s1",
        input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
        data_dir=tmpdir,
    )
    state["current_stage"] = "gene_annotation"
    state["stage_outputs"] = {
        "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
        "gene_annotation": {
            "status": "merge_accepted",
            "genome_input_ref": str(softmasked),
            "genome_input_source": "softmasked_genome",
            "candidate_refs": {"gene_annotation.candidate.augustus": str(Path(tmpdir) / "augustus.gff3")},
            "accepted_candidate_refs": {"gene_annotation.candidate.augustus": str(Path(tmpdir) / "augustus.gff3")},
            "merge_weights": {"ab_initio": 1, "rnaseq": 5, "protein": 5},
            "expected_outputs": {
                "genes_gff3": str(Path(tmpdir) / "genes.gff3"),
                "transcripts_fasta": str(Path(tmpdir) / "transcripts.fa"),
                "predicted_proteins": str(Path(tmpdir) / "proteins.fa"),
                "evidence_report": str(Path(tmpdir) / "evidence_report.json"),
            },
            "env": {"PATH": os.environ.get("PATH", "")},
            **gene_updates,
        },
    }
    return state


class EVMFreezeTests(unittest.TestCase):
    def test_single_candidate_passthrough_skips_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _merge_accepted_state(tmpdir)
            result = freeze_evm_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "evm_collected")
            self.assertEqual(gene["merge_method"], "single_candidate_passthrough")
            self.assertIn("final_gff_ref", gene)

    def test_single_candidate_no_candidate_refs_still_passthrough(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _merge_accepted_state(tmpdir, candidate_refs={}, accepted_candidate_refs={})
            result = freeze_evm_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "evm_collected")
            self.assertEqual(gene["merge_method"], "single_candidate_passthrough")

    def test_multi_candidate_freezes_evm_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gff1 = Path(tmpdir) / "braker3.gff3"
            gff2 = Path(tmpdir) / "miniprot.gff3"
            gff3 = Path(tmpdir) / "genewise.gff3"
            for gff in (gff1, gff2, gff3):
                gff.write_text("##gff-version 3\n", encoding="utf-8")
            state = _merge_accepted_state(
                tmpdir,
                candidate_refs={
                    "gene_annotation.candidate.braker3": str(gff1),
                    "gene_annotation.candidate.miniprot": str(gff2),
                    "gene_annotation.candidate.genewise": str(gff3),
                },
                accepted_candidate_refs={
                    "gene_annotation.candidate.braker3": str(gff1),
                    "gene_annotation.candidate.miniprot": str(gff2),
                    "gene_annotation.candidate.genewise": str(gff3),
                },
            )
            result = freeze_evm_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "evm_frozen")
            attempt = FrozenAttemptSpec.model_validate(gene["evm_frozen_attempt"])
            script = attempt.script_path.read_text(encoding="utf-8")
            weights = (attempt.working_dir / "weights.txt").read_text(encoding="utf-8")
            self.assertIn("evidence_modeler.pl", script)
            self.assertIn("--weights", script)
            # braker3 + native miniprot GFF both emit gene-model-like records → --gene_predictions.
            self.assertIn(f"--gene_predictions {gff1},{gff2}", script)
            # genewise is a true protein alignment → --protein_alignments.
            self.assertIn(f"--protein_alignments {gff3}", script)
            self.assertNotIn(f"--protein_alignments {gff2}", script)
            self.assertIn("OTHER_PREDICTION\tbraker3\t1", weights)
            self.assertIn("OTHER_PREDICTION\tminiprot\t1", weights)
            self.assertIn("PROTEIN\tgenewise\t5", weights)
            self.assertNotIn(str(gff1), weights)
            self.assertNotIn(str(gff2), weights)
            self.assertNotIn(str(gff3), weights)

    def test_freeze_requires_merge_accepted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _merge_accepted_state(tmpdir, status="merge_ready")
            result = freeze_evm_attempt(state)
            self.assertEqual(result, {})


class EVMRunTests(unittest.TestCase):
    def _fake_evm_bin(self, tmpdir: str, *, fail: bool = False) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        evm = bin_dir / "evidence_modeler.pl"
        if fail:
            evm.write_text("#!/bin/sh\necho fake evm failed >&2\nexit 3\n", encoding="utf-8")
        else:
            evm.write_text(
                "#!/bin/sh\n"
                "# Parse --genome argument\n"
                "while [ $# -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --genome) genome=\"$2\"; shift 2;;\n"
                "    *) shift;;\n"
                "  esac\n"
                "done\n"
                "# Output fake EVM GFF3\n"
                "printf '##gff-version 3\\nctg1\\tEVM\\tgene\\t1\\t1000\\t.\\t+\\t.\\tID=evm_g1\\n'\n",
                encoding="utf-8",
            )
        evm.chmod(0o755)
        return bin_dir

    def _run_state(self, tmpdir: str, bin_dir: Path, *, status: str = "evm_frozen"):
        output_gff3 = Path(tmpdir) / "evm_merged.gff3"
        script = Path(tmpdir) / "run_evm.sh"
        script.write_text("#!/bin/bash\nset -e\nevidence_modeler.pl --genome genome.fa > evm_merged.gff3\ntest -f evm_merged.gff3\n", encoding="utf-8")
        script.chmod(0o755)
        attempt = FrozenAttemptSpec(
            attempt_id="s1:gene_annotation:evm",
            session_id="s1", stage="gene_annotation",
            method_name="gene_annotation.evm",
            script_path=script, working_dir=Path(tmpdir),
            expected_outputs=[output_gff3],
            env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
            metadata={"output_gff3": str(output_gff3), "genome_input_ref": str(Path(tmpdir) / "genome.fa"), "candidate_refs": {}, "merge_weights": {}},
        )
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        state["current_stage"] = "gene_annotation"
        state["stage_outputs"] = {
            "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
            "gene_annotation": {"status": status, "evm_frozen_attempt": attempt.model_dump(mode="json")},
        }
        return state

    def test_run_success_sets_evm_ran(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = self._fake_evm_bin(tmpdir)
            state = self._run_state(tmpdir, bin_dir)
            result = run_evm_attempt(state)
            self.assertEqual(result["stage_outputs"]["gene_annotation"]["status"], "evm_ran")

    def test_run_failure_sets_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = self._fake_evm_bin(tmpdir, fail=True)
            state = self._run_state(tmpdir, bin_dir)
            result = run_evm_attempt(state)
            self.assertEqual(result["stage_outputs"]["gene_annotation"]["status"], "failed")


class EVMCollectTests(unittest.TestCase):
    def _collect_state(self, tmpdir: str, *, gff3_exists: bool = True):
        output_gff3 = Path(tmpdir) / "evm_merged.gff3"
        if gff3_exists:
            output_gff3.write_text("##gff-version 3\nctg1\tEVM\tgene\t1\t1000\t.\t+\t.\tID=g1\n", encoding="utf-8")
        attempt = FrozenAttemptSpec(
            attempt_id="s1:gene_annotation:evm",
            session_id="s1", stage="gene_annotation",
            method_name="gene_annotation.evm",
            script_path=Path(tmpdir) / "run_evm.sh", working_dir=Path(tmpdir),
            expected_outputs=[output_gff3],
            metadata={"output_gff3": str(output_gff3), "genome_input_ref": str(Path(tmpdir) / "genome.fa"), "candidate_refs": {}, "merge_weights": {}},
        )
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        state["current_stage"] = "gene_annotation"
        genome = Path(tmpdir) / "genome.fa"
        genome.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        gffread_bin = _fake_gffread_bin(tmpdir)
        state["stage_outputs"] = {
            "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
            "gene_annotation": {
                "status": "evm_ran",
                "genome_input_ref": str(genome),
                "env": {"PATH": f"{gffread_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "evm_frozen_attempt": attempt.model_dump(mode="json"),
            },
        }
        return state

    def test_collect_registers_final_gff_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir)
            result = collect_evm_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "evm_collected")
            self.assertEqual(gene["merge_method"], "evm")
            self.assertIn("final_gff_ref", gene)

            artifacts_path = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifacts_path.read_text(encoding="utf-8").splitlines()]
            self.assertIn("gene_annotation.final_gff", artifact_types)

    def test_collect_fails_when_gff3_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir, gff3_exists=False)
            result = collect_evm_artifacts(state)
            self.assertEqual(result["stage_outputs"]["gene_annotation"]["status"], "failed")


class GeneBUSBOTests(unittest.TestCase):
    def _busco_inputs(self, tmpdir: str):
        proteins = Path(tmpdir) / "proteins.fa"
        proteins.write_text(">p1\nMKT\n", encoding="utf-8")
        lineage_path = Path(tmpdir) / "busco_lineage"
        lineage_path.mkdir()
        return str(proteins), str(lineage_path)

    def test_freeze_skips_when_lineage_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _merge_accepted_state(tmpdir, status="evm_collected", final_gff_ref=str(Path(tmpdir) / "final.gff3"))
            result = freeze_gene_busco_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "merge_accepted")
            self.assertEqual(gene["busco_completeness_status"], "missing")

    def test_freeze_skips_when_no_final_gff_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _merge_accepted_state(tmpdir, status="evm_collected", final_gff_ref=None)
            result = freeze_gene_busco_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "merge_accepted")
            self.assertEqual(gene["busco_completeness_status"], "missing")

    def test_freeze_with_lineage_uses_materialized_final_proteins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _, lineage = self._busco_inputs(tmpdir)
            final_proteins = Path(tmpdir) / "final_proteins.fa"
            final_proteins.write_text(">p1\nMKT\n", encoding="utf-8")
            state = _merge_accepted_state(
                tmpdir,
                status="evm_collected",
                final_gff_ref=str(Path(tmpdir) / "final.gff3"),
                final_proteins_ref=str(final_proteins),
                lineage_or_species_hints={"busco_lineage": lineage},
            )
            result = freeze_gene_busco_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "busco_frozen")
            attempt = FrozenAttemptSpec.model_validate(gene["busco_frozen_attempt"])
            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn("busco -i", script)
            self.assertEqual(attempt.metadata["busco_input"], str(final_proteins))
            self.assertIn("-m proteins", script)
            self.assertIn(f"-l {lineage}", script)

    def test_freeze_requires_evm_collected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _merge_accepted_state(tmpdir, status="merge_accepted")
            result = freeze_gene_busco_attempt(state)
            self.assertEqual(result, {})

    def test_busco_run_failure_downgrades_to_missing(self):
        # run_gene_busco_attempt: guard for busco_frozen, then run, on fail → merge_accepted + missing
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir(exist_ok=True)
            busco_bin = bin_dir / "busco"
            busco_bin.write_text("#!/bin/sh\necho fake busco failed >&2\nexit 1\n", encoding="utf-8")
            busco_bin.chmod(0o755)

            short_summary = Path(tmpdir) / "short_summary.txt"
            script = Path(tmpdir) / "run_busco.sh"
            script.write_text("#!/bin/bash\nbusco -i input.fa -m proteins\n", encoding="utf-8")
            script.chmod(0o755)
            attempt = FrozenAttemptSpec(
                attempt_id="s1:gene_annotation:busco",
                session_id="s1", stage="gene_annotation",
                method_name="gene_annotation.busco",
                script_path=script, working_dir=Path(tmpdir),
                expected_outputs=[short_summary],
                env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
                metadata={"short_summary_ref": str(short_summary), "busco_mode": "proteins", "busco_lineage": "/tmp/lineage", "busco_output_dir": str(tmpdir)},
            )
            state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
            state["current_stage"] = "gene_annotation"
            state["stage_outputs"] = {
                "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
                "gene_annotation": {"status": "busco_frozen", "busco_frozen_attempt": attempt.model_dump(mode="json")},
            }
            result = run_gene_busco_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "merge_accepted")
            self.assertEqual(gene["busco_completeness_status"], "missing")

    def test_collect_parses_busco_short_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            short_summary = Path(tmpdir) / "short_summary.txt"
            short_summary.write_text(
                "# BUSCO version 5.x\n"
                "C:95.2%[S:90.1%,D:5.1%],F:2.1%,M:2.7%,n:3642\n"
                "    3467    Complete BUSCOs (C)\n"
                "    3281    Complete and single-copy BUSCOs (S)\n"
                "    186    Complete and duplicated BUSCOs (D)\n"
                "    76    Fragmented BUSCOs (F)\n"
                "    99    Missing BUSCOs (M)\n"
                "    3642    Total BUSCO groups searched\n",
                encoding="utf-8",
            )
            attempt = FrozenAttemptSpec(
                attempt_id="s1:gene_annotation:busco",
                session_id="s1", stage="gene_annotation",
                method_name="gene_annotation.busco",
                script_path=Path(tmpdir) / "run_busco.sh", working_dir=Path(tmpdir),
                expected_outputs=[short_summary],
                metadata={"short_summary_ref": str(short_summary), "busco_mode": "proteins", "busco_lineage": "/tmp/l", "busco_output_dir": str(tmpdir)},
            )
            state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
            state["current_stage"] = "gene_annotation"
            state["stage_outputs"] = {
                "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
                "gene_annotation": {
                    "status": "busco_ran",
                    "busco_frozen_attempt": attempt.model_dump(mode="json"),
                    "final_gff_ref": str(Path(tmpdir) / "final.gff3"),
                },
            }
            result = collect_gene_busco_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "merge_accepted")
            self.assertEqual(gene["busco_completeness_status"], "available")
            self.assertAlmostEqual(gene["busco_completeness"], 95.2)
            self.assertEqual(gene["busco_metrics"]["complete_buscos"], 3467)
            self.assertEqual(gene["busco_metrics"]["missing_buscos"], 99)

            artifacts_path = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [json.loads(line)["artifact_type"] for line in artifacts_path.read_text(encoding="utf-8").splitlines()]
            self.assertIn("gene_annotation.busco_summary", artifact_types)


class EVMBUSCOFullPathTests(unittest.TestCase):
    """End-to-end: merge_accepted → EVM passthrough → BUSCO → merge_accepted with signals."""

    def test_full_path_single_candidate_passthrough_to_busco_signal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins = Path(tmpdir) / "proteins.fa"
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")
            lineage = Path(tmpdir) / "busco_lineage"
            lineage.mkdir()

            # Step 1: EVM freeze (single candidate → passthrough)
            state = _merge_accepted_state(
                tmpdir,
                candidate_bundles={
                    "gene_annotation.candidate.augustus": {
                        "method_name": "gene_annotation.candidate.augustus",
                        "category": "ab_initio",
                        "annotation_gff_ref": str(Path(tmpdir) / "augustus.gff3"),
                        "proteins_fasta_ref": str(proteins),
                        "cds_fasta_ref": None,
                        "evm_evidence_class": "ABINITIO_PREDICTION",
                        "evm_source_type": "augustus",
                        "status": "materialized",
                    }
                },
                lineage_or_species_hints={"busco_lineage": str(lineage)},
            )
            evm_result = freeze_evm_attempt(state)
            gene = evm_result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "evm_collected")
            self.assertEqual(gene["merge_method"], "single_candidate_passthrough")

            # Step 2: BUSCO freeze (has lineage → freeze)
            busco_state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
            busco_state["current_stage"] = "gene_annotation"
            busco_state["stage_outputs"] = {
                "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
                "gene_annotation": evm_result["stage_outputs"]["gene_annotation"],
            }
            busco_freeze = freeze_gene_busco_attempt(busco_state)
            gene = busco_freeze["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "busco_frozen")

            # BUSCO freeze produces a script → run → fail → downgrade
            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir(exist_ok=True)
            busco_bin = bin_dir / "busco"
            busco_bin.write_text("#!/bin/sh\necho fake busco failed >&2\nexit 1\n", encoding="utf-8")
            busco_bin.chmod(0o755)

            attempt = FrozenAttemptSpec.model_validate(gene["busco_frozen_attempt"])
            attempt_dict = attempt.model_dump(mode="json")
            attempt_dict["env"]["PATH"] = f"{bin_dir}{os.pathsep}{attempt_dict['env'].get('PATH', os.environ.get('PATH', ''))}"
            updated_attempt = FrozenAttemptSpec.model_validate(attempt_dict)

            run_state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
            run_state["current_stage"] = "gene_annotation"
            run_state["stage_outputs"] = {
                "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
                "gene_annotation": {
                    "status": "busco_frozen",
                    "busco_frozen_attempt": updated_attempt.model_dump(mode="json"),
                },
            }
            busco_run = run_gene_busco_attempt(run_state)
            gene = busco_run["stage_outputs"]["gene_annotation"]
            # BUSCO fails → downgrades to missing
            self.assertEqual(gene["status"], "merge_accepted")
            self.assertEqual(gene["busco_completeness_status"], "missing")
