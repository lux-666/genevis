"""Tests for Phase 7 task 04 slice 1: miniprot execution."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import initial_state
from app.stages.gene_annotation.nodes import (
    collect_miniprot_artifacts,
    freeze_miniprot_attempt,
    run_miniprot_attempt,
)


def _write_intake_plan(tmpdir: str) -> str:
    path = Path(tmpdir) / "annotation_intake_plan.json"
    path.write_text(
        json.dumps(
            {
                "artifact_type": "annotation.intake_plan",
                "recommended_plan": {},
                "final_plan": {},
                "final_requested_stages": ["gene_annotation", "function_annotation"],
                "changed": False,
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _fake_miniprot_bin(tmpdir: str, *, fail: bool = False) -> Path:
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir()
    mp = bin_dir / "miniprot"
    if fail:
        mp.write_text("#!/bin/sh\necho fake miniprot failed >&2\nexit 3\n", encoding="utf-8")
    else:
        mp.write_text(
            "#!/bin/sh\n"
            "printf '##gff-version 3\\n"
            "ctg1\\tminiprot\\tgene\\t1\\t100\\t.\\t+\\t.\\tID=g1\\n"
            "ctg1\\tminiprot\\tmRNA\\t1\\t100\\t.\\t+\\t.\\tID=m1;Parent=g1\\n"
            "ctg1\\tminiprot\\tCDS\\t1\\t100\\t.\\t+\\t0\\tID=c1;Parent=m1\\n"
            "ctg1\\tminiprot\\texon\\t1\\t50\\t.\\t+\\t.\\tID=e1;Parent=m1\\n"
            "ctg1\\tminiprot\\texon\\t51\\t100\\t.\\t+\\t.\\tID=e2;Parent=m1\\n"
            "ctg1\\tminiprot\\tgene\\t101\\t200\\t.\\t-\\t.\\tID=g2\\n"
            "ctg1\\tminiprot\\tmRNA\\t101\\t200\\t.\\t-\\t.\\tID=m2;Parent=g2\\n'\n",
            encoding="utf-8",
        )
    mp.chmod(0o755)
    return bin_dir


class MiniprotFreezeTests(unittest.TestCase):
    def _freeze_state(self, tmpdir: str, **gene_updates):
        softmasked = Path(tmpdir) / "softmasked.fa"
        proteins = Path(tmpdir) / "proteins.fa"
        softmasked.write_text(">ctg1\nACGT\n", encoding="utf-8")
        proteins.write_text(">p1\nMKT\n", encoding="utf-8")
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "gene_annotation"
        state["stage_outputs"] = {
            "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
            "gene_annotation": {
                "status": "candidates_ready",
                "genome_input_ref": str(softmasked),
                "genome_input_source": "softmasked_genome",
                "protein_evidence_refs": [str(proteins)],
                "confirmed_methods": ["gene_annotation.candidate.miniprot"],
                "candidate_refs": {"gene_annotation.candidate.miniprot": str(Path(tmpdir) / "miniprot.gff3")},
                "env": {"PATH": os.environ.get("PATH", "")},
                **gene_updates,
            },
        }
        return state

    def test_freeze_requires_candidates_ready_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, status="methods_confirmed")
            result = freeze_miniprot_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_skips_when_miniprot_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, confirmed_methods=["gene_annotation.candidate.augustus"])
            result = freeze_miniprot_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_produces_frozen_attempt_spec_with_correct_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir)
            result = freeze_miniprot_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "miniprot_frozen")
            attempt = FrozenAttemptSpec.model_validate(gene["miniprot_frozen_attempt"])
            self.assertEqual(attempt.method_name, "gene_annotation.candidate.miniprot")
            self.assertEqual(attempt.stage, "gene_annotation")
            self.assertIn("miniprot", attempt.script_path.name)

            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn("miniprot --gff", script)
            self.assertIn("softmasked.fa", script)
            self.assertIn("proteins.fa", script)
            self.assertIn("miniprot.gff3", script)

    def test_freeze_fails_without_genome_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, genome_input_ref=None)
            result = freeze_miniprot_attempt(state)
            self.assertEqual(result["stage_outputs"]["gene_annotation"]["status"], "failed")
            self.assertIn("genome_input_ref", result["stage_outputs"]["gene_annotation"]["reason"])

    def test_freeze_fails_without_protein_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, protein_evidence_refs=[])
            result = freeze_miniprot_attempt(state)
            self.assertEqual(result["stage_outputs"]["gene_annotation"]["status"], "failed")
            self.assertIn("protein_evidence_refs", result["stage_outputs"]["gene_annotation"]["reason"])


class MiniprotRunTests(unittest.TestCase):
    def _run_state(self, tmpdir: str, bin_dir: Path, *, status: str = "miniprot_frozen"):
        output_gff3 = Path(tmpdir) / "miniprot.gff3"
        script = Path(tmpdir) / "run_miniprot.sh"
        script.write_text(
            "#!/bin/bash\nset -e\nminiprot --gff genome.fa proteins.fa > miniprot.gff3\ntest -f miniprot.gff3\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        attempt = FrozenAttemptSpec(
            attempt_id="s1:gene_annotation:miniprot",
            session_id="s1",
            stage="gene_annotation",
            method_name="gene_annotation.candidate.miniprot",
            script_path=script,
            working_dir=Path(tmpdir),
            expected_outputs=[output_gff3],
            env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
            metadata={
                "output_gff3": str(output_gff3),
                "genome_input_ref": str(Path(tmpdir) / "genome.fa"),
                "protein_evidence_refs": [str(Path(tmpdir) / "proteins.fa")],
            },
        )
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "gene_annotation"
        state["stage_outputs"] = {
            "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
            "gene_annotation": {
                "status": status,
                "miniprot_frozen_attempt": attempt.model_dump(mode="json"),
            },
        }
        return state

    def test_run_requires_miniprot_frozen_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_miniprot_bin(tmpdir)
            state = self._run_state(tmpdir, bin_dir, status="candidates_ready")
            result = run_miniprot_attempt(state)
            self.assertEqual(result, {})

    def test_run_success_sets_miniprot_ran(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_miniprot_bin(tmpdir)
            state = self._run_state(tmpdir, bin_dir)
            result = run_miniprot_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "miniprot_ran")
            self.assertIsNotNone(gene.get("miniprot_execution_result"))

    def test_run_failure_records_attempt_failed_and_sets_failed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_miniprot_bin(tmpdir, fail=True)
            state = self._run_state(tmpdir, bin_dir)
            result = run_miniprot_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "failed")
            self.assertIn("miniprot failed", gene["reason"])

            # Check ledger recorded attempt_failed
            ledger_path = Path(tmpdir) / "ledger" / "s1" / "events.jsonl"
            self.assertTrue(ledger_path.exists(), "ledger should record attempt_failed event")
            events = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            event_types = [e["event_type"] for e in events]
            self.assertIn("attempt_failed", event_types)


class MiniprotCollectTests(unittest.TestCase):
    def _collect_state(self, tmpdir: str, *, gff3_exists: bool = True):
        output_gff3 = Path(tmpdir) / "miniprot.gff3"
        if gff3_exists:
            output_gff3.write_text(
                "##gff-version 3\n"
                "ctg1\tminiprot\tgene\t1\t100\t.\t+\t.\tID=g1\n"
                "ctg1\tminiprot\tmRNA\t1\t100\t.\t+\t.\tID=m1;Parent=g1\n"
                "ctg1\tminiprot\tCDS\t1\t100\t.\t+\t0\tID=c1;Parent=m1\n"
                "ctg1\tminiprot\texon\t1\t50\t.\t+\t.\tID=e1;Parent=m1\n"
                "ctg1\tminiprot\texon\t51\t100\t.\t+\t.\tID=e2;Parent=m1\n"
                "ctg1\tminiprot\tgene\t101\t200\t.\t-\t.\tID=g2\n"
                "ctg1\tminiprot\tmRNA\t101\t200\t.\t-\t.\tID=m2;Parent=g2\n",
                encoding="utf-8",
            )
        attempt = FrozenAttemptSpec(
            attempt_id="s1:gene_annotation:miniprot",
            session_id="s1",
            stage="gene_annotation",
            method_name="gene_annotation.candidate.miniprot",
            script_path=Path(tmpdir) / "run_miniprot.sh",
            working_dir=Path(tmpdir),
            expected_outputs=[output_gff3],
            metadata={
                "output_gff3": str(output_gff3),
                "genome_input_ref": str(Path(tmpdir) / "genome.fa"),
                "protein_evidence_refs": [],
            },
        )
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "gene_annotation"
        state["stage_outputs"] = {
            "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
            "gene_annotation": {
                "status": "miniprot_ran",
                "miniprot_frozen_attempt": attempt.model_dump(mode="json"),
                "candidate_refs": {"gene_annotation.candidate.miniprot": str(output_gff3)},
                "candidate_gene_counts": {"gene_annotation.candidate.miniprot": 0},
            },
        }
        return state

    def test_collect_requires_miniprot_ran_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir)
            state["stage_outputs"]["gene_annotation"]["status"] = "miniprot_frozen"
            result = collect_miniprot_artifacts(state)
            self.assertEqual(result, {})

    def test_collect_registers_candidate_gff_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir)
            result = collect_miniprot_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "candidates_ready")
            self.assertEqual(gene["candidate_gff_ref"], str(Path(tmpdir) / "miniprot.gff3"))
            self.assertIn("gene_annotation.candidate.miniprot", gene["candidate_refs"])
            self.assertGreater(gene["miniprot_gene_count"], 0)
            self.assertEqual(gene["miniprot_gene_count"], 2,
                             "gene count should count only GFF3 gene features, not mRNA/CDS/exon lines")
            self.assertEqual(
                gene["candidate_gene_counts"]["gene_annotation.candidate.miniprot"],
                gene["miniprot_gene_count"],
            )

            artifacts_path = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            self.assertTrue(artifacts_path.exists())
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifacts_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("gene_annotation.candidate_gff", artifact_types)

    def test_collect_fails_when_gff3_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir, gff3_exists=False)
            result = collect_miniprot_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "failed")
            self.assertIn("missing", gene["reason"])


class MiniprotFullSliceTests(unittest.TestCase):
    """End-to-end miniprot slice: freeze → run → collect in sequence."""

    def test_full_slice_path_produces_candidate_gff_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            softmasked = Path(tmpdir) / "softmasked.fa"
            proteins = Path(tmpdir) / "proteins.fa"
            softmasked.write_text(">ctg1\nACGT\n", encoding="utf-8")
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")

            bin_dir = _fake_miniprot_bin(tmpdir)

            # Step 1: freeze
            freeze_state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=tmpdir,
            )
            freeze_state["current_stage"] = "gene_annotation"
            freeze_state["stage_outputs"] = {
                "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
                "gene_annotation": {
                    "status": "candidates_ready",
                    "genome_input_ref": str(softmasked),
                    "genome_input_source": "softmasked_genome",
                    "protein_evidence_refs": [str(proteins)],
                    "confirmed_methods": ["gene_annotation.candidate.miniprot"],
                    "candidate_refs": {"gene_annotation.candidate.miniprot": str(Path(tmpdir) / "miniprot.gff3")},
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
                },
            }
            freeze_result = freeze_miniprot_attempt(freeze_state)
            self.assertEqual(freeze_result["stage_outputs"]["gene_annotation"]["status"], "miniprot_frozen")

            # Step 2: run
            run_state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=tmpdir,
            )
            run_state["current_stage"] = "gene_annotation"
            run_state["stage_outputs"] = {
                "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
                "gene_annotation": freeze_result["stage_outputs"]["gene_annotation"],
            }
            run_result = run_miniprot_attempt(run_state)
            self.assertEqual(run_result["stage_outputs"]["gene_annotation"]["status"], "miniprot_ran")

            # Step 3: collect
            collect_state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=tmpdir,
            )
            collect_state["current_stage"] = "gene_annotation"
            collect_state["stage_outputs"] = {
                "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
                "gene_annotation": {
                    **run_result["stage_outputs"]["gene_annotation"],
                    "candidate_refs": {"gene_annotation.candidate.miniprot": str(Path(tmpdir) / "miniprot.gff3")},
                    "candidate_gene_counts": {"gene_annotation.candidate.miniprot": 0},
                },
            }
            collect_result = collect_miniprot_artifacts(collect_state)

            gene = collect_result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "candidates_ready")
            self.assertIn("candidate_gff_ref", gene)
            self.assertTrue(Path(gene["candidate_gff_ref"]).exists())
            gff3_content = Path(gene["candidate_gff_ref"]).read_text(encoding="utf-8")
            self.assertIn("ID=g1", gff3_content)

            # Verify artifact records include candidate_gff
            artifacts_path = Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl"
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifacts_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("gene_annotation.candidate_gff", artifact_types)
