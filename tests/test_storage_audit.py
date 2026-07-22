from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.storage.artifacts import register_artifact_records
from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.storage.audit import (
    audit_run_storage,
    build_cleanup_manifest,
    verify_checkpoint_free_artifact_retention,
)
from app.integrity import sha256_file
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout


class StorageAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temporary.name) / "data"
        self.paths = SessionPaths(self.data_root, "audit-session")
        ensure_run_layout(self.paths)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _register_json(
        self,
        filename: str,
        payload: dict,
        *,
        artifact_type: str,
        metadata: dict | None = None,
    ) -> ArtifactRef:
        path = self.paths.artifact_path("assembly", filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        artifact = ArtifactMeta.from_file(
            path,
            stage="assembly",
            method_name="audit-test",
            artifact_type=artifact_type,
            metadata=metadata,
        )
        return register_artifact_records(
            self.paths.run_dir,
            self.paths.session_id,
            [artifact],
        )[0]

    def _write_checkpoint(self, serialization_type: str, payload: bytes) -> None:
        connection = sqlite3.connect(self.paths.checkpoint_db_path)
        try:
            connection.execute(
                "CREATE TABLE checkpoints ("
                "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
                "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB)"
            )
            connection.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("audit-session", "", "checkpoint-001", None, serialization_type, payload, b"{}"),
            )
            connection.commit()
        finally:
            connection.close()

    def _tree_snapshot(self) -> list[tuple[str, int, str]]:
        snapshot: list[tuple[str, int, str]] = []
        for path in sorted(self.paths.run_dir.rglob("*")):
            if path.is_file():
                snapshot.append(
                    (
                        path.relative_to(self.paths.run_dir).as_posix(),
                        path.stat().st_size,
                        sha256_file(path),
                    )
                )
        return snapshot

    def test_structured_audit_is_read_only_and_reports_reachability(self) -> None:
        leaf_ref = self._register_json(
            "leaf.json",
            {"value": 1},
            artifact_type="assembly.parameter.response_parse",
        )
        manifest_ref = self._register_json(
            "recommendation_manifest.json",
            {"schema_version": 2, "response_parse_ref": leaf_ref.model_dump(mode="json")},
            artifact_type="assembly.parameter.recommendation_manifest",
        )
        orphan_ref = self._register_json(
            "orphan.json",
            {"orphan": True},
            artifact_type="assembly.parameter.model_response",
        )
        unindexed = self.paths.artifact_path("assembly", "unindexed.json")
        unindexed.write_text('{"unindexed": true}\n', encoding="utf-8")

        self.paths.run_config_path.write_text(
            json.dumps({"recommendation_manifest_ref": manifest_ref.model_dump(mode="json")}),
            encoding="utf-8",
        )
        duplicate_bytes = b"same derived bytes\n"
        report = self.paths.report_path("session_summary.md")
        report.write_bytes(duplicate_bytes)
        work = self.paths.work_path("assembly", "scratch.txt")
        work.parent.mkdir(parents=True, exist_ok=True)
        work.write_bytes(duplicate_bytes)

        checkpoint = {
            "channel_values": {
                "small": "x",
                "large": "z" * 4096,
                "manifest_ref": manifest_ref.model_dump(mode="json"),
            }
        }
        self._write_checkpoint("json", json.dumps(checkpoint).encode("utf-8"))

        before = self._tree_snapshot()
        audit = audit_run_storage(self.paths.run_dir)
        after = self._tree_snapshot()

        self.assertEqual(before, after, "the default storage audit must be read-only")
        self.assertEqual(audit["schema_version"], 2)
        self.assertEqual(audit["session_id"], "audit-session")
        self.assertEqual(set(audit["layers"]), {
            "control", "ledger", "checkpoints", "artifacts", "reports", "work"
        })
        self.assertEqual(audit["layers"]["reports"]["file_count"], 1)
        self.assertEqual(audit["layers"]["reports"]["bytes"], len(duplicate_bytes))

        checkpoint_audit = audit["checkpoint"]
        self.assertEqual(checkpoint_audit["row_count"], 1)
        self.assertEqual(checkpoint_audit["parsed_row_count"], 1)
        self.assertEqual(checkpoint_audit["largest_state_fields"][0]["field"], "large")
        self.assertGreater(
            checkpoint_audit["largest_state_fields"][0]["serialized_bytes"],
            4000,
        )

        index = audit["artifact_index"]
        self.assertEqual(index["record_count"], 3)
        self.assertEqual(index["verified_record_count"], 3)
        self.assertEqual(index["reachable_record_count"], 2)
        self.assertEqual(index["unreachable_record_count"], 1)
        self.assertEqual(index["unreachable_artifacts"][0]["artifact_id"], orphan_ref.artifact_id)
        self.assertEqual(index["unindexed_file_count"], 1)
        self.assertEqual(index["unindexed_artifacts"][0]["path_ref"], self.paths.to_ref(unindexed))

        duplicate_groups = audit["duplicates"]["groups"]
        group = next(
            item for item in duplicate_groups if self.paths.to_ref(report) in item["path_refs"]
        )
        self.assertEqual(group["file_count"], 2)
        self.assertEqual(group["logical_duplicate_bytes"], len(duplicate_bytes))

        reclaimable = audit["reclaimable"]
        self.assertEqual(reclaimable["confirmed_report_bytes"], len(duplicate_bytes))
        self.assertEqual(reclaimable["conditional_work_bytes"], len(duplicate_bytes))
        manifest = build_cleanup_manifest(audit)
        self.assertTrue(manifest["dry_run"])
        self.assertFalse(manifest["deletion_performed"])
        self.assertEqual(manifest["candidate_file_count"], 1)
        self.assertEqual(manifest["candidate_bytes"], len(duplicate_bytes))
        self.assertEqual(manifest["retained_work_file_count"], 1)
        self.assertEqual(manifest["retained_work_bytes"], len(duplicate_bytes))
        self.assertFalse(any(item["path_ref"].startswith("work/") for item in manifest["entries"]))

    def test_requires_strict_v2_run_root(self) -> None:
        with self.assertRaises(DataLayoutError):
            audit_run_storage(self.data_root)
        with self.assertRaises(DataLayoutError):
            audit_run_storage(self.paths.control_dir)

    def test_corrupt_artifact_index_fails_closed(self) -> None:
        self._register_json("evidence.json", {"ok": True}, artifact_type="test.evidence")
        self.paths.artifact_index_path.write_text("{not-json}\n", encoding="utf-8")
        with self.assertRaisesRegex(DataLayoutError, "malformed artifact index"):
            audit_run_storage(self.paths.run_dir)

    def test_traversing_artifact_index_ref_fails_closed(self) -> None:
        self._register_json("evidence.json", {"ok": True}, artifact_type="test.evidence")
        record = json.loads(self.paths.artifact_index_path.read_text(encoding="utf-8"))
        record["path_ref"] = "artifacts/../control/run.json"
        self.paths.artifact_index_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaises(DataLayoutError):
            audit_run_storage(self.paths.run_dir)

    def test_registered_artifact_integrity_mismatch_fails_closed(self) -> None:
        ref = self._register_json("evidence.json", {"ok": True}, artifact_type="test.evidence")
        self.paths.resolve_ref(ref.path_ref).write_text('{"ok": false}\n', encoding="utf-8")
        with self.assertRaisesRegex(DataLayoutError, "integrity mismatch"):
            audit_run_storage(self.paths.run_dir)

    def test_legacy_ledger_asset_path_ref_is_ignored_by_retention_scan(self) -> None:
        ledger = Ledger(self.paths.run_dir)
        ledger.create_session(self.paths.session_id, "control/manifest.yaml")
        ledger.record_session_terminal(
            self.paths.session_id,
            outcome="completed",
            stage="done",
            refs=[
                {
                    "role": "terminal.completed.stage_outputs.read_qc.input_asset_ref",
                    "path_ref": "art_session:raw_long_reads:reads.fastq",
                }
            ],
        )

        audit = audit_run_storage(self.paths.run_dir)
        retention = verify_checkpoint_free_artifact_retention(
            self.paths.run_dir,
            {
                "stage_outputs": {
                    "read_qc": {
                        "input_asset_ref": "art_session:raw_long_reads:reads.fastq"
                    }
                }
            },
        )

        self.assertEqual(audit["artifact_index"]["record_count"], 0)
        self.assertEqual(retention["missing_artifact_ids"], [])

    def test_ledger_replaces_transient_artifact_id_with_indexed_ref(self) -> None:
        indexed_ref = self._register_json(
            "attempt-output.json",
            {"value": 1},
            artifact_type="test.attempt_output",
        )
        transient = ArtifactMeta.from_file(
            self.paths.resolve_ref(indexed_ref.path_ref),
            stage="assembly",
            method_name="audit-test",
            artifact_type="test.attempt_output",
        )
        self.assertNotEqual(transient.artifact_id, indexed_ref.artifact_id)
        ledger = Ledger(self.paths.run_dir)
        ledger.create_session(self.paths.session_id, "control/manifest.yaml")

        ledger.record_event(
            self.paths.session_id,
            "attempt_succeeded",
            {"artifacts": [transient.model_dump(mode="json")]},
            stage="assembly",
            subject_id="attempt-001",
        )

        row = ledger.read_event_rows(self.paths.session_id)[-1]
        self.assertEqual(row["refs"][0]["artifact_id"], indexed_ref.artifact_id)
        self.assertEqual(row["refs"][0]["path_ref"], indexed_ref.path_ref)
        audit = audit_run_storage(self.paths.run_dir)
        self.assertEqual(audit["artifact_index"]["reachable_record_count"], 1)

    def test_conflicting_artifact_ref_error_identifies_role_id_and_path(self) -> None:
        first = self._register_json(
            "first.json",
            {"value": 1},
            artifact_type="test.first",
        )
        second = self._register_json(
            "second.json",
            {"value": 2},
            artifact_type="test.second",
        )
        conflict = first.model_dump(mode="json")
        conflict["role"] = "stage.assembly.output_artifact_ref"
        conflict["path_ref"] = second.path_ref
        self.paths.run_config_path.write_text(
            json.dumps({"result_artifact_ref": conflict}),
            encoding="utf-8",
        )

        with self.assertRaises(DataLayoutError) as raised:
            audit_run_storage(self.paths.run_dir)

        message = str(raised.exception)
        self.assertIn("role=stage.assembly.output_artifact_ref", message)
        self.assertIn(f"artifact_id={first.artifact_id}", message)
        self.assertIn(f"path_ref={second.path_ref}", message)

    def test_unsafe_checkpoint_serialization_is_only_a_warning(self) -> None:
        self._write_checkpoint("pickle", b"not loaded")
        audit = audit_run_storage(self.paths.run_dir)
        self.assertEqual(audit["checkpoint"]["row_count"], 1)
        self.assertEqual(audit["checkpoint"]["parsed_row_count"], 0)
        self.assertTrue(
            any("unsupported serialization type" in warning for warning in audit["warnings"])
        )

    def test_live_checkpoint_wal_is_not_opened_mutably(self) -> None:
        checkpoint = {"channel_values": {"field": "value"}}
        self._write_checkpoint("json", json.dumps(checkpoint).encode("utf-8"))
        wal = Path(f"{self.paths.checkpoint_db_path}-wal")
        wal.write_bytes(b"uncheckpointed-placeholder")
        before = self._tree_snapshot()
        audit = audit_run_storage(self.paths.run_dir)
        self.assertEqual(before, self._tree_snapshot())
        self.assertEqual(audit["checkpoint"]["parsed_row_count"], 0)
        self.assertTrue(any("uncheckpointed WAL" in item for item in audit["warnings"]))

    def test_symlink_inside_run_fails_closed(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.paths.work_path("assembly", "escape")
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(DataLayoutError, "symlink is not allowed"):
            audit_run_storage(self.paths.run_dir)

    def test_cleanup_manifest_rejects_injected_traversal(self) -> None:
        audit = audit_run_storage(self.paths.run_dir)
        audit["reclaimable"]["files"] = [
            {
                "path_ref": "../outside",
                "sha256": "0" * 64,
                "size_bytes": 1,
                "reason": "injected",
                "requires_output_verification": False,
            }
        ]
        with self.assertRaises(ValueError):
            build_cleanup_manifest(audit)


if __name__ == "__main__":
    unittest.main()
