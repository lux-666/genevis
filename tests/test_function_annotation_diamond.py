"""Tests for Phase 7 task 06 Slice 1: DIAMOND execution."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import initial_state
from app.stages.function_annotation.nodes import (
    collect_diamond_artifacts,
    commit_function_annotation,
    freeze_diamond_attempt,
    run_diamond_attempt,
)


def _fake_diamond_bin(tmpdir: str, *, fail: bool = False) -> str:
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    diamond = bin_dir / "diamond"
    if fail:
        diamond.write_text("#!/bin/sh\necho fake diamond failed >&2\nexit 3\n", encoding="utf-8")
    else:
        diamond.write_text(
            "#!/bin/sh\n"
            "out=\"\"\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = \"--out\" ]; then\n"
            "    out=\"$2\"; shift 2; break\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            "if [ -n \"$out\" ]; then\n"
            '  printf "prot1\\tsp|P12345\\t100.0\\t300\\t0\\t0\\t1\\t300\\t1\\t300\\t0.0\\t600\\n" > \"$out\"\n'
            "fi\n",
            encoding="utf-8",
        )
    diamond.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _provenance_state(tmpdir: str, **func_updates):
    """State after validate_database_provenance with a diamond database."""
    proteins = Path(tmpdir) / "proteins.fa"
    proteins.write_text(">prot1\nMKTLLLL\n", encoding="utf-8")
    db_fasta = Path(tmpdir) / "swissprot.fasta"
    db_fasta.write_text(">sp|P12345\nMKTLLLL\n", encoding="utf-8")
    proteins_ref = str(proteins)
    state = initial_state(
        session_id="s1",
        input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
        data_dir=tmpdir,
    )
    state["current_stage"] = "function_annotation"
    state["stage_outputs"] = {
        "function_annotation": {
            "status": "provenance_validated",
            "proteins_fasta_ref": proteins_ref,
            "expected_outputs": {"gene_function_tsv": str(Path(tmpdir) / "gene_function.tsv")},
            "database_provenance": [
                {
                    "method_name": "function_annotation.candidate.diamond",
                    "database_name": "SwissProt",
                    "database_version": "2024-01",
                    "snapshot_date": "2026-01-01",
                    "database_source_ref": str(db_fasta),
                    "descriptor_version": "diamond.yaml",
                    "input_proteins_ref": proteins_ref,
                },
            ],
            "env": {"PATH": os.environ.get("PATH", "")},
            **func_updates,
        },
    }
    return state


class DiamondFreezeTests(unittest.TestCase):
    def test_freeze_requires_provenance_validated_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _provenance_state(tmpdir, status="resolved")
            result = freeze_diamond_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_skips_when_no_diamond_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins = Path(tmpdir) / "proteins.fa"
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")
            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
                data_dir=tmpdir,
            )
            state["current_stage"] = "function_annotation"
            state["stage_outputs"] = {
                "function_annotation": {
                    "status": "provenance_validated",
                    "proteins_fasta_ref": str(proteins),
                    "expected_outputs": {"gene_function_tsv": str(Path(tmpdir) / "gene_function.tsv")},
                    "database_provenance": [],
                },
            }
            result = freeze_diamond_attempt(state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_completed")

    def test_freeze_produces_correct_diamond_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            result = freeze_diamond_attempt(state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_frozen")
            attempt = FrozenAttemptSpec.model_validate(func_out["diamond_frozen_attempt"])
            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn("diamond makedb --in ", script)
            self.assertIn("diamond blastp --db ", script)
            self.assertIn("--outfmt 6 --threads ", script)
            self.assertIn("--out ", script)
            self.assertIn("--query ", script)
            self.assertIn("test -f ", script)
            self.assertEqual(attempt.stage, "function_annotation")
            self.assertEqual(attempt.method_name, "function_annotation.candidate.diamond")
            self.assertIn("swissprot.fasta", script)

    def test_freeze_creates_working_directory_and_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            result = freeze_diamond_attempt(state)
            func_out = result["stage_outputs"]["function_annotation"]
            attempt = FrozenAttemptSpec.model_validate(func_out["diamond_frozen_attempt"])
            self.assertTrue(attempt.script_path.exists())
            self.assertTrue(os.access(attempt.script_path, os.X_OK))


class DiamondRunTests(unittest.TestCase):
    def test_run_requires_diamond_frozen_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _provenance_state(tmpdir, status="provenance_validated")
            result = run_diamond_attempt(state)
            self.assertEqual(result, {})

    def test_run_failure_records_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir, fail=True)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            freeze_result = freeze_diamond_attempt(state)
            frozen_state = {
                **state,
                "stage_outputs": freeze_result["stage_outputs"],
            }
            result = run_diamond_attempt(frozen_state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "failed")
            self.assertIn("diamond failed", func_out["reason"])

    def test_run_success_sets_diamond_ran(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            freeze_result = freeze_diamond_attempt(state)
            frozen_state = {
                **state,
                "stage_outputs": freeze_result["stage_outputs"],
            }
            result = run_diamond_attempt(frozen_state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_ran")
            self.assertIn("diamond_execution_result", func_out)


class DiamondCollectTests(unittest.TestCase):
    def test_collect_requires_diamond_ran_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _provenance_state(tmpdir, status="diamond_frozen")
            result = collect_diamond_artifacts(state)
            self.assertEqual(result, {})

    def test_collect_registers_diamond_hits_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            freeze_result = freeze_diamond_attempt(state)
            frozen_state = {**state, "stage_outputs": freeze_result["stage_outputs"]}
            run_result = run_diamond_attempt(frozen_state)
            ran_state = {**state, "stage_outputs": run_result["stage_outputs"]}
            result = collect_diamond_artifacts(ran_state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_completed")
            self.assertIn("diamond_hits_ref", func_out)
            self.assertTrue(Path(func_out["diamond_hits_ref"]).exists())
            # Verify the TSV content is outfmt 6
            tsv_content = Path(func_out["diamond_hits_ref"]).read_text(encoding="utf-8")
            self.assertIn("sp|P12345", tsv_content)
            self.assertIn("100.0", tsv_content)

    def test_collect_missing_tsv_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            freeze_result = freeze_diamond_attempt(state)
            func_out = freeze_result["stage_outputs"]["function_annotation"]
            attempt = FrozenAttemptSpec.model_validate(func_out["diamond_frozen_attempt"])
            # Remove the output before collect
            for p in attempt.expected_outputs:
                if p.exists():
                    p.unlink()
            # Fake the ran status
            ran_state = _provenance_state(
                tmpdir,
                env={"PATH": fake_path},
                status="diamond_ran",
                diamond_frozen_attempt=func_out["diamond_frozen_attempt"],
            )
            result = collect_diamond_artifacts(ran_state)
            collected = result["stage_outputs"]["function_annotation"]
            self.assertEqual(collected["status"], "failed")
            self.assertIn("missing", collected["reason"])


class DiamondFullPathTests(unittest.TestCase):
    def test_end_to_end_diamond_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            # freeze
            result = freeze_diamond_attempt(state)
            state["stage_outputs"] = result["stage_outputs"]
            self.assertEqual(state["stage_outputs"]["function_annotation"]["status"], "diamond_frozen")
            # run
            result = run_diamond_attempt(state)
            state["stage_outputs"] = result["stage_outputs"]
            self.assertEqual(state["stage_outputs"]["function_annotation"]["status"], "diamond_ran")
            # collect
            result = collect_diamond_artifacts(state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_completed")
            self.assertTrue(Path(func_out["diamond_hits_ref"]).exists())
            self.assertIn("diamond_hits_ref", func_out)

            state["stage_outputs"] = result["stage_outputs"]
            committed = commit_function_annotation(state)
            committed_out = committed["stage_outputs"]["function_annotation"]
            self.assertEqual(committed_out["diamond_hits_ref"], func_out["diamond_hits_ref"])
            self.assertIsNone(committed_out["gene_function_tsv_ref"])
            self.assertIsNone(committed_out["go_annotation_table_ref"])
            self.assertIsNone(committed_out["kegg_annotation_table_ref"])
            self.assertIsNone(committed_out["pfam_interpro_table_ref"])
            bundle = json.loads(Path(committed_out["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(bundle["diamond_hits_ref"], func_out["diamond_hits_ref"])
            self.assertIsNone(bundle["gene_function_tsv_ref"])
            self.assertIsNone(bundle["go_annotation_table_ref"])
            self.assertIsNone(bundle["kegg_annotation_table_ref"])
            self.assertIsNone(bundle["pfam_interpro_table_ref"])
            self.assertEqual(len(bundle["method_results"]), 1)
            method_result = bundle["method_results"][0]
            self.assertEqual(method_result["method_name"], "function_annotation.candidate.diamond")
            self.assertEqual(method_result["result_refs"]["diamond_hits"], func_out["diamond_hits_ref"])
            self.assertEqual(method_result["database_provenance"][0]["method_name"], "function_annotation.candidate.diamond")


if __name__ == "__main__":
    unittest.main()
