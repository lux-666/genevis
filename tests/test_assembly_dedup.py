"""Tests for the assembly_dedup stage."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.control.workflow_plan import workflow_steps_from_run_config
from app.graph.builder import build_graph
from app.integrity import sha256_file
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ArtifactMeta,
    AssemblyCandidateBundle,
)
from app.schemas.resource_ref import ResourceRef
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.stages.assembly_dedup.builder import build_stage as build_dedup_stage
from app.stages.assembly_review.nodes import resolve_candidates
from app.storage.artifacts import publish_artifact_record
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class AssemblyDedupStageTests(unittest.TestCase):
    def _state(self, tmpdir: str):
        run_dir = v2_run_dir(tmpdir, "s1")
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            workflow_steps=[
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-001-assembly",
                    "stage": "assembly",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-002-assembly_dedup",
                    "stage": "assembly_dedup",
                    "allow_local_execution": True,
                },
                {
                    "step_id": "step-003-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": False,
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-002-assembly_dedup",
                    },
                },
            ],
        )
        state["current_stage"] = "assembly_dedup"
        state["workflow_step_index"] = 2
        state["entered_workflow_step_index"] = 2
        self._publish_asset_registry(state, assets=[])
        return state

    @staticmethod
    def _publish_asset_registry(state: dict, *, assets: list[dict]) -> None:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        source = paths.workflow_step_work_path(
            "step-000-inventory",
            "inventory",
            f"asset_registry_{len(assets)}.json",
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": state["session_id"],
                    "assets": assets,
                }
            ),
            encoding="utf-8",
        )
        state["asset_registry_ref"] = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            ArtifactMeta.from_file(
                source,
                stage="inventory",
                method_name="inventory.register_assets",
                artifact_type="asset_registry",
            ),
        ).ref.model_dump(mode="json")

    @staticmethod
    def _publish_candidate(
        state: dict,
        *,
        candidate_id: str,
        method_name: str,
        draft: Path,
        member_id: str,
        platform: str = "ont",
    ) -> tuple[dict, dict]:
        """Publish one typed, indexed executed candidate and its primary output."""

        data_dir = state["data_dir"]
        session_id = state["session_id"]
        paths = SessionPaths.from_run_dir(data_dir)
        safe_candidate_id = candidate_id.replace(":", "_")
        work_dir = paths.workflow_step_work_path(
            "step-001-assembly", "assembly", "fixture_candidates", safe_candidate_id
        )
        artifact_dir = paths.workflow_step_artifact_path(
            "step-001-assembly", "assembly", "fixture_candidates", safe_candidate_id
        )
        work_dir.mkdir(parents=True, exist_ok=True)

        primary_source = work_dir / "draft.fasta"
        primary_source.write_bytes(draft.read_bytes())
        primary = publish_artifact_record(
            data_dir,
            session_id,
            ArtifactMeta.from_file(
                primary_source,
                stage="assembly",
                method_name=method_name,
                artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
            ),
            destination=artifact_dir / "draft.fasta",
        )

        planned = AssemblyCandidateBundle(
            session_id=session_id,
            candidate_id=candidate_id,
            method_name=method_name,
            status="planned",
            input_type="clean_long_reads",
            input_member_ids={"primary": [member_id]},
            input_member_platforms={"primary": [platform]},
            parameter_attempt_id=0,
            expected_outputs=["primary_assembly"],
        )
        planned_source = work_dir / "candidate.planned.json"
        planned_source.write_text(planned.model_dump_json(), encoding="utf-8")
        planned_ref = publish_artifact_record(
            data_dir,
            session_id,
            ArtifactMeta.from_file(
                planned_source,
                stage="assembly",
                method_name=method_name,
                artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
            ),
            destination=artifact_dir / "candidate.planned.json",
        ).ref

        executed = planned.model_copy(
            update={
                "status": "executed",
                "plan_ref": planned_ref,
                "primary_output": primary.ref,
            }
        )
        executed_source = work_dir / "candidate.executed.json"
        executed_source.write_text(executed.model_dump_json(), encoding="utf-8")
        executed_ref = publish_artifact_record(
            data_dir,
            session_id,
            ArtifactMeta.from_file(
                executed_source,
                stage="assembly",
                method_name=method_name,
                artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
            ),
            destination=artifact_dir / "candidate.executed.json",
        ).ref
        return (
            executed_ref.model_dump(mode="json"),
            primary.ref.model_dump(mode="json"),
        )

    @staticmethod
    def _set_assembly_resource(
        state: dict,
        *,
        primary_path: Path,
        candidate_bundle_ref: dict,
        primary_output_ref: dict,
        source_stage: str,
        workflow_step_id: str,
        raw_candidate_bundle_ref: dict | None = None,
        raw_primary_output_ref: dict | None = None,
    ) -> None:
        metadata = {
            "candidate_bundle_ref": candidate_bundle_ref,
            "primary_output_ref": primary_output_ref,
        }
        if raw_candidate_bundle_ref is not None:
            metadata["raw_candidate_bundle_ref"] = raw_candidate_bundle_ref
        if raw_primary_output_ref is not None:
            metadata["raw_primary_output_ref"] = raw_primary_output_ref
        state["resource_refs"]["assembly"] = ResourceRef(
            resource_type="assembly",
            paths=[str(primary_path)],
            source_stage=source_stage,
            workflow_step_id=workflow_step_id,
            metadata=metadata,
        ).to_dict()

    def test_local_dedup_without_materialized_assembly_fails_occurrence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            config = RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "assembly_dedup", "allow_local_execution": True}
                    ]
                }
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(config),
            )
            state["current_stage"] = "assembly_dedup"
            state["workflow_step_index"] = 1
            state["entered_workflow_step_index"] = 1
            state["stage_outputs"] = {"assembly_dedup": {"execution_mode": "local"}}
            self._publish_asset_registry(state, assets=[])
            Ledger(run_dir).create_session("s1", state["input_manifest_ref"])

            result = build_graph(backend="memory", data_dir=str(run_dir)).invoke(
                state,
                config={"configurable": {"thread_id": "s1"}},
            )

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(result["current_stage"], "assembly_dedup")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(result["workflow_step_index"], 1)
            self.assertEqual(dedup["status"], "failed")
            self.assertEqual(dedup["reason"], "no_materialized_assembly")
            self.assertEqual(dedup["purge_dups_status"], "not_started")
            self.assertEqual(dedup["execution_mode"], "local")
            terminal_events = [
                row["event_type"]
                for row in Ledger(run_dir).read_event_rows("s1")
                if row.get("workflow_step_id") == "step-001-assembly_dedup"
                and row["event_type"]
                in {"stage_completed", "stage_failed", "stage_skipped"}
            ]
            self.assertEqual(terminal_events, ["stage_failed"])
            dispositions = [
                event
                for event in Ledger(run_dir).read_events("s1")
                if event["event_type"] == "method_disposition_recorded"
            ]
            self.assertEqual(len(dispositions), 1)
            self.assertEqual(dispositions[0]["disposition"], "not_started")
            self.assertEqual(dispositions[0]["reason"], "no_materialized_assembly")

    def test_nonlocal_dedup_without_materialized_assembly_fails_occurrence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = v2_run_dir(tmpdir, "s1")
            config = RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "assembly_dedup", "allow_local_execution": False}
                    ]
                }
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(config),
            )
            state["current_stage"] = "assembly_dedup"
            state["workflow_step_index"] = 1
            state["entered_workflow_step_index"] = 1
            self._publish_asset_registry(state, assets=[])
            Ledger(run_dir).create_session("s1", state["input_manifest_ref"])

            result = build_graph(backend="memory", data_dir=str(run_dir)).invoke(
                state,
                config={"configurable": {"thread_id": "s1"}},
            )

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(result["current_stage"], "assembly_dedup")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(result["workflow_step_index"], 1)
            self.assertEqual(dedup["status"], "failed")
            self.assertEqual(dedup["reason"], "no_materialized_assembly")
            self.assertEqual(dedup["purge_dups_status"], "not_started")
            self.assertNotIn("execution_mode", dedup)
            terminal_events = [
                row["event_type"]
                for row in Ledger(run_dir).read_event_rows("s1")
                if row.get("workflow_step_id") == "step-001-assembly_dedup"
                and row["event_type"]
                in {"stage_completed", "stage_failed", "stage_skipped"}
            ]
            self.assertEqual(terminal_events, ["stage_failed"])
            dispositions = [
                event
                for event in Ledger(run_dir).read_events("s1")
                if event["event_type"] == "method_disposition_recorded"
            ]
            self.assertEqual(len(dispositions), 1)
            self.assertEqual(dispositions[0]["disposition"], "not_started")
            self.assertEqual(dispositions[0]["reason"], "no_materialized_assembly")

    def test_invalid_candidate_handoff_fails_before_purge_dups_starts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            invalid_primary = Path(tmpdir) / "unbound.fasta"
            invalid_primary.write_text(">contig1\nACGT\n", encoding="utf-8")
            state["resource_refs"]["assembly"] = ResourceRef(
                resource_type="assembly",
                paths=[str(invalid_primary)],
                source_stage="assembly",
                workflow_step_id="step-001-assembly",
                metadata={},
            ).to_dict()
            ledger = Ledger(state["data_dir"])
            ledger.create_session("s1", state["input_manifest_ref"])

            result = build_graph(backend="memory", data_dir=state["data_dir"]).invoke(
                state,
                config={"configurable": {"thread_id": "s1"}},
            )

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertEqual(dedup["purge_dups_status"], "not_started")
            events = ledger.read_events("s1")
            disposition = next(
                event
                for event in events
                if event["event_type"] == "method_disposition_recorded"
            )
            self.assertEqual(disposition["disposition"], "not_started")
            self.assertFalse(
                any(
                    event["event_type"]
                    in {"attempt_started", "attempt_succeeded", "attempt_failed"}
                    for event in events
                )
            )

    def test_nonlocal_dedup_passes_through_materialized_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">contig1\nACGT\n", encoding="utf-8")
            run_dir = v2_run_dir(tmpdir, "s1")
            config = RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "assembly", "allow_local_execution": False},
                        {"stage": "assembly_dedup", "allow_local_execution": False},
                    ]
                }
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(config),
            )
            state["current_stage"] = "assembly_dedup"
            state["workflow_step_index"] = 2
            state["entered_workflow_step_index"] = 2
            self._publish_asset_registry(state, assets=[])
            Ledger(run_dir).create_session("s1", state["input_manifest_ref"])
            member_id = "s1:clean_long_reads:reads.fastq"
            raw_bundle_ref, raw_primary_ref = self._publish_candidate(
                state,
                candidate_id="s1:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft=draft,
                member_id=member_id,
            )
            raw_path = SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(
                raw_primary_ref["path_ref"]
            )
            self._set_assembly_resource(
                state,
                primary_path=raw_path,
                candidate_bundle_ref=raw_bundle_ref,
                primary_output_ref=raw_primary_ref,
                source_stage="assembly",
                workflow_step_id="step-001-assembly",
            )
            result = build_graph(backend="memory", data_dir=str(run_dir)).invoke(
                state,
                config={"configurable": {"thread_id": "s1"}},
            )

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(result["current_stage"], "done")
            self.assertEqual(dedup["status"], "completed")
            self.assertEqual(dedup["purge_dups_status"], "skipped")
            self.assertEqual(
                dedup["purge_dups_skip_reason"], "local_execution_not_enabled"
            )
            self.assertEqual(dedup["candidate_bundle_ref"], raw_bundle_ref)
            self.assertEqual(result["skip_reasons"], {})
            assembly = ResourceRef.from_dict(result["resource_refs"]["assembly"])
            self.assertEqual(assembly.source_stage, "assembly_dedup")
            self.assertEqual(assembly.workflow_step_id, "step-002-assembly_dedup")
            self.assertEqual(assembly.metadata["candidate_bundle_ref"], raw_bundle_ref)
            self.assertEqual(assembly.metadata["primary_output_ref"], raw_primary_ref)
            self.assertFalse(assembly.metadata["deduplicated"])
            terminal_events = [
                row["event_type"]
                for row in Ledger(run_dir).read_event_rows("s1")
                if row.get("workflow_step_id") == "step-002-assembly_dedup"
                and row["event_type"]
                in {"stage_completed", "stage_failed", "stage_skipped"}
            ]
            self.assertEqual(terminal_events, ["stage_completed"])
            events = Ledger(run_dir).read_events("s1")
            dispositions = [
                event
                for event in events
                if event["event_type"] == "method_disposition_recorded"
            ]
            self.assertEqual(len(dispositions), 1)
            self.assertEqual(dispositions[0]["disposition"], "skipped")
            self.assertEqual(dispositions[0]["reason"], "local_execution_not_enabled")
            deliveries = [
                event for event in events if event["event_type"] == "resource_delivered"
            ]
            self.assertEqual(len(deliveries), 1)
            self.assertEqual(deliveries[0]["delivery_mode"], "reused")

    def test_assembly_review_consumes_current_deduplicated_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">contig1\nACGT\n", encoding="utf-8")
            purged = Path(tmpdir) / "purged.fasta"
            purged.write_text(">contig1\nACGT\n", encoding="utf-8")
            state = self._state(tmpdir)
            member_id = "s1:clean_long_reads:reads.fastq"
            raw_bundle_ref, raw_primary_ref = self._publish_candidate(
                state,
                candidate_id="s1:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft=draft,
                member_id=member_id,
            )
            purged_bundle_ref, purged_primary_ref = self._publish_candidate(
                state,
                candidate_id="s1:assembly.candidate.flye:assembly_dedup.purge_dups",
                method_name="assembly_dedup.purge_dups",
                draft=purged,
                member_id=member_id,
            )
            purged_path = SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(
                purged_primary_ref["path_ref"]
            )
            state["current_stage"] = "assembly_review"
            state["workflow_step_index"] = 3
            state["entered_workflow_step_index"] = 3
            self._set_assembly_resource(
                state,
                primary_path=purged_path,
                candidate_bundle_ref=purged_bundle_ref,
                primary_output_ref=purged_primary_ref,
                source_stage="assembly_dedup",
                workflow_step_id="step-002-assembly_dedup",
                raw_candidate_bundle_ref=raw_bundle_ref,
                raw_primary_output_ref=raw_primary_ref,
            )
            ledger = Ledger(state["data_dir"])
            ledger.record_resource_delivered(
                state["session_id"],
                stage="assembly_dedup",
                workflow_step_id="step-002-assembly_dedup",
                resource_type="assembly",
                delivery_mode="materialized",
                candidate_bundle_ref=purged_bundle_ref,
                primary_output_ref=purged_primary_ref,
            )
            ledger.record_stage_completed(
                state["session_id"],
                "assembly_dedup",
                workflow_step_id="step-002-assembly_dedup",
            )

            update = resolve_candidates(state)
            result = {**state, **update}

            candidates = result["stage_outputs"]["assembly_review"]["candidates"]
            self.assertEqual(candidates[0]["source"], "assembly_dedup")
            self.assertEqual(candidates[0]["primary_output_ref"], purged_primary_ref)
            self.assertEqual(candidates[1]["source"], "assembly_dedup:raw")

    def test_default_auto_purge_dups_writes_purged_candidate_without_overwriting_raw(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">contig1\nACGT\n", encoding="utf-8")
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n####\n", encoding="utf-8")
            member_id = "s1:clean_long_reads:reads.fastq"
            state = self._state(tmpdir)
            self._publish_asset_registry(
                state,
                assets=[
                    {
                        "asset_id": member_id,
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                        "platform": "ont",
                        "format": "fastq",
                        "checksum": sha256_file(reads),
                        "checksum_status": "ready",
                        "size_bytes": reads.stat().st_size,
                    }
                ],
            )
            raw_bundle_ref, raw_primary_ref = self._publish_candidate(
                state,
                candidate_id="s1:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft=draft,
                member_id=member_id,
            )
            raw_path = SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(
                raw_primary_ref["path_ref"]
            )
            self._set_assembly_resource(
                state,
                primary_path=raw_path,
                candidate_bundle_ref=raw_bundle_ref,
                primary_output_ref=raw_primary_ref,
                source_stage="assembly",
                workflow_step_id="step-001-assembly",
            )
            state["resource_refs"]["long_reads"] = ResourceRef(
                resource_type="long_reads",
                paths=[str(reads)],
                source_stage="read_qc",
                workflow_step_id="step-001-read_qc",
                metadata={
                    "asset_registry_ref": state["asset_registry_ref"],
                    "asset_ids": [member_id],
                },
            ).to_dict()

            bin_dir = Path(tmpdir) / "bin"
            bin_dir.mkdir()
            minimap2 = bin_dir / "minimap2"
            minimap2.write_text("#!/bin/sh\nprintf 'alignment\\n'\n", encoding="utf-8")
            minimap2.chmod(0o755)
            pbcstat = bin_dir / "pbcstat"
            pbcstat.write_text(
                "#!/bin/sh\n"
                'case "$1" in *.paf.gz) ;; *) exit 9;; esac\n'
                'test -s "$1"\n'
                "printf 'stats\\n' > PB.stat\n"
                "printf 'cov\\n' > PB.base.cov\n",
                encoding="utf-8",
            )
            pbcstat.chmod(0o755)
            split_fa = bin_dir / "split_fa"
            split_fa.write_text('#!/bin/sh\n/bin/cat "$1"\n', encoding="utf-8")
            split_fa.chmod(0o755)
            calcuts = bin_dir / "calcuts"
            calcuts.write_text("#!/bin/sh\nprintf 'cutoffs\\n'\n", encoding="utf-8")
            calcuts.chmod(0o755)
            purge_dups = bin_dir / "purge_dups"
            purge_dups.write_text(
                "#!/bin/sh\nprintf 'contig1\\t0\\t0\\n'\n", encoding="utf-8"
            )
            purge_dups.chmod(0o755)
            get_seqs = bin_dir / "get_seqs"
            get_seqs.write_text(
                "#!/bin/sh\n"
                "prefix=''\n"
                "last=''\n"
                'while [ "$#" -gt 0 ]; do\n'
                '  if [ "$1" = -p ]; then shift; prefix="$1"; fi\n'
                '  last="$1"\n'
                "  shift\n"
                "done\n"
                '/bin/cat "$last" > "${prefix}.purged.fa"\n'
                ': > "${prefix}.hap.fa"\n',
                encoding="utf-8",
            )
            get_seqs.chmod(0o755)

            state["stage_outputs"] = {
                "assembly": {"status": "completed"},
                "assembly_dedup": {
                    "execution_mode": "local",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
                },
            }

            result = build_dedup_stage().invoke(state)

            dedup = result["stage_outputs"]["assembly_dedup"]
            self.assertEqual(
                result["current_stage"], "assembly_review", dedup.get("reason")
            )
            self.assertEqual(dedup["status"], "completed")
            self.assertEqual(dedup["candidate_id"], "s1:assembly.candidate.flye")
            self.assertTrue(Path(dedup["purged_assembly_ref"]).exists())
            self.assertEqual(
                Path(dedup["purged_assembly_ref"]).read_text(encoding="utf-8"),
                draft.read_text(encoding="utf-8"),
            )
            self.assertEqual(draft.read_text(encoding="utf-8"), ">contig1\nACGT\n")
            self.assertIsNotNone(dedup["purged_candidate_bundle_ref"])
            run_dir = Path(state["data_dir"])
            self.assertTrue(
                (
                    run_dir
                    / "artifacts"
                    / "steps"
                    / "step-002-assembly_dedup"
                    / "assembly_dedup"
                    / "reads_to_raw_assembly.paf.gz"
                ).is_file()
            )
            self.assertTrue(
                (
                    run_dir
                    / "artifacts"
                    / "steps"
                    / "step-002-assembly_dedup"
                    / "assembly_dedup"
                    / "raw_assembly.split.fasta"
                ).is_file()
            )
            self.assertEqual(dedup["cutoffs_strategy"], "calcuts_auto")
            self.assertTrue(
                any(ref.endswith("dups.bed") for ref in dedup["artifact_refs"])
            )
            self.assertFalse(any(".bam" in ref for ref in dedup["artifact_refs"]))
            paths = SessionPaths.from_run_dir(state["data_dir"])
            script = paths.workflow_step_work_path(
                "step-002-assembly_dedup",
                "assembly_dedup",
                "run_purge_dups.sh",
            ).read_text(encoding="utf-8")
            self.assertNotIn("samtools", script)
            self.assertNotIn(".bam", script)
            self.assertEqual(script.count("-x map-ont"), 1)
            events = Ledger(run_dir).read_events("s1")
            event_types = [event["event_type"] for event in events]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_succeeded", event_types)
            dispositions = [
                event
                for event in events
                if event["event_type"] == "method_disposition_recorded"
            ]
            self.assertEqual(len(dispositions), 1)
            self.assertEqual(dispositions[0]["disposition"], "completed")
            self.assertEqual(
                result["workflow_step_outcome"]["deliveries"][0]["delivery_mode"],
                "materialized",
            )


if __name__ == "__main__":
    unittest.main()
