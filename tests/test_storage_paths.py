"""Regression coverage for the forward-only session storage layout."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records, read_artifact_records
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.execution.results import ExecutionResult
from app.schemas.artifacts import ArtifactMeta
from app.storage.paths import SessionPaths, locate_session_data_dir


class SessionPathsTests(unittest.TestCase):
    def test_new_session_paths_are_contained_under_runs_session(self):
        paths = SessionPaths("/tmp/gene-agent-data", "s1")

        self.assertEqual(paths.run_dir, Path("/tmp/gene-agent-data/runs/s1").resolve(strict=False))
        self.assertEqual(paths.control_dir, paths.run_dir / "control")
        self.assertEqual(paths.artifacts_dir, paths.run_dir / "artifacts")
        self.assertEqual(paths.work_dir, paths.run_dir / "work")
        self.assertEqual(paths.manifest_path, paths.control_dir / "manifest.yaml")

    def test_new_layout_uses_one_ledger_and_artifact_index_per_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            payload = paths.artifacts_dir / "inventory" / "s1" / "registry.json"
            payload.parent.mkdir(parents=True)
            payload.write_text("{}", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                payload,
                stage="inventory",
                method_name="inventory.register_assets",
                artifact_type="asset_registry",
            )

            append_artifact_records(paths.run_dir, "s1", [artifact])
            ledger = Ledger(paths.run_dir)
            ledger.create_session("s1", "manifest.yaml")
            ledger.record_artifact_registered("s1", artifact.model_dump(mode="json"))

            self.assertTrue((paths.artifacts_dir / "artifacts.jsonl").exists())
            self.assertTrue((paths.ledger_dir / "session.json").exists())
            self.assertFalse((paths.artifacts_dir / "s1" / "artifacts.jsonl").exists())
            self.assertEqual(read_artifact_records(paths.run_dir, "s1")[0]["file_path"], str(payload))
            event = ledger.read_events("s1")[0]
            self.assertEqual(event["artifact"]["file_path"], str(payload))
            self.assertNotIn("file_hash", event["artifact"])

    def test_work_backed_artifact_replay_reuses_identical_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            source = paths.work_dir / "assembly" / "s1" / "flye" / "log"
            source.parent.mkdir(parents=True)
            source.write_text("assembly completed\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )

            append_artifact_records(paths.run_dir, "s1", [artifact])

            published = Path(artifact.file_path)
            self.assertTrue(published.is_relative_to(paths.artifacts_dir))
            self.assertFalse(source.exists())
            self.assertEqual(published.read_text(encoding="utf-8"), "assembly completed\n")
            source.write_text("assembly completed\n", encoding="utf-8")
            retry_artifact = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            append_artifact_records(paths.run_dir, "s1", [retry_artifact])
            self.assertFalse(source.exists())
            self.assertEqual(retry_artifact.file_path, published)
            self.assertEqual(published.read_text(encoding="utf-8"), "assembly completed\n")
            records = read_artifact_records(paths.run_dir, "s1")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["file_path"], str(published))

    def test_work_backed_artifact_replay_rejects_different_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            source = paths.work_dir / "assembly" / "s1" / "flye" / "log"
            source.parent.mkdir(parents=True)
            source.write_text("first output\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            append_artifact_records(paths.run_dir, "s1", [artifact])

            source.write_text("different output\n", encoding="utf-8")
            retry_artifact = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            with self.assertRaises(FileExistsError):
                append_artifact_records(paths.run_dir, "s1", [retry_artifact])

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

    def test_runner_returns_published_log_paths_before_collectors_build_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            work = paths.work_dir / "read_qc" / "s1" / "seqkit_stats"
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
            self.assertTrue(outcome.result.stdout_path.is_relative_to(paths.artifacts_dir))
            self.assertFalse(stdout.exists())
            self.assertEqual(outcome.result.stdout_path.read_text(encoding="utf-8"), "first stdout\n")

    def test_existing_flat_session_remains_discoverable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path(tmpdir) / "ledger" / "old-session"
            legacy.mkdir(parents=True)

            self.assertEqual(locate_session_data_dir(tmpdir, "old-session"), Path(tmpdir).resolve())
            self.assertIsNone(locate_session_data_dir(tmpdir, "new-session"))
