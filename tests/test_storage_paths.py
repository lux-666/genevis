"""Regression coverage for the forward-only Data Architecture v2 storage layer."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.execution.evidence import (
    ExecutionEvidence,
    execution_log_paths,
    load_execution_evidence,
    load_frozen_attempt,
    persist_execution_result,
    persist_frozen_attempt,
    read_execution_stderr_tail,
    validate_execution_evidence,
)
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.schemas.artifacts import ArtifactMeta
from app.schemas.execution import ExecutionResult
from app.storage.artifacts import (
    publish_artifact_record,
    publish_artifact_records,
    publish_or_recover_work_artifact,
    read_artifact_records,
    register_artifact_records,
    resolve_artifact_ref,
)
from app.storage.paths import (
    DataLayoutError,
    SessionPaths,
    ensure_data_layout,
    ensure_run_layout,
    locate_session_data_dir,
)


def workflow_state(paths: SessionPaths, step_id: str, stage: str) -> dict[str, object]:
    """Build the minimum frozen occurrence context required by evidence APIs."""

    return {
        "session_id": paths.session_id,
        "data_dir": str(paths.run_dir),
        "workflow_steps": [{"step_id": step_id, "stage": stage}],
        "workflow_step_index": 0,
    }


def workflow_metadata(step_id: str) -> dict[str, str]:
    return {"workflow_step_id": step_id}


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
        self.assertEqual(
            paths.checkpoint_db_path, paths.checkpoints_dir / "checkpoints.sqlite"
        )
        self.assertEqual(
            paths.workflow_step_artifact_path(
                "step-001", "assembly", "outputs", "draft.fa"
            ),
            paths.artifacts_dir
            / "steps"
            / "step-001"
            / "assembly"
            / "outputs"
            / "draft.fa",
        )
        self.assertNotIn(
            "s1",
            paths.workflow_step_artifact_path("step-001", "assembly", "outputs")
            .relative_to(paths.run_dir)
            .parts,
        )

    def test_resume_requires_the_retained_work_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_id = "step-001"
            work_dir = paths.workflow_step_work_path(step_id, "read_qc", "attempt")
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
                metadata=workflow_metadata(step_id),
            )
            state = workflow_state(paths, step_id, "read_qc")
            attempt_ref = persist_frozen_attempt(state, attempt)
            script.unlink()

            with self.assertRaisesRegex(DataLayoutError, "work script is missing"):
                load_frozen_attempt(
                    state,
                    attempt_ref,
                    expected_stage="read_qc",
                    expected_method_name=attempt.method_name,
                )

    def test_resume_rejects_retained_script_byte_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_id = "step-001"
            work_dir = paths.workflow_step_work_path(step_id, "read_qc", "attempt")
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
                metadata=workflow_metadata(step_id),
            )
            state = workflow_state(paths, step_id, "read_qc")
            attempt_ref = persist_frozen_attempt(state, attempt)
            script.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")

            with self.assertRaisesRegex(DataLayoutError, "changed after freeze"):
                load_frozen_attempt(
                    state,
                    attempt_ref,
                    expected_stage="read_qc",
                    expected_method_name=attempt.method_name,
                )

    def test_frozen_attempt_rejects_working_directory_outside_stage_work(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_id = "step-001"
            outside_work = Path(tmpdir) / "outside-work"
            outside_work.mkdir()
            script = outside_work / "run.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:read_qc:outside-work",
                session_id="s1",
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                script_path=script,
                working_dir=outside_work,
                expected_outputs=[],
                env=None,
                env_spec=None,
                metadata=workflow_metadata(step_id),
            )

            with self.assertRaisesRegex(
                DataLayoutError,
                "working directory is outside",
            ):
                persist_frozen_attempt(
                    workflow_state(paths, step_id, "read_qc"),
                    attempt,
                )

    def test_frozen_attempt_rejects_expected_output_outside_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_id = "step-001"
            work_dir = paths.workflow_step_work_path(step_id, "read_qc", "attempt")
            work_dir.mkdir(parents=True)
            script = work_dir / "run.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:read_qc:outside-output",
                session_id="s1",
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                script_path=script,
                working_dir=work_dir,
                expected_outputs=[Path(tmpdir) / "outside-output.tsv"],
                env=None,
                env_spec=None,
                metadata=workflow_metadata(step_id),
            )

            with self.assertRaisesRegex(DataLayoutError, "expected output is outside"):
                persist_frozen_attempt(
                    workflow_state(paths, step_id, "read_qc"),
                    attempt,
                )

    def test_frozen_attempt_rejects_work_script_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_id = "step-001"
            work_dir = paths.workflow_step_work_path(step_id, "read_qc", "attempt")
            work_dir.mkdir(parents=True)
            external_script = Path(tmpdir) / "external.sh"
            external_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script = work_dir / "run.sh"
            script.symlink_to(external_script)
            attempt = build_frozen_attempt(
                attempt_id="s1:read_qc:symlink-escape",
                session_id="s1",
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                script_path=script,
                working_dir=work_dir,
                expected_outputs=[],
                env=None,
                env_spec=None,
                metadata=workflow_metadata(step_id),
            )

            with self.assertRaisesRegex(DataLayoutError, "script resolves outside"):
                persist_frozen_attempt(
                    workflow_state(paths, step_id, "read_qc"),
                    attempt,
                )

    def test_frozen_attempt_accepts_run_local_artifact_script_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_id = "step-001"
            work_dir = paths.workflow_step_work_path(
                step_id, "hic_scaffolding", "autohic"
            )
            work_dir.mkdir(parents=True)
            artifact_script = paths.workflow_step_artifact_path(
                step_id, "hic_scaffolding", "attempts", "000", "run_autohic.sh"
            )
            artifact_script.parent.mkdir(parents=True)
            artifact_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script = work_dir / "run_autohic.sh"
            script.symlink_to(artifact_script)
            expected_output = paths.workflow_step_artifact_path(
                step_id, "hic_scaffolding", "outputs", "result.html"
            )
            attempt = build_frozen_attempt(
                attempt_id="s1:hic_scaffolding:autohic",
                session_id="s1",
                stage="hic_scaffolding",
                method_name="hic_scaffolding.autohic",
                script_path=script,
                working_dir=work_dir,
                expected_outputs=[expected_output],
                env=None,
                env_spec=None,
                metadata=workflow_metadata(step_id),
            )
            state = workflow_state(paths, step_id, "hic_scaffolding")

            attempt_ref = persist_frozen_attempt(state, attempt)
            loaded = load_frozen_attempt(
                state,
                attempt_ref,
                expected_stage="hic_scaffolding",
                expected_method_name=attempt.method_name,
            )

            self.assertEqual(loaded.script_path, script)
            self.assertTrue(loaded.script_path.is_symlink())
            self.assertEqual(loaded.script_sha256, attempt.script_sha256)
            self.assertEqual(loaded.expected_outputs, [expected_output])

    def test_from_run_dir_is_strict_and_validates_session_id(self):
        paths = SessionPaths("/tmp/gene-agent-data", "s1")

        self.assertEqual(SessionPaths.from_run_dir(paths.run_dir), paths)
        with self.assertRaises(DataLayoutError):
            SessionPaths.from_run_dir("/tmp/not-a-run-root")
        with self.assertRaises(DataLayoutError):
            paths.require_session_id("s2")
        with self.assertRaises(DataLayoutError):
            SessionPaths("/tmp/gene-agent-data", "../escape")

    def test_session_id_rejects_boundary_whitespace_and_backslashes(self):
        for invalid in (" s1", "s1 ", "s\\1", "./s1", "s1/.", "s1/"):
            with self.subTest(invalid=invalid), self.assertRaises(DataLayoutError):
                SessionPaths("/tmp/gene-agent-data", invalid)

    def test_refs_are_normalized_and_contained_by_run_root(self):
        paths = SessionPaths("/tmp/gene-agent-data", "s1")
        artifact = paths.workflow_step_artifact_path(
            "step-001", "assembly", "outputs", "draft.fa"
        )

        self.assertEqual(
            paths.to_ref(artifact),
            "artifacts/steps/step-001/assembly/outputs/draft.fa",
        )
        self.assertEqual(
            paths.resolve_ref("artifacts/steps/step-001/assembly/outputs/draft.fa"),
            artifact,
        )
        for invalid in (
            "/tmp/escape",
            "../escape",
            "artifacts/../control/run.json",
            "",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(DataLayoutError):
                paths.resolve_ref(invalid)
        with self.assertRaises(DataLayoutError):
            paths.to_ref("/tmp/outside-run")

    def test_layout_marker_is_v2_and_rejects_flat_runtime_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = ensure_data_layout(tmpdir)
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8")), {"schema_version": 2}
            )
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
            self.assertEqual(
                locate_session_data_dir(paths.run_dir, "s1"), paths.run_dir
            )


class ArtifactIndexV2Tests(unittest.TestCase):
    def _paths(self, tmpdir: str, session_id: str = "s1") -> SessionPaths:
        paths = SessionPaths(tmpdir, session_id)
        ensure_run_layout(paths)
        return paths

    def test_index_contains_canonical_ref_metadata_not_absolute_file_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            step_id = "step-001"
            payload = paths.workflow_step_artifact_path(
                step_id, "inventory", "registry.json"
            )
            payload.parent.mkdir(parents=True)
            payload.write_text("{}", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                payload,
                stage="inventory",
                method_name="inventory.register_assets",
                artifact_type="asset_registry",
                metadata={
                    "attempt_id": "inventory-0",
                    "business_status": "must-not-be-indexed",
                },
            )

            refs = register_artifact_records(paths.run_dir, "s1", [artifact])
            records = read_artifact_records(paths.run_dir, "s1")

            self.assertEqual(len(refs), 1)
            self.assertEqual(
                refs[0].path_ref,
                "artifacts/steps/step-001/inventory/registry.json",
            )
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
            step_id = "step-001"
            source = paths.workflow_step_work_path(step_id, "assembly", "flye", "log")
            source.parent.mkdir(parents=True)
            source.write_text("assembly completed\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )

            [artifact_ref] = publish_artifact_records(paths.run_dir, "s1", [artifact])

            published = paths.resolve_ref(artifact_ref.path_ref)
            self.assertEqual(
                published,
                paths.workflow_step_artifact_path(
                    step_id, "assembly", "outputs", "flye", "log"
                ),
            )
            self.assertFalse(source.exists())
            self.assertNotIn("s1", published.relative_to(paths.run_dir).parts)
            self.assertEqual(artifact.file_path, published)
            self.assertEqual(artifact.artifact_id, artifact_ref.artifact_id)

    def test_explicit_publication_returns_readable_durable_ref_after_source_move(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            step_id = "step-001"
            source = paths.workflow_step_work_path(
                step_id, "assembly_review", "quast", "report.txt"
            )
            source.parent.mkdir(parents=True)
            source.write_text("##gff-version 3\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="assembly_review",
                method_name="assembly_review.eval.quast",
                artifact_type="assembly_review.report",
            )

            published = publish_artifact_record(paths.run_dir, "s1", artifact)

            self.assertFalse(source.exists())
            self.assertTrue(published.path.is_file())
            self.assertEqual(
                published.path.read_text(encoding="utf-8"), "##gff-version 3\n"
            )
            self.assertEqual(
                published.path,
                paths.workflow_step_artifact_path(
                    step_id, "assembly_review", "outputs", "quast", "report.txt"
                ),
            )
            self.assertEqual(
                resolve_artifact_ref(paths.run_dir, "s1", published.ref),
                published.path,
            )

    def test_strict_registration_does_not_implicitly_publish_work_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.workflow_step_work_path(
                "step-001", "assembly_review", "quast", "report.txt"
            )
            source.parent.mkdir(parents=True)
            source.write_text("##gff-version 3\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="assembly_review",
                method_name="assembly_review.eval.quast",
                artifact_type="assembly_review.report",
            )

            with self.assertRaises(DataLayoutError):
                register_artifact_records(paths.run_dir, "s1", [artifact])

            self.assertTrue(source.is_file())
            self.assertFalse(paths.artifact_index_path.exists())

    def test_collect_replay_recovers_moved_work_output_and_index_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.workflow_step_work_path(
                "step-001", "read_qc", "seqkit", "stats.tsv"
            )
            source.parent.mkdir(parents=True)
            source.write_text("file\tnum_seqs\nreads.fq\t2\n", encoding="utf-8")

            first = publish_or_recover_work_artifact(
                paths.run_dir,
                "s1",
                source,
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )
            paths.artifact_index_path.unlink()

            replay = publish_or_recover_work_artifact(
                paths.run_dir,
                "s1",
                source,
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )

            self.assertFalse(source.exists())
            self.assertEqual(replay.path, first.path)
            self.assertEqual(replay.ref, first.ref)
            self.assertEqual(len(read_artifact_records(paths.run_dir, "s1")), 1)

    def test_publication_rejects_a_source_already_below_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            destination = paths.workflow_step_artifact_path(
                "step-001", "inventory", "registry.json"
            )
            destination.parent.mkdir(parents=True)
            destination.write_text("old bytes\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                destination,
                stage="inventory",
                method_name="inventory.register_assets",
                artifact_type="asset_registry",
            )

            with self.assertRaisesRegex(DataLayoutError, "work path is outside"):
                publish_artifact_records(paths.run_dir, "s1", [artifact])

            self.assertEqual(destination.read_text(encoding="utf-8"), "old bytes\n")
            self.assertFalse(paths.artifact_index_path.exists())

    def test_identical_replay_reuses_ref_and_conflicting_bytes_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.workflow_step_work_path(
                "step-001", "assembly", "flye", "log"
            )
            source.parent.mkdir(parents=True)
            source.write_text("first output\n", encoding="utf-8")
            first = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            [first_ref] = publish_artifact_records(paths.run_dir, "s1", [first])
            published_path = paths.resolve_ref(first_ref.path_ref)
            first_records = read_artifact_records(paths.run_dir, "s1")

            source.write_text("first output\n", encoding="utf-8")
            replay = ArtifactMeta.from_file(
                source,
                stage="assembly",
                method_name="assembly.candidate.flye",
                artifact_type="command.stdout",
            )
            [replay_ref] = publish_artifact_records(paths.run_dir, "s1", [replay])
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
                publish_artifact_records(paths.run_dir, "s1", [conflict])
            self.assertEqual(
                published_path.read_text(encoding="utf-8"), "first output\n"
            )
            self.assertEqual(read_artifact_records(paths.run_dir, "s1"), first_records)

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
                publish_artifact_records(paths.run_dir, "s1", [external_meta])

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
                publish_artifact_records(paths.run_dir, "s1", [report_meta])

            repeated = paths.workflow_step_artifact_path(
                "step-001", "assembly", "s1", "draft.fa"
            )
            repeated.parent.mkdir(parents=True)
            repeated.write_text(">draft\nA\n", encoding="utf-8")
            repeated_meta = ArtifactMeta.from_file(
                repeated,
                stage="assembly",
                method_name="assembly.test",
                artifact_type="assembly.draft",
            )
            with self.assertRaises(DataLayoutError):
                publish_artifact_records(paths.run_dir, "s1", [repeated_meta])

    def test_flat_root_and_mismatched_session_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(DataLayoutError):
                read_artifact_records(tmpdir, "s1")
            paths = self._paths(tmpdir)
            with self.assertRaises(DataLayoutError):
                read_artifact_records(paths.run_dir, "s2")

    def test_same_stage_occurrences_publish_to_isolated_work_and_artifact_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            first_step_id = "step-001"
            second_step_id = "step-002"
            first_source = paths.workflow_step_work_path(
                first_step_id, "read_qc", "seqkit", "stats.tsv"
            )
            second_source = paths.workflow_step_work_path(
                second_step_id, "read_qc", "seqkit", "stats.tsv"
            )
            first_source.parent.mkdir(parents=True)
            second_source.parent.mkdir(parents=True)
            first_source.write_text("first occurrence\n", encoding="utf-8")
            second_source.write_text("second occurrence\n", encoding="utf-8")

            first = publish_or_recover_work_artifact(
                paths.run_dir,
                "s1",
                first_source,
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )
            second = publish_or_recover_work_artifact(
                paths.run_dir,
                "s1",
                second_source,
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )

            self.assertNotEqual(first.path, second.path)
            self.assertEqual(
                first.path,
                paths.workflow_step_artifact_path(
                    first_step_id, "read_qc", "outputs", "seqkit", "stats.tsv"
                ),
            )
            self.assertEqual(
                second.path,
                paths.workflow_step_artifact_path(
                    second_step_id, "read_qc", "outputs", "seqkit", "stats.tsv"
                ),
            )
            self.assertEqual(
                first.path.read_text(encoding="utf-8"), "first occurrence\n"
            )
            self.assertEqual(
                second.path.read_text(encoding="utf-8"), "second occurrence\n"
            )
            self.assertEqual(
                [
                    record["workflow_step_id"]
                    for record in read_artifact_records(paths.run_dir, "s1")
                ],
                [first_step_id, second_step_id],
            )

    def test_explicit_destination_rejects_cross_occurrence_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = self._paths(tmpdir)
            source = paths.workflow_step_work_path(
                "step-001-read_qc", "read_qc", "seqkit", "stats.tsv"
            )
            source.parent.mkdir(parents=True)
            source.write_text("first occurrence\n", encoding="utf-8")
            artifact = ArtifactMeta.from_file(
                source,
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                artifact_type="read_qc.long_read_stats",
            )
            destination = paths.workflow_step_artifact_path(
                "step-002-read_qc", "read_qc", "stats.tsv"
            )

            with self.assertRaisesRegex(DataLayoutError, "same workflow step"):
                publish_artifact_record(
                    paths.run_dir,
                    "s1",
                    artifact,
                    destination=destination,
                )

            self.assertTrue(source.is_file())
            self.assertFalse(destination.exists())
            self.assertFalse(paths.artifact_index_path.exists())


class ImmutablePublishTests(unittest.TestCase):
    def test_shell_publish_replay_reuses_identical_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "work-output"
            destination = Path(tmpdir) / "artifact-output"
            source.write_text("first output\n", encoding="utf-8")

            from app.storage.publish import shell_publish_file

            subprocess.run(
                shell_publish_file(source, destination), shell=True, check=True
            )
            source.write_text("first output\n", encoding="utf-8")
            subprocess.run(
                shell_publish_file(source, destination), shell=True, check=True
            )

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_text(encoding="utf-8"), "first output\n")

    def test_same_stage_occurrences_isolate_attempt_and_log_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_ids = ("step-001", "step-002")
            state = {
                "session_id": "s1",
                "data_dir": str(paths.run_dir),
                "workflow_steps": [
                    {"step_id": step_id, "stage": "read_qc"} for step_id in step_ids
                ],
                "workflow_step_index": 0,
            }
            attempts = []
            for step_id in step_ids:
                work_dir = paths.workflow_step_work_path(
                    step_id, "read_qc", "seqkit_stats"
                )
                work_dir.mkdir(parents=True)
                script = work_dir / "run.sh"
                script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                (work_dir / "stdout.log").write_text(step_id, encoding="utf-8")
                (work_dir / "stderr.log").write_text(step_id, encoding="utf-8")
                attempts.append(
                    build_frozen_attempt(
                        attempt_id=f"s1:{step_id}:seqkit_stats",
                        session_id="s1",
                        stage="read_qc",
                        method_name="read_qc.long_read_stats.seqkit_stats",
                        script_path=script,
                        working_dir=work_dir,
                        expected_outputs=[],
                        env=None,
                        env_spec=None,
                        metadata=workflow_metadata(step_id),
                    )
                )

            attempt_refs = []
            for index, attempt in enumerate(attempts):
                state["workflow_step_index"] = index
                attempt_refs.append(persist_frozen_attempt(state, attempt))

            class WorkLogRunner:
                def run(self, spec):
                    return ExecutionResult(
                        attempt_id=spec.attempt_id,
                        success=True,
                        return_code=0,
                        stdout_path=spec.working_dir / "stdout.log",
                        stderr_path=spec.working_dir / "stderr.log",
                    )

            outcomes = [
                run_frozen_attempt(
                    attempt,
                    data_dir=paths.run_dir,
                    fail_label="synthetic",
                    runner=WorkLogRunner(),
                )
                for attempt in attempts
            ]

            self.assertNotEqual(attempt_refs[0].path_ref, attempt_refs[1].path_ref)
            self.assertNotEqual(
                outcomes[0].result.stdout_path, outcomes[1].result.stdout_path
            )
            for step_id, outcome in zip(step_ids, outcomes, strict=True):
                self.assertTrue(outcome.success)
                self.assertEqual(
                    outcome.result.stdout_path.parent,
                    paths.workflow_step_artifact_path(step_id, "read_qc", "logs"),
                )
                self.assertEqual(
                    outcome.result.stderr_path.parent,
                    paths.workflow_step_artifact_path(step_id, "read_qc", "logs"),
                )
                self.assertEqual(
                    outcome.result.stdout_path.read_text(encoding="utf-8"), step_id
                )
            records = read_artifact_records(paths.run_dir, "s1")
            self.assertEqual(
                {record["workflow_step_id"] for record in records}, set(step_ids)
            )

    def test_runner_publishes_logs_before_collectors_build_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = SessionPaths(tmpdir, "s1")
            ensure_run_layout(paths)
            step_id = "step-001"
            work = paths.workflow_step_work_path(step_id, "read_qc", "seqkit_stats")
            work.mkdir(parents=True)
            stdout = work / "log"
            stderr = work / "err"
            stdout.write_text("first stdout\n", encoding="utf-8")
            stderr_text = (
                "discarded diagnostic\n" * 1_000
                + "penultimate diagnostic\nfinal diagnostic\n"
            )
            stderr.write_text(stderr_text, encoding="utf-8")
            script = work / "run.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            attempt = build_frozen_attempt(
                attempt_id="s1:read_qc:seqkit_stats",
                session_id="s1",
                stage="read_qc",
                method_name="read_qc.long_read_stats.seqkit_stats",
                script_path=script,
                working_dir=work,
                expected_outputs=[],
                env=None,
                env_spec=None,
                metadata=workflow_metadata(step_id),
            )
            state = workflow_state(paths, step_id, "read_qc")
            frozen_attempt_ref = persist_frozen_attempt(state, attempt)

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
                paths.workflow_step_artifact_path(step_id, "read_qc", "logs"),
            )
            self.assertNotIn(
                "s1", outcome.result.stdout_path.relative_to(paths.run_dir).parts
            )
            self.assertFalse(stdout.exists())

            execution_ref = persist_execution_result(
                state,
                stage="read_qc",
                method_name=attempt.method_name,
                frozen_attempt_ref=frozen_attempt_ref,
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
                expected_frozen_attempt_ref=frozen_attempt_ref,
            )
            _, loaded_stderr = execution_log_paths(state, loaded)
            self.assertEqual(loaded_stderr.read_text(encoding="utf-8"), stderr_text)
            self.assertEqual(
                read_execution_stderr_tail(state, loaded, max_bytes=128, max_lines=2),
                "penultimate diagnostic\nfinal diagnostic",
            )
            types = [
                record["artifact_type"]
                for record in read_artifact_records(paths.run_dir, "s1")
            ]
            self.assertEqual(types.count("command.stdout"), 1)
            self.assertEqual(types.count("command.stderr"), 1)


if __name__ == "__main__":
    unittest.main()
