"""Regression coverage for the forward-only Data Architecture v2 storage layer."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.execution.artifacts import (
    append_artifact_records,
    publish_artifact_record,
    read_artifact_records,
    register_artifact_records,
    resolve_artifact_ref,
)
from app.execution.evidence import (
    ExecutionEvidence,
    execution_log_paths,
    load_execution_evidence,
    load_frozen_attempt,
    persist_execution_result,
    persist_frozen_attempt,
    validate_execution_evidence,
)
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.execution.results import ExecutionResult
from app.schemas.artifacts import ArtifactMeta
from app.storage.paths import (
    DataLayoutError,
    SessionPaths,
    ensure_data_layout,
    ensure_run_layout,
    locate_session_data_dir,
)


class SessionPathsTests(unittest.TestCase):
    def test_run_paths_and_fixed_files_have_one_session_component(self):
        paths = SessionPaths("/tmp/gene-agent-data", "s1")

        self.assertEqual(paths.run_dir, Path("/tmp/gene-agent-data/runs/s1").resolve())
        self.assertEqual(paths.control_dir, paths.run_dir / "control")
        self.assertEqual(paths.artifacts_dir, paths.run_dir / "artifacts")
        self.assertEqual(paths.reports_dir, paths.run_dir / "reports")
        self.assertEqual(paths.work_dir, paths.run_dir / "work")
        self.assertEqual(paths.artifact_index_path, paths.artifacts_dir / "index.jsonl")
        self.assertEqual(paths.ledger_events_path, paths.ledger_dir / "events.jsonl")
        self.assertEqual(paths.checkpoint_db_path, paths.checkpoints_dir / "checkpoints.sqlite")
        self.assertEqual(
            paths.artifact_path("assembly", "outputs", "draft.fa"),
            paths.artifacts_dir / "assembly" / "outputs" / "draft.fa",
        )
        self.assertNotIn(
            "s1",
            paths.artifact_path("assembly", "outputs").relative_to(paths.run_dir).parts,
        )

    def test_resume_requires_the_retained_work_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            work_dir = paths.work_path("read_qc", "attempt")
            work_dir.mkdir(parents=True)
            script = work_dir / "run.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:read_qc:attempt",
                session_id="s1",
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                script_path=script,
                working_dir=work_dir,
                expected_outputs=[],
                env=None,
                env_spec=None,
            )
            state = {"session_id": "s1", "data_dir": str(paths.run_dir)}
            attempt_ref = persist_frozen_attempt(state, attempt)
            script.unlink()

            with self.assertRaisesRegex(DataLayoutError, "work script is missing"):
                load_frozen_attempt(
                    state,
                    attempt_ref,
                    expected_stage="read_qc",
                    expected_method_name=attempt.method_name,
                )

    def test_from_run_dir_is_strict_and_validates_session_id(self):
        paths = SessionPaths("/tmp/gene-agent-data", "s1")

        self.assertEqual(SessionPaths.from_run_dir(paths.run_dir), paths)
        with self.assertRaises(DataLayoutError):
            SessionPaths.from_run_dir("/tmp/not-a-run-root")
        with self.assertRaises(DataLayoutError):
            paths.require_session_id("s2")
        with self.assertRaises(DataLayoutError):
            SessionPaths("/tmp/gene-agent-data", "../escape")

    def test_refs_are_normalized_and_contained_by_run_root(self):
        paths = SessionPaths("/tmp/gene-agent-data", "s1")
        artifact = paths.artifact_path("assembly", "outputs", "draft.fa")

        self.assertEqual(paths.to_ref(artifact), "artifacts/assembly/outputs/draft.fa")
        self.assertEqual(paths.resolve_ref("artifacts/assembly/outputs/draft.fa"), artifact)
        for invalid in ("/tmp/escape", "../escape", "artifacts/../control/run.json", ""):
            with self.subTest(invalid=invalid), self.assertRaises(DataLayoutError):
                paths.resolve_ref(invalid)
        with self.assertRaises(DataLayoutError):
            paths.to_ref("/tmp/outside-run")

    def test_layout_marker_is_v2_and_rejects_flat_runtime_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = ensure_data_layout(tmpdir)
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), {"schema_version": 2})
            self.assertEqual(ensure_data_layout(tmpdir), marker)

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "artifacts").mkdir()
            with self.assertRaises(DataLayoutError):
                ensure_data_layout(tmpdir)

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "runs" / "old-session").mkdir(parents=True)
            with self.assertRaises(DataLayoutError):
                ensure_data_layout(tmpdir)

    def test_locate_only_discovers_marked_v2_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path(tmpdir) / "ledger" / "old-session"
            legacy.mkdir(parents=True)
            self.assertIsNone(locate_session_data_dir(tmpdir, "old-session"))

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            self.assertEqual(locate_session_data_dir(tmpdir, "s1"), paths.run_dir)
            self.assertEqual(locate_session_data_dir(paths.run_dir, "s1"), paths.run_dir)


class ArtifactIndexV2Tests(unittest.TestCase):
    def _paths(self, tmpdir: str, session_id: str = "s1") -> SessionPaths:
        paths = SessionPaths(tmpdir, session_id)
        ensure_run_layout(paths)
        return paths

    def test_index_contains_canonical_ref_metadata_not_absolute_file_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            payload = paths.artifact_path("inventory", "registry.json")
            payload.parent.mkdir(parents=True)
            payload.write_text("{}", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                payload,
                stage="inventory",
                method_name="inventory.register_assets",
                artifact_type="asset_registry",
                metadata={"attempt_id": "inventory-0", "business_status": "must-not-be-indexed"},
            )

            refs = append_artifact_records(paths.run_dir, "s1", [artifact])
            records = read_artifact_records(paths.run_dir, "s1")

            self.assertEqual(len(refs), 1)
            self.assertEqual(refs[0].path_ref, "artifacts/inventory/registry.json")
            self.assertEqual(records[0]["artifact_id"], refs[0].artifact_id)
            self.assertEqual(records[0]["schema_version"], 2)
            self.assertEqual(records[0]["sha256"], refs[0].sha256)
            self.assertEqual(records[0]["size_bytes"], 2)
            self.assertEqual(records[0]["attempt_id"], "inventory-0")
            self.assertNotIn("file_path", records[0])
            self.assertNotIn("file_hash", records[0])
            self.assertNotIn("metadata", records[0])
            self.assertNotIn("business_status", records[0])

    def test_work_product_is_published_without_repeating_session_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.work_path("assembly", "flye", "log")
            source.parent.mkdir(parents=True)
            source.write_text("assembly completed\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )

            [artifact_ref] = append_artifact_records(paths.run_dir, "s1", [artifact])

            published = paths.resolve_ref(artifact_ref.path_ref)
            self.assertEqual(published, paths.artifact_path("assembly", "outputs", "flye", "log"))
            self.assertFalse(source.exists())
            self.assertNotIn("s1", published.relative_to(paths.run_dir).parts)
            self.assertEqual(artifact.file_path, published)
            self.assertEqual(artifact.artifact_id, artifact_ref.artifact_id)

    def test_explicit_publication_returns_readable_durable_ref_after_source_move(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.work_path("gene_annotation", "miniprot", "candidate.gff3")
            source.parent.mkdir(parents=True)
            source.write_text("##gff-version 3\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="gene_annotation",
                method_name="gene_annotation.candidate.miniprot",
                artifact_type="gene_annotation.candidate_gff",
            )

            published = publish_artifact_record(paths.run_dir, "s1", artifact)

            self.assertFalse(source.exists())
            self.assertTrue(published.path.is_file())
            self.assertEqual(published.path.read_text(encoding="utf-8"), "##gff-version 3\n")
            self.assertEqual(
                published.path,
                paths.artifact_path(
                    "gene_annotation", "outputs", "miniprot", "candidate.gff3"
                ),
            )
            self.assertEqual(
                resolve_artifact_ref(paths.run_dir, "s1", published.ref),
                published.path,
            )

    def test_strict_registration_does_not_implicitly_publish_work_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.work_path("gene_annotation", "miniprot", "candidate.gff3")
            source.parent.mkdir(parents=True)
            source.write_text("##gff-version 3\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="gene_annotation",
                method_name="gene_annotation.candidate.miniprot",
                artifact_type="gene_annotation.candidate_gff",
            )

            with self.assertRaises(DataLayoutError):
                register_artifact_records(paths.run_dir, "s1", [artifact])

            self.assertTrue(source.is_file())
            self.assertFalse(paths.artifact_index_path.exists())

    def test_identical_replay_reuses_ref_and_conflicting_bytes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.work_path("assembly", "flye", "log")
            source.parent.mkdir(parents=True)
            source.write_text("first output\n", encoding="utf-8")
            first = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            [first_ref] = append_artifact_records(paths.run_dir, "s1", [first])

            source.write_text("first output\n", encoding="utf-8")
            replay = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            [replay_ref] = append_artifact_records(paths.run_dir, "s1", [replay])
            self.assertEqual(replay_ref, first_ref)
            self.assertEqual(len(read_artifact_records(paths.run_dir, "s1")), 1)

            source.write_text("different output\n", encoding="utf-8")
            conflict = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            with self.assertRaises(FileExistsError):
                append_artifact_records(paths.run_dir, "s1", [conflict])

    def test_registration_rejects_external_reports_and_repeated_session_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            external = Path(tmpdir) / "external.txt"
            external.write_text("external", encoding="utf-8")
            external_meta = ArtifactMeta.from_file(
                external,
                stage="assembly",
                method_name="assembly.test",
                artifact_type="test.external",
            )
            with self.assertRaises(DataLayoutError):
                append_artifact_records(paths.run_dir, "s1", [external_meta])

            report = paths.report_path("session_summary.json")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("{}", encoding="utf-8")
            report_meta = ArtifactMeta.from_file(
                report,
                stage="reports",
                method_name="reports.summary",
                artifact_type="report",
            )
            with self.assertRaises(DataLayoutError):
                append_artifact_records(paths.run_dir, "s1", [report_meta])

            repeated = paths.artifact_path("assembly", "s1", "draft.fa")
            repeated.parent.mkdir(parents=True)
            repeated.write_text(">draft\nA\n", encoding="utf-8")
            repeated_meta = ArtifactMeta.from_file(
                repeated,
                stage="assembly",
                method_name="assembly.test",
                artifact_type="assembly.draft",
            )
            with self.assertRaises(DataLayoutError):
                append_artifact_records(paths.run_dir, "s1", [repeated_meta])

    def test_flat_root_and_mismatched_session_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(DataLayoutError):
                read_artifact_records(tmpdir, "s1")
            paths = self._paths(tmpdir)
            with self.assertRaises(DataLayoutError):
                read_artifact_records(paths.run_dir, "s2")


class ImmutablePublishTests(unittest.TestCase):
    def test_shell_publish_replay_reuses_identical_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "work-output"
            destination = Path(tmpdir) / "artifact-output"
            source.write_text("first output\n", encoding="utf-8")

            from app.storage.publish import shell_publish_file

            subprocess.run(shell_publish_file(source, destination), shell=True, check=True)
            source.write_text("first output\n", encoding="utf-8")
            subprocess.run(shell_publish_file(source, destination), shell=True, check=True)

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "first output\n")

    def test_runner_publishes_logs_before_collectors_build_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            work = paths.work_path("read_qc", "seqkit_stats")
            work.mkdir(parents=True)
            stdout = work / "log"
            stderr = work / "err"
            stdout.write_text("first stdout\n", encoding="utf-8")
            stderr.write_text("first stderr\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:read_qc:seqkit_stats",
                session_id="s1",
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                script_path=work / "run.sh",
                working_dir=work,
                expected_outputs=[],
                env=None,
                env_spec=None,
            )

            class StaticRunner:
                def run(self, spec):
                    return ExecutionResult(
                        attempt_id=spec.attempt_id,
                        success=True,
                        return_code=0,
                        stdout_path=stdout,
                        stderr_path=stderr,
                    )

            outcome = run_frozen_attempt(
                attempt,
                data_dir=paths.run_dir,
                fail_label="synthetic",
                runner=StaticRunner(),
            )

            self.assertTrue(outcome.success)
            self.assertEqual(
                outcome.result.stdout_path.parent,
                paths.artifact_path("read_qc", "logs"),
            )
            self.assertNotIn("s1", outcome.result.stdout_path.relative_to(paths.run_dir).parts)
            self.assertFalse(stdout.exists())

            state = {"session_id": "s1", "data_dir": str(paths.run_dir)}
            execution_ref = persist_execution_result(
                state,
                stage="read_qc",
                method_name=attempt.method_name,
                result=outcome.result,
            )
            record_path = resolve_artifact_ref(paths.run_dir, "s1", execution_ref)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            durable = record["result"]
            self.assertEqual(
                set(durable),
                {
                    "attempt_id",
                    "success",
                    "return_code",
                    "status",
                    "status_detail",
                    "error",
                    "stdout_ref",
                    "stderr_ref",
                },
            )
            with self.assertRaises(ValueError):
                ExecutionEvidence.model_validate(
                    {**durable, "stderr_path": str(outcome.result.stderr_path)}
                )
            tampered_stdout = {
                **durable["stdout_ref"],
                "sha256": "0" * 64,
            }
            with self.assertRaises(DataLayoutError):
                validate_execution_evidence(
                    state,
                    {**durable, "stdout_ref": tampered_stdout},
                )
            loaded = load_execution_evidence(
                state,
                execution_ref,
                expected_stage="read_qc",
                expected_attempt_id=attempt.attempt_id,
                expected_method_name=attempt.method_name,
            )
            _, loaded_stderr = execution_log_paths(state, loaded)
            self.assertEqual(loaded_stderr.read_text(encoding="utf-8"), "first stderr\n")
            types = [
                record["artifact_type"]
                for record in read_artifact_records(paths.run_dir, "s1")
            ]
            self.assertEqual(types.count("command.stdout"), 1)
            self.assertEqual(types.count("command.stderr"), 1)


if __name__ == "__main__":
    unittest.main()
