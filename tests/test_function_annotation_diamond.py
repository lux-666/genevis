"""Tests for Phase 7 task 06 Slice 1: DIAMOND execution."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.execution.collect import collect_with_failure_boundary
from app.execution.evidence import load_execution_evidence, load_frozen_attempt
from app.execution.lifecycle import run_method_attempts
from app.schemas.artifacts import ArtifactMeta
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import initial_state
from app.stages.base import stage_artifacts_dir, stage_work_dir
from app.stages.function_annotation.nodes import (
    DIAMOND_LIFECYCLE,
    collect_diamond_artifacts,
    commit_function_annotation,
    freeze_diamond_attempt,
)
from app.storage.artifacts import (
    publish_artifact_record,
    read_artifact_records,
    resolve_artifact_ref,
)
from tests.data_v2 import v2_run_dir


def run_diamond_attempt(state: dict) -> dict:
    return run_method_attempts(state, DIAMOND_LIFECYCLE)


def _execution_cursor(output: dict) -> dict:
    [cursor] = output["execution_attempts"]
    return cursor


def _fake_diamond_bin(tmpdir: str, *, fail: bool = False) -> str:
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    diamond = bin_dir / "diamond"
    if fail:
        diamond.write_text(
            "#!/bin/sh\necho fake diamond failed >&2\nexit 3\n", encoding="utf-8"
        )
    else:
        diamond.write_text(
            "#!/bin/sh\n"
            'out=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--out" ]; then\n'
            '    out="$2"; shift 2; break\n'
            "  fi\n"
            "  shift\n"
            "done\n"
            'if [ -n "$out" ]; then\n'
            '  printf "prot1\\tsp|P12345\\t100.0\\t300\\t0\\t0\\t1\\t300'
            '\\t1\\t300\\t0.0\\t600\\n" > "$out"\n'
            "fi\n",
            encoding="utf-8",
        )
    diamond.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _provenance_state(tmpdir: str, **func_updates):
    """State after validate_database_provenance with a diamond database."""
    db_fasta = Path(tmpdir) / "swissprot.fasta"
    db_fasta.write_text(">sp|P12345\nMKTLLLL\n", encoding="utf-8")
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(v2_run_dir(tmpdir, "s1")),
    )
    registry_path = stage_work_dir(state, "inventory", "asset_registry.json")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": state["session_id"],
                "assets": [],
            }
        ),
        encoding="utf-8",
    )
    published_registry = publish_artifact_record(
        state["data_dir"],
        state["session_id"],
        ArtifactMeta.from_file(
            registry_path,
            stage="inventory",
            method_name="inventory.register_assets",
            artifact_type="asset_registry",
        ),
    )
    state["asset_registry_ref"] = published_registry.ref.model_dump(mode="json")
    proteins = stage_work_dir(state, "gene_annotation", "proteins.fa")
    proteins.parent.mkdir(parents=True, exist_ok=True)
    proteins.write_text(">prot1\nMKTLLLL\n", encoding="utf-8")
    published_proteins = publish_artifact_record(
        state["data_dir"],
        state["session_id"],
        ArtifactMeta.from_file(
            proteins,
            stage="gene_annotation",
            method_name="gene_annotation.contract",
            artifact_type="gene_annotation.final_proteins",
        ),
        destination=stage_artifacts_dir(
            state,
            "gene_annotation",
            "proteins.fa",
        ),
    )
    proteins_ref = str(published_proteins.path)
    state["resource_refs"]["predicted_proteins"] = ResourceRef(
        resource_type="predicted_proteins",
        paths=[proteins_ref],
        source_stage="gene_annotation",
        metadata={
            "primary_output_ref": published_proteins.ref.model_dump(mode="json")
        },
    ).to_dict()
    state["current_stage"] = "function_annotation"
    state["stage_outputs"] = {
        "function_annotation": {
            "status": "provenance_validated",
            "execution_mode": "local",
            "proteins_fasta_ref": proteins_ref,
            "expected_outputs": {
                "gene_function_tsv": str(Path(tmpdir) / "gene_function.tsv")
            },
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
    def test_freeze_skips_execution_when_local_execution_is_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _provenance_state(tmpdir, execution_mode="planned")

            result = freeze_diamond_attempt(state)

            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_completed")
            self.assertNotIn("diamond_attempt_ref", func_out)

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
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["current_stage"] = "function_annotation"
            state["stage_outputs"] = {
                "function_annotation": {
                    "status": "provenance_validated",
                    "proteins_fasta_ref": str(proteins),
                    "expected_outputs": {
                        "gene_function_tsv": str(Path(tmpdir) / "gene_function.tsv")
                    },
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
            attempt = load_frozen_attempt(
                state,
                _execution_cursor(func_out)["frozen_attempt_ref"],
                expected_stage="function_annotation",
                expected_method_name="function_annotation.candidate.diamond",
            )
            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn("diamond makedb --in ", script)
            self.assertIn("diamond blastp --db ", script)
            self.assertIn("--outfmt 6 --threads ", script)
            self.assertIn("--out ", script)
            self.assertIn("--query ", script)
            self.assertIn("test -f ", script)
            self.assertEqual(attempt.stage, "function_annotation")
            self.assertEqual(
                attempt.method_name, "function_annotation.candidate.diamond"
            )
            self.assertIn("swissprot.fasta", script)

    def test_freeze_uses_prebuilt_diamond_index_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            index = Path(tmpdir) / "swissprot.dmnd"
            index.write_bytes(b"test-index")
            state = _provenance_state(tmpdir)
            state["stage_outputs"]["function_annotation"]["database_provenance"][0][
                "database_source_ref"
            ] = str(index)

            result = freeze_diamond_attempt(state)

            func_out = result["stage_outputs"]["function_annotation"]
            attempt = load_frozen_attempt(
                state,
                _execution_cursor(func_out)["frozen_attempt_ref"],
                expected_stage="function_annotation",
                expected_method_name="function_annotation.candidate.diamond",
            )
            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertNotIn("diamond makedb", script)
            self.assertIn(f"diamond blastp --db {index.resolve()}", script)
            self.assertEqual(attempt.metadata["database_source_kind"], "diamond_index")

    def test_freeze_reports_missing_database_before_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _provenance_state(tmpdir)
            missing = Path(tmpdir) / "missing.dmnd"
            state["stage_outputs"]["function_annotation"]["database_provenance"][0][
                "database_source_ref"
            ] = str(missing)

            result = freeze_diamond_attempt(state)

            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "failed")
            self.assertIn("database source not found", func_out["reason"])

    def test_freeze_creates_working_directory_and_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            result = freeze_diamond_attempt(state)
            func_out = result["stage_outputs"]["function_annotation"]
            attempt = load_frozen_attempt(
                state,
                _execution_cursor(func_out)["frozen_attempt_ref"],
                expected_stage="function_annotation",
                expected_method_name="function_annotation.candidate.diamond",
            )
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
            execution = load_execution_evidence(
                frozen_state,
                _execution_cursor(func_out)["execution_ref"],
                expected_stage="function_annotation",
                expected_attempt_id="s1:function_annotation:diamond",
                expected_method_name="function_annotation.candidate.diamond",
                expected_frozen_attempt_ref=_execution_cursor(func_out)[
                    "frozen_attempt_ref"
                ],
            )
            self.assertTrue(execution.success)
            self.assertEqual(execution.stdout_ref.artifact_type, "command.stdout")
            self.assertEqual(execution.stderr_ref.artifact_type, "command.stderr")
            artifact_types = {
                record["artifact_type"]
                for record in read_artifact_records(state["data_dir"], "s1")
            }
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            execution_record = json.loads(
                (
                    Path(state["data_dir"])
                    / _execution_cursor(func_out)["execution_ref"]["path_ref"]
                ).read_text(encoding="utf-8")
            )
            self.assertIn("stdout_ref", execution_record["result"])
            self.assertIn("stderr_ref", execution_record["result"])
            resolve_artifact_ref(
                state["data_dir"],
                "s1",
                execution_record["result"]["stdout_ref"],
            )
            resolve_artifact_ref(
                state["data_dir"],
                "s1",
                execution_record["result"]["stderr_ref"],
            )


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
            attempt = load_frozen_attempt(
                ran_state,
                _execution_cursor(run_result["stage_outputs"]["function_annotation"])[
                    "frozen_attempt_ref"
                ],
                expected_stage="function_annotation",
                expected_method_name="function_annotation.candidate.diamond",
            )
            source = Path(attempt.metadata["output_tsv"])
            result = collect_diamond_artifacts(ran_state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_completed")
            self.assertIn("diamond_hits_ref", func_out)
            self.assertFalse(source.exists())
            self.assertTrue(Path(func_out["diamond_hits_ref"]).exists())
            self.assertEqual(
                resolve_artifact_ref(
                    state["data_dir"],
                    "s1",
                    func_out["diamond_hits_artifact_ref"],
                ),
                Path(func_out["diamond_hits_ref"]),
            )
            # Verify the TSV content is outfmt 6
            tsv_content = Path(func_out["diamond_hits_ref"]).read_text(encoding="utf-8")
            self.assertIn("sp|P12345", tsv_content)
            self.assertIn("100.0", tsv_content)

    def test_collect_replay_recovers_already_published_tsv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _provenance_state(tmpdir, env={"PATH": _fake_diamond_bin(tmpdir)})
            frozen = freeze_diamond_attempt(state)
            frozen_state = {**state, "stage_outputs": frozen["stage_outputs"]}
            ran = run_diamond_attempt(frozen_state)
            ran_state = {**state, "stage_outputs": ran["stage_outputs"]}

            first = collect_diamond_artifacts(ran_state)
            replay = collect_diamond_artifacts(ran_state)

            first_output = first["stage_outputs"]["function_annotation"]
            replay_output = replay["stage_outputs"]["function_annotation"]
            self.assertEqual(replay_output["status"], "diamond_completed")
            self.assertEqual(
                replay_output["diamond_hits_artifact_ref"],
                first_output["diamond_hits_artifact_ref"],
            )
            self.assertEqual(
                replay_output["diamond_hits_ref"],
                first_output["diamond_hits_ref"],
            )

    def test_collect_missing_tsv_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            freeze_result = freeze_diamond_attempt(state)
            frozen_state = {**state, "stage_outputs": freeze_result["stage_outputs"]}
            run_result = run_diamond_attempt(frozen_state)
            ran_state = {**state, "stage_outputs": run_result["stage_outputs"]}
            func_out = run_result["stage_outputs"]["function_annotation"]
            attempt = load_frozen_attempt(
                ran_state,
                _execution_cursor(func_out)["frozen_attempt_ref"],
                expected_stage="function_annotation",
                expected_method_name="function_annotation.candidate.diamond",
            )
            # Remove the output before collect
            for p in attempt.expected_outputs:
                if p.exists():
                    p.unlink()
            result = collect_with_failure_boundary(
                ran_state,
                lifecycle=DIAMOND_LIFECYCLE,
                collect=collect_diamond_artifacts,
            )
            collected = result["stage_outputs"]["function_annotation"]
            self.assertEqual(collected["status"], "failed")
            self.assertEqual(collected["failure_kind"], "collect")
            self.assertIn("diamond collect failed", collected["reason"])


class DiamondFullPathTests(unittest.TestCase):
    def test_checkpoint_uses_compact_verified_execution_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            freeze_result = freeze_diamond_attempt(state)
            frozen = freeze_result["stage_outputs"]["function_annotation"]
            attempt_ref = _execution_cursor(frozen)["frozen_attempt_ref"]
            ref_fields = {
                "schema_version",
                "artifact_id",
                "path_ref",
                "sha256",
                "size_bytes",
                "artifact_type",
            }
            self.assertEqual(set(attempt_ref), ref_fields)
            self.assertNotIn("s1_function_annotation", attempt_ref["path_ref"])

            state["stage_outputs"] = freeze_result["stage_outputs"]
            run_result = run_diamond_attempt(state)
            ran = run_result["stage_outputs"]["function_annotation"]
            self.assertEqual(
                set(_execution_cursor(ran)["execution_ref"]),
                ref_fields,
            )
            self.assertNotIn("diamond_frozen_attempt", ran)
            self.assertNotIn("diamond_execution_result", ran)
            checkpoint = json.dumps(ran, sort_keys=True)
            for field in (
                "script_path",
                "working_dir",
                "stdout_path",
                "stderr_path",
                "output_paths",
            ):
                self.assertNotIn(field, checkpoint)

    def test_run_rejects_tampered_attempt_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _provenance_state(tmpdir, env={"PATH": _fake_diamond_bin(tmpdir)})
            freeze_result = freeze_diamond_attempt(state)
            frozen = freeze_result["stage_outputs"]["function_annotation"]
            cursor = _execution_cursor(frozen)
            cursor["frozen_attempt_ref"] = {
                **cursor["frozen_attempt_ref"],
                "sha256": "0" * 64,
            }
            state["stage_outputs"] = freeze_result["stage_outputs"]

            result = run_diamond_attempt(state)
            output = result["stage_outputs"]["function_annotation"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("frozen attempt is invalid", output["reason"])

    def test_end_to_end_diamond_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = _fake_diamond_bin(tmpdir)
            state = _provenance_state(tmpdir, env={"PATH": fake_path})
            # freeze
            result = freeze_diamond_attempt(state)
            state["stage_outputs"] = result["stage_outputs"]
            self.assertEqual(
                state["stage_outputs"]["function_annotation"]["status"],
                "diamond_frozen",
            )
            # run
            result = run_diamond_attempt(state)
            state["stage_outputs"] = result["stage_outputs"]
            self.assertEqual(
                state["stage_outputs"]["function_annotation"]["status"], "diamond_ran"
            )
            # collect
            result = collect_diamond_artifacts(state)
            func_out = result["stage_outputs"]["function_annotation"]
            self.assertEqual(func_out["status"], "diamond_completed")
            self.assertTrue(Path(func_out["diamond_hits_ref"]).exists())
            self.assertIn("diamond_hits_ref", func_out)

            state["stage_outputs"] = result["stage_outputs"]
            committed = commit_function_annotation(state)
            committed_out = committed["stage_outputs"]["function_annotation"]
            self.assertEqual(
                committed_out["diamond_hits_ref"], func_out["diamond_hits_ref"]
            )
            self.assertIsNone(committed_out["gene_function_tsv_ref"])
            self.assertIsNone(committed_out["go_annotation_table_ref"])
            self.assertIsNone(committed_out["kegg_annotation_table_ref"])
            self.assertIsNone(committed_out["pfam_interpro_table_ref"])
            bundle = json.loads(
                Path(committed_out["bundle_artifact_ref"]).read_text(encoding="utf-8")
            )
            self.assertEqual(bundle["diamond_hits_ref"], func_out["diamond_hits_ref"])
            self.assertIsNone(bundle["gene_function_tsv_ref"])
            self.assertIsNone(bundle["go_annotation_table_ref"])
            self.assertIsNone(bundle["kegg_annotation_table_ref"])
            self.assertIsNone(bundle["pfam_interpro_table_ref"])
            self.assertEqual(len(bundle["method_results"]), 1)
            method_result = bundle["method_results"][0]
            self.assertEqual(
                method_result["method_name"], "function_annotation.candidate.diamond"
            )
            self.assertEqual(
                method_result["result_refs"]["diamond_hits"],
                func_out["diamond_hits_ref"],
            )
            self.assertEqual(
                method_result["database_provenance"][0]["method_name"],
                "function_annotation.candidate.diamond",
            )


if __name__ == "__main__":
    unittest.main()
