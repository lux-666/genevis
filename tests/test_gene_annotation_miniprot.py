"""Tests for Phase 7 task 04: miniprot and augustus execution slices."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.storage.artifacts import register_artifact_records
from app.schemas.execution import ExecutionResult
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import initial_state
from app.stages.gene_annotation.nodes import (
    collect_augustus_artifacts,
    collect_miniprot_artifacts,
    freeze_augustus_attempt,
    freeze_miniprot_attempt,
    run_augustus_attempt,
    run_miniprot_attempt,
)
from app.stages.gene_annotation.persistence import (
    load_gene_attempt,
    load_gene_execution,
    persist_gene_attempt,
    persist_gene_execution,
)
from app.storage.publish import publish_execution_logs
from tests.data_v2 import v2_run_dir


def _v2_state(tmpdir: str) -> dict:
    return initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(v2_run_dir(tmpdir, "s1")),
    )


def _gene_work_path(tmpdir: str, *parts: str) -> Path:
    path = v2_run_dir(tmpdir, "s1") / "work" / "gene_annotation" / Path(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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


def _persist_attempt(state: dict, attempt: FrozenAttemptSpec) -> dict:
    attempt.script_path.parent.mkdir(parents=True, exist_ok=True)
    if not attempt.script_path.exists():
        attempt.script_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return persist_gene_attempt(state, attempt).model_dump(mode="json")


def _persist_successful_execution(
    state: dict,
    attempt: FrozenAttemptSpec,
    output_paths: list[Path],
) -> dict:
    stdout_path = (
        Path(state["data_dir"]) / "work" / "gene_annotation" / "test.stdout.log"
    )
    stderr_path = (
        Path(state["data_dir"]) / "work" / "gene_annotation" / "test.stderr.log"
    )
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    result = ExecutionResult(
        attempt_id=attempt.attempt_id,
        success=True,
        return_code=0,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        output_paths=output_paths,
        status="ok",
    )
    result = publish_execution_logs(state["data_dir"], attempt, result)
    register_artifact_records(
        state["data_dir"],
        attempt.session_id,
        [
            ArtifactMeta.from_file(
                path,
                stage=attempt.stage,
                method_name=attempt.method_name,
                artifact_type=artifact_type,
                metadata={"attempt_id": attempt.attempt_id},
            )
            for path, artifact_type in (
                (result.stdout_path, "command.stdout"),
                (result.stderr_path, "command.stderr"),
            )
        ],
    )
    return persist_gene_execution(
        state,
        method_name=attempt.method_name,
        result=result,
    ).model_dump(mode="json")


def _fake_miniprot_bin(tmpdir: str, *, fail: bool = False) -> Path:
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    mp = bin_dir / "miniprot"
    if fail:
        mp.write_text(
            "#!/bin/sh\necho fake miniprot failed >&2\nexit 3\n", encoding="utf-8"
        )
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


def _fake_augustus_bin(tmpdir: str, *, fail: bool = False) -> Path:
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    aug = bin_dir / "augustus"
    if fail:
        aug.write_text(
            "#!/bin/sh\necho fake augustus failed >&2\nexit 3\n", encoding="utf-8"
        )
    else:
        aug.write_text(
            "#!/bin/sh\n"
            "printf '##gff-version 3\\n"
            "ctg1\\tAUGUSTUS\\tgene\\t1\\t500\\t.\\t+\\t.\\tID=g1\\n"
            "ctg1\\tAUGUSTUS\\tmRNA\\t1\\t500\\t.\\t+\\t.\\tID=m1;Parent=g1\\n'\n",
            encoding="utf-8",
        )
    aug.chmod(0o755)
    return bin_dir


def _fake_gffread_bin(tmpdir: str) -> Path:
    """Fake gffread that materializes proteins.fa (-y) and cds.fa (-x)."""
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    gffread = bin_dir / "gffread"
    gffread.write_text(
        "#!/bin/sh\n"
        'prot=""; cds=""\n'
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -y) prot="$2"; shift 2;;\n'
        '    -x) cds="$2"; shift 2;;\n'
        "    -g) shift 2;;\n"
        "    *) shift;;\n"
        "  esac\n"
        "done\n"
        '[ -n "$prot" ] && printf \'>g1\\nMKT\\n\' > "$prot"\n'
        '[ -n "$cds" ] && printf \'>g1\\nATGAAAACC\\n\' > "$cds"\n',
        encoding="utf-8",
    )
    gffread.chmod(0o755)
    return bin_dir


def _pending_execution_state(tmpdir: str, **gene_updates):
    """State as it stands after preparing executable gene methods."""
    softmasked = Path(tmpdir) / "softmasked.fa"
    softmasked.write_text(">ctg1\nACGT\n", encoding="utf-8")
    state = _v2_state(tmpdir)
    state["current_stage"] = "gene_annotation"
    state["stage_outputs"] = {
        "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
        "gene_annotation": {
            "status": "pending_execution",
            "genome_input_ref": str(softmasked),
            "genome_input_source": "softmasked_genome",
            "confirmed_methods": ["gene_annotation.candidate.miniprot"],
            "candidate_refs": {
                "gene_annotation.candidate.miniprot": str(
                    _gene_work_path(tmpdir, "miniprot", "miniprot.gff3")
                )
            },
            "candidate_gene_counts": {"gene_annotation.candidate.miniprot": 0},
            "executed_methods": [],
            "env": {"PATH": os.environ.get("PATH", "")},
            **gene_updates,
        },
    }
    return state


class MiniprotFreezeTests(unittest.TestCase):
    def _freeze_state(self, tmpdir: str, **gene_updates):
        proteins = Path(tmpdir) / "proteins.fa"
        proteins.write_text(">p1\nMKT\n", encoding="utf-8")
        gene_updates.setdefault("protein_evidence_refs", [str(proteins)])
        return _pending_execution_state(tmpdir, **gene_updates)

    def test_freeze_requires_pending_execution_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, status="candidates_ready")
            result = freeze_miniprot_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_skips_when_miniprot_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(
                tmpdir, confirmed_methods=["gene_annotation.candidate.augustus"]
            )
            result = freeze_miniprot_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_skips_when_miniprot_already_executed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(
                tmpdir, executed_methods=["gene_annotation.candidate.miniprot"]
            )
            result = freeze_miniprot_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_produces_frozen_attempt_spec_with_correct_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir)
            result = freeze_miniprot_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "miniprot_frozen")
            attempt = load_gene_attempt(state, gene["miniprot_attempt_ref"])
            self.assertEqual(attempt.method_name, "gene_annotation.candidate.miniprot")
            self.assertIn("miniprot", attempt.script_path.name)

            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn("miniprot --gff", script)
            self.assertIn("softmasked.fa", script)
            self.assertIn("proteins.fa", script)

    def test_freeze_fails_without_genome_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, genome_input_ref=None)
            result = freeze_miniprot_attempt(state)
            self.assertEqual(
                result["stage_outputs"]["gene_annotation"]["status"], "failed"
            )

    def test_freeze_fails_without_protein_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, protein_evidence_refs=[])
            result = freeze_miniprot_attempt(state)
            self.assertEqual(
                result["stage_outputs"]["gene_annotation"]["status"], "failed"
            )


class MiniprotRunTests(unittest.TestCase):
    def _run_state(
        self, tmpdir: str, bin_dir: Path, *, status: str = "miniprot_frozen"
    ):
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
        state = _v2_state(tmpdir)
        state["current_stage"] = "gene_annotation"
        state["stage_outputs"] = {
            "assembly_review": {
                "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
            },
            "gene_annotation": {
                "status": status,
                "miniprot_attempt_ref": _persist_attempt(state, attempt),
            },
        }
        return state

    def test_run_requires_miniprot_frozen_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_miniprot_bin(tmpdir)
            state = self._run_state(tmpdir, bin_dir, status="pending_execution")
            result = run_miniprot_attempt(state)
            self.assertEqual(result, {})

    def test_run_success_sets_miniprot_ran(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_miniprot_bin(tmpdir)
            state = self._run_state(tmpdir, bin_dir)
            result = run_miniprot_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "miniprot_ran")
            execution = load_gene_execution(
                state,
                gene["miniprot_execution_ref"],
                expected_attempt_id="s1:gene_annotation:miniprot",
            )
            self.assertTrue(execution.success)

    def test_run_failure_records_attempt_failed_and_sets_failed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_miniprot_bin(tmpdir, fail=True)
            state = self._run_state(tmpdir, bin_dir)
            result = run_miniprot_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "failed")
            self.assertIn("miniprot failed", gene["reason"])

            ledger_path = Path(state["data_dir"]) / "ledger" / "events.jsonl"
            self.assertTrue(
                ledger_path.exists(), "ledger should record attempt_failed event"
            )
            events = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            event_types = [e["event_type"] for e in events]
            self.assertIn("attempt_failed", event_types)


class MiniprotCollectTests(unittest.TestCase):
    def _collect_state(self, tmpdir: str, *, gff3_exists: bool = True):
        output_gff3 = _gene_work_path(tmpdir, "miniprot", "miniprot.gff3")
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
        state = _v2_state(tmpdir)
        state["current_stage"] = "gene_annotation"
        genome = Path(tmpdir) / "genome.fa"
        genome.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        gffread_bin = _fake_gffread_bin(tmpdir)
        state["stage_outputs"] = {
            "assembly_review": {
                "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
            },
            "gene_annotation": {
                "status": "miniprot_ran",
                "genome_input_ref": str(genome),
                "env": {
                    "PATH": f"{gffread_bin}{os.pathsep}{os.environ.get('PATH', '')}"
                },
                "miniprot_attempt_ref": _persist_attempt(state, attempt),
                "miniprot_execution_ref": _persist_successful_execution(
                    state, attempt, [output_gff3]
                ),
                "candidate_refs": {
                    "gene_annotation.candidate.miniprot": str(output_gff3)
                },
                "candidate_gene_counts": {"gene_annotation.candidate.miniprot": 0},
                "confirmed_methods": ["gene_annotation.candidate.miniprot"],
                "executed_methods": [],
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
            source = Path(
                state["stage_outputs"]["gene_annotation"]["candidate_refs"][
                    "gene_annotation.candidate.miniprot"
                ]
            )
            result = collect_miniprot_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "candidates_ready")
            self.assertEqual(
                gene["candidate_gff_ref"],
                str(
                    Path(state["data_dir"])
                    / "artifacts"
                    / "gene_annotation"
                    / "outputs"
                    / "miniprot"
                    / "miniprot.gff3"
                ),
            )
            self.assertFalse(
                source.exists(), "publication should move the mutable work file"
            )
            self.assertTrue(Path(gene["candidate_gff_ref"]).is_file())
            self.assertIn("gene_annotation.candidate.miniprot", gene["candidate_refs"])
            self.assertGreater(gene["miniprot_gene_count"], 0)
            self.assertEqual(
                gene["miniprot_gene_count"],
                2,
                "gene count should count only GFF3 gene features",
            )
            self.assertEqual(
                gene["candidate_gene_counts"]["gene_annotation.candidate.miniprot"],
                gene["miniprot_gene_count"],
            )

            artifacts_path = Path(state["data_dir"]) / "artifacts" / "index.jsonl"
            self.assertTrue(artifacts_path.exists())
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifacts_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("gene_annotation.candidate_gff", artifact_types)

    def test_collect_replay_recovers_already_published_gff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir)

            first = collect_miniprot_artifacts(state)
            replay = collect_miniprot_artifacts(state)

            first_gene = first["stage_outputs"]["gene_annotation"]
            replay_gene = replay["stage_outputs"]["gene_annotation"]
            self.assertEqual(replay_gene["status"], "candidates_ready")
            self.assertEqual(
                replay_gene["candidate_artifact_refs"],
                first_gene["candidate_artifact_refs"],
            )
            self.assertEqual(
                replay_gene["candidate_gff_ref"],
                first_gene["candidate_gff_ref"],
            )

    def test_collect_materialization_failure_sets_materialization_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir)
            # Point at a gffread that exits non-zero and writes no proteins.
            fail_bin = Path(tmpdir) / "failbin"
            fail_bin.mkdir()
            bad = fail_bin / "gffread"
            bad.write_text("#!/bin/sh\necho boom >&2\nexit 1\n", encoding="utf-8")
            bad.chmod(0o755)
            state["stage_outputs"]["gene_annotation"]["env"] = {
                "PATH": f"{fail_bin}{os.pathsep}{os.environ.get('PATH', '')}"
            }
            result = collect_miniprot_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "materialization_failed")
            self.assertNotIn("proteins_fasta_ref", gene)

    def test_collect_fails_when_gff3_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir, gff3_exists=False)
            result = collect_miniprot_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "failed")
            self.assertIn("missing", gene["reason"])

    def test_collect_loops_when_more_executable_methods_pending(self):
        """When miniprot is done but augustus is still pending, status stays pending_execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir)
            state["stage_outputs"]["gene_annotation"]["confirmed_methods"] = [
                "gene_annotation.candidate.miniprot",
                "gene_annotation.candidate.augustus",
            ]
            result = collect_miniprot_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "pending_execution")
            self.assertIn(
                "gene_annotation.candidate.miniprot", gene["executed_methods"]
            )
            self.assertNotIn(
                "gene_annotation.candidate.augustus", gene["executed_methods"]
            )


class MiniprotFullSliceTests(unittest.TestCase):
    """End-to-end miniprot slice: freeze → run → collect in sequence."""

    def test_full_slice_path_produces_candidate_gff_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            softmasked = Path(tmpdir) / "softmasked.fa"
            proteins = Path(tmpdir) / "proteins.fa"
            softmasked.write_text(">ctg1\nACGT\n", encoding="utf-8")
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")

            bin_dir = _fake_miniprot_bin(tmpdir)
            _fake_gffread_bin(tmpdir)

            freeze_state = _pending_execution_state(
                tmpdir,
                protein_evidence_refs=[str(proteins)],
                env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            freeze_result = freeze_miniprot_attempt(freeze_state)
            self.assertEqual(
                freeze_result["stage_outputs"]["gene_annotation"]["status"],
                "miniprot_frozen",
            )

            run_state = _v2_state(tmpdir)
            run_state["current_stage"] = "gene_annotation"
            run_state["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": freeze_result["stage_outputs"]["gene_annotation"],
            }
            run_result = run_miniprot_attempt(run_state)
            self.assertEqual(
                run_result["stage_outputs"]["gene_annotation"]["status"], "miniprot_ran"
            )

            collect_state = _v2_state(tmpdir)
            collect_state["current_stage"] = "gene_annotation"
            collect_state["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": {
                    **run_result["stage_outputs"]["gene_annotation"],
                    "candidate_refs": {
                        "gene_annotation.candidate.miniprot": str(
                            _gene_work_path(tmpdir, "miniprot", "miniprot.gff3")
                        )
                    },
                    "candidate_gene_counts": {"gene_annotation.candidate.miniprot": 0},
                    "confirmed_methods": ["gene_annotation.candidate.miniprot"],
                    "executed_methods": [],
                },
            }
            collect_result = collect_miniprot_artifacts(collect_state)

            gene = collect_result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "candidates_ready")
            self.assertIn("candidate_gff_ref", gene)
            self.assertTrue(Path(gene["candidate_gff_ref"]).exists())
            self.assertIn(
                "gene_annotation.candidate.miniprot", gene["executed_methods"]
            )

    def test_checkpoint_keeps_attempt_and_execution_as_compact_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins = Path(tmpdir) / "proteins.fa"
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")
            bin_dir = _fake_miniprot_bin(tmpdir)
            freeze_state = _pending_execution_state(
                tmpdir,
                protein_evidence_refs=[str(proteins)],
                env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
            )

            frozen = freeze_miniprot_attempt(freeze_state)["stage_outputs"][
                "gene_annotation"
            ]
            attempt_ref = frozen["miniprot_attempt_ref"]
            self.assertEqual(
                set(attempt_ref),
                {
                    "schema_version",
                    "artifact_id",
                    "path_ref",
                    "sha256",
                    "size_bytes",
                    "artifact_type",
                },
            )
            self.assertNotIn("env", attempt_ref)
            self.assertNotIn("s1_gene_annotation", attempt_ref["path_ref"])
            self.assertEqual(
                load_gene_attempt(freeze_state, attempt_ref).attempt_id,
                "s1:gene_annotation:miniprot",
            )

            run_state = _v2_state(tmpdir)
            run_state["current_stage"] = "gene_annotation"
            run_state["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": frozen,
            }
            ran = run_miniprot_attempt(run_state)["stage_outputs"]["gene_annotation"]
            execution_ref = ran["miniprot_execution_ref"]
            self.assertEqual(set(execution_ref), set(attempt_ref))
            self.assertNotIn("env", execution_ref)
            self.assertTrue(load_gene_execution(run_state, execution_ref).success)

            self.assertNotIn("miniprot_frozen_attempt", ran)
            self.assertNotIn("miniprot_execution_result", ran)
            checkpoint_json = json.dumps(ran, sort_keys=True)
            for forbidden in (
                "script_path",
                "working_dir",
                "expected_outputs",
                "stdout_path",
                "stderr_path",
                "output_paths",
            ):
                self.assertNotIn(forbidden, checkpoint_json)


# --- AUGUSTUS tests -----------------------------------------------------------


def _augustus_pending_state(tmpdir: str, **gene_updates):
    state = _pending_execution_state(tmpdir, **gene_updates)
    gene = state["stage_outputs"]["gene_annotation"]
    if "confirmed_methods" not in gene_updates:
        gene["confirmed_methods"] = ["gene_annotation.candidate.augustus"]
    gene.setdefault("candidate_refs", {})["gene_annotation.candidate.augustus"] = str(
        _gene_work_path(tmpdir, "augustus", "augustus.gff3")
    )
    gene.setdefault("candidate_gene_counts", {})[
        "gene_annotation.candidate.augustus"
    ] = 0
    # AUGUSTUS now requires an explicit species model; supply one by default
    # unless the caller passed its own lineage_or_species_hints.
    if "lineage_or_species_hints" not in gene_updates:
        gene["lineage_or_species_hints"] = {"augustus_species_model": "test_species"}
    return state


class AugustusFreezeTests(unittest.TestCase):
    def _freeze_state(self, tmpdir: str, **gene_updates):
        return _augustus_pending_state(tmpdir, **gene_updates)

    def test_freeze_requires_pending_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, status="candidates_ready")
            result = freeze_augustus_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_skips_when_augustus_not_confirmed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(
                tmpdir, confirmed_methods=["gene_annotation.candidate.miniprot"]
            )
            result = freeze_augustus_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_skips_when_already_executed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(
                tmpdir, executed_methods=["gene_annotation.candidate.augustus"]
            )
            result = freeze_augustus_attempt(state)
            self.assertEqual(result, {})

    def test_freeze_produces_frozen_attempt_spec_with_correct_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir)
            result = freeze_augustus_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "augustus_frozen")
            attempt = load_gene_attempt(state, gene["augustus_attempt_ref"])
            self.assertEqual(attempt.method_name, "gene_annotation.candidate.augustus")

            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn("augustus --species=", script)
            self.assertIn("softmasked.fa", script)
            self.assertIn("augustus.gff3", script)

    def test_freeze_uses_species_hint_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(
                tmpdir,
                lineage_or_species_hints={"augustus_species_model": "human"},
            )
            result = freeze_augustus_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            attempt = load_gene_attempt(state, gene["augustus_attempt_ref"])
            script = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn("--species=human", script)

    def test_freeze_fails_without_species_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._freeze_state(tmpdir, lineage_or_species_hints={})
            result = freeze_augustus_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "failed")
            self.assertIn("augustus_species_model", gene["reason"])


class AugustusRunTests(unittest.TestCase):
    def _run_state(
        self, tmpdir: str, bin_dir: Path, *, status: str = "augustus_frozen"
    ):
        output_gff3 = Path(tmpdir) / "augustus.gff3"
        script = Path(tmpdir) / "run_augustus.sh"
        script.write_text(
            "#!/bin/bash\nset -e\naugustus --species=fly genome.fa > augustus.gff3\ntest -f augustus.gff3\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        attempt = FrozenAttemptSpec(
            attempt_id="s1:gene_annotation:augustus",
            session_id="s1",
            stage="gene_annotation",
            method_name="gene_annotation.candidate.augustus",
            script_path=script,
            working_dir=Path(tmpdir),
            expected_outputs=[output_gff3],
            env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
            metadata={
                "output_gff3": str(output_gff3),
                "genome_input_ref": str(Path(tmpdir) / "genome.fa"),
                "species": "fly",
            },
        )
        state = _v2_state(tmpdir)
        state["current_stage"] = "gene_annotation"
        state["stage_outputs"] = {
            "assembly_review": {
                "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
            },
            "gene_annotation": {
                "status": status,
                "augustus_attempt_ref": _persist_attempt(state, attempt),
            },
        }
        return state

    def test_run_success_sets_augustus_ran(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_augustus_bin(tmpdir)
            state = self._run_state(tmpdir, bin_dir)
            result = run_augustus_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "augustus_ran")

    def test_run_failure_sets_failed_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bin_dir = _fake_augustus_bin(tmpdir, fail=True)
            state = self._run_state(tmpdir, bin_dir)
            result = run_augustus_attempt(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "failed")


class AugustusCollectTests(unittest.TestCase):
    def _collect_state(self, tmpdir: str, *, gff3_exists: bool = True):
        output_gff3 = _gene_work_path(tmpdir, "augustus", "augustus.gff3")
        if gff3_exists:
            output_gff3.write_text(
                "##gff-version 3\n"
                "ctg1\tAUGUSTUS\tgene\t1\t500\t.\t+\t.\tID=g1\n"
                "ctg1\tAUGUSTUS\tmRNA\t1\t500\t.\t+\t.\tID=m1;Parent=g1\n"
                "ctg1\tAUGUSTUS\tgene\t501\t1000\t.\t-\t.\tID=g2\n",
                encoding="utf-8",
            )
        attempt = FrozenAttemptSpec(
            attempt_id="s1:gene_annotation:augustus",
            session_id="s1",
            stage="gene_annotation",
            method_name="gene_annotation.candidate.augustus",
            script_path=Path(tmpdir) / "run_augustus.sh",
            working_dir=Path(tmpdir),
            expected_outputs=[output_gff3],
            metadata={
                "output_gff3": str(output_gff3),
                "genome_input_ref": str(Path(tmpdir) / "genome.fa"),
                "species": "fly",
            },
        )
        state = _v2_state(tmpdir)
        state["current_stage"] = "gene_annotation"
        genome = Path(tmpdir) / "genome.fa"
        genome.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        gffread_bin = _fake_gffread_bin(tmpdir)
        state["stage_outputs"] = {
            "assembly_review": {
                "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
            },
            "gene_annotation": {
                "status": "augustus_ran",
                "genome_input_ref": str(genome),
                "env": {
                    "PATH": f"{gffread_bin}{os.pathsep}{os.environ.get('PATH', '')}"
                },
                "augustus_attempt_ref": _persist_attempt(state, attempt),
                "augustus_execution_ref": _persist_successful_execution(
                    state, attempt, [output_gff3]
                ),
                "candidate_refs": {
                    "gene_annotation.candidate.augustus": str(output_gff3)
                },
                "candidate_gene_counts": {"gene_annotation.candidate.augustus": 0},
                "confirmed_methods": ["gene_annotation.candidate.augustus"],
                "executed_methods": [],
            },
        }
        return state

    def test_collect_registers_candidate_gff_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._collect_state(tmpdir)
            result = collect_augustus_artifacts(state)

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "candidates_ready")
            self.assertEqual(gene["augustus_gene_count"], 2)
            self.assertIn(
                "gene_annotation.candidate.augustus", gene["executed_methods"]
            )

            artifacts_path = Path(state["data_dir"]) / "artifacts" / "index.jsonl"
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in artifacts_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("gene_annotation.candidate_gff", artifact_types)


class MultiMethodExecutionTests(unittest.TestCase):
    """Tests for the multi-method dispatch: miniprot + augustus in sequence."""

    def test_both_methods_execute_then_candidates_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mp_bin = _fake_miniprot_bin(tmpdir)
            aug_bin = _fake_augustus_bin(tmpdir)
            _fake_gffread_bin(tmpdir)
            combined_path = f"{mp_bin}:{aug_bin}:{os.environ.get('PATH', '')}"

            # Step 1: freeze miniprot
            proteins = Path(tmpdir) / "proteins.fa"
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")
            state = _pending_execution_state(
                tmpdir,
                protein_evidence_refs=[str(proteins)],
                lineage_or_species_hints={"augustus_species_model": "test_species"},
                candidate_refs={
                    "gene_annotation.candidate.miniprot": str(
                        _gene_work_path(tmpdir, "miniprot", "miniprot.gff3")
                    ),
                    "gene_annotation.candidate.augustus": str(
                        _gene_work_path(tmpdir, "augustus", "augustus.gff3")
                    ),
                },
                env={"PATH": combined_path},
            )
            # miniprot freeze
            mp_freeze = freeze_miniprot_attempt(state)
            self.assertEqual(
                mp_freeze["stage_outputs"]["gene_annotation"]["status"],
                "miniprot_frozen",
            )

            # miniprot run
            run_state = _v2_state(tmpdir)
            run_state["current_stage"] = "gene_annotation"
            run_state["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": mp_freeze["stage_outputs"]["gene_annotation"],
            }
            mp_run = run_miniprot_attempt(run_state)
            self.assertEqual(
                mp_run["stage_outputs"]["gene_annotation"]["status"], "miniprot_ran"
            )

            # miniprot collect
            collect_state = _v2_state(tmpdir)
            collect_state["current_stage"] = "gene_annotation"
            collect_state["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": {
                    **mp_run["stage_outputs"]["gene_annotation"],
                    "candidate_refs": {
                        "gene_annotation.candidate.miniprot": str(
                            _gene_work_path(tmpdir, "miniprot", "miniprot.gff3")
                        ),
                        "gene_annotation.candidate.augustus": str(
                            _gene_work_path(tmpdir, "augustus", "augustus.gff3")
                        ),
                    },
                    "candidate_gene_counts": {
                        "gene_annotation.candidate.miniprot": 0,
                        "gene_annotation.candidate.augustus": 0,
                    },
                    "confirmed_methods": [
                        "gene_annotation.candidate.miniprot",
                        "gene_annotation.candidate.augustus",
                    ],
                    "executed_methods": [],
                },
            }
            mp_collect = collect_miniprot_artifacts(collect_state)
            gene = mp_collect["stage_outputs"]["gene_annotation"]
            # Should still be pending_execution because augustus is not yet executed
            self.assertEqual(gene["status"], "pending_execution")
            self.assertIn(
                "gene_annotation.candidate.miniprot", gene["executed_methods"]
            )

            # Step 2: augustus freeze
            aug_state = _v2_state(tmpdir)
            aug_state["current_stage"] = "gene_annotation"
            aug_state["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": mp_collect["stage_outputs"]["gene_annotation"],
            }
            aug_freeze = freeze_augustus_attempt(aug_state)
            self.assertEqual(
                aug_freeze["stage_outputs"]["gene_annotation"]["status"],
                "augustus_frozen",
            )

            # augustus run
            run2 = _v2_state(tmpdir)
            run2["current_stage"] = "gene_annotation"
            run2["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": aug_freeze["stage_outputs"]["gene_annotation"],
            }
            aug_run = run_augustus_attempt(run2)
            self.assertEqual(
                aug_run["stage_outputs"]["gene_annotation"]["status"], "augustus_ran"
            )

            # augustus collect
            coll2 = _v2_state(tmpdir)
            coll2["current_stage"] = "gene_annotation"
            coll2["stage_outputs"] = {
                "assembly_review": {
                    "annotation_intake_plan_ref": _write_intake_plan(tmpdir)
                },
                "gene_annotation": {
                    **aug_run["stage_outputs"]["gene_annotation"],
                    "candidate_refs": {
                        "gene_annotation.candidate.miniprot": gene["candidate_refs"][
                            "gene_annotation.candidate.miniprot"
                        ],
                        "gene_annotation.candidate.augustus": str(
                            _gene_work_path(tmpdir, "augustus", "augustus.gff3")
                        ),
                    },
                    "candidate_gene_counts": {
                        "gene_annotation.candidate.miniprot": 2,
                        "gene_annotation.candidate.augustus": 0,
                    },
                    "confirmed_methods": [
                        "gene_annotation.candidate.miniprot",
                        "gene_annotation.candidate.augustus",
                    ],
                    "executed_methods": ["gene_annotation.candidate.miniprot"],
                },
            }
            aug_collect = collect_augustus_artifacts(coll2)
            gene = aug_collect["stage_outputs"]["gene_annotation"]
            # Both methods done → candidates_ready
            self.assertEqual(gene["status"], "candidates_ready")
            self.assertEqual(
                set(gene["executed_methods"]),
                {
                    "gene_annotation.candidate.miniprot",
                    "gene_annotation.candidate.augustus",
                },
            )
