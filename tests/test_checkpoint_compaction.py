from __future__ import annotations

import tempfile
import unittest

from langgraph.graph import StateGraph

from app.control.lifecycle import inspect_session
from app.graph.checkpoint_compaction import (
    TerminalCheckpointCompactionError,
    compact_terminal_checkpoint,
)
from app.graph.checkpointer import get_checkpointer
from app.integrity import sha256_file
from app.reports.trace_writer import build_session_trace
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState, initial_state
from app.storage.artifacts import register_artifact_records
from app.storage.audit import verify_checkpoint_free_artifact_retention
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths, ensure_run_layout


class TerminalCheckpointCompactionTests(unittest.TestCase):
    def _terminal_graph(self, paths: SessionPaths):
        saver = get_checkpointer("sqlite", data_dir=paths.run_dir)
        graph = StateGraph(TopLevelState)
        graph.add_node(
            "finish",
            lambda state: {
                "current_stage": "done",
                "workflow_step_index": len(state["workflow_steps"]),
                "entered_workflow_step_index": len(state["workflow_steps"]) - 1,
                "supervision_state": "completed",
            },
        )
        graph.set_entry_point("finish")
        return graph.compile(checkpointer=saver), saver

    @staticmethod
    def _workflow_steps(*requested_stages: str) -> list[dict[str, object]]:
        stages = ["inventory", *requested_stages]
        if (
            any(stage in {"assembly", "t2t"} for stage in requested_stages)
            and stages[-1] != "assembly_review"
        ):
            stages.append("assembly_review")
        steps = [
            {
                "step_id": f"step-{index:03d}-{stage}",
                "stage": stage,
                "allow_local_execution": False,
            }
            for index, stage in enumerate(stages)
        ]
        producer_step_id = steps[0]["step_id"]
        for step in steps:
            if step["stage"] == "assembly_review":
                step["review_target"] = {
                    "resource_type": "assembly",
                    "producer_step_id": producer_step_id,
                }
            if step["stage"] in {
                "inventory",
                "assembly",
                "assembly_dedup",
                "assembly_review",
                "hic_scaffolding",
                "t2t",
            }:
                producer_step_id = step["step_id"]
        return steps

    def _state(self, paths: SessionPaths, *requested_stages: str) -> dict[str, object]:
        return initial_state(
            session_id=paths.session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(paths.run_dir),
            workflow_steps=self._workflow_steps(*requested_stages),
            stage_plan_ref="control/run_config.json",
        )

    @staticmethod
    def _step_id(index: int, stage: str) -> str:
        return f"step-{index:03d}-{stage}"

    def test_compacts_verified_terminal_sqlite_history_to_one_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            artifact_path = paths.workflow_step_artifact_path(
                self._step_id(1, "assembly"), "assembly", "result.json"
            )
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text('{"ok": true}\n', encoding="utf-8")
            [artifact_ref] = register_artifact_records(
                paths.run_dir,
                paths.session_id,
                [
                    ArtifactMeta.from_file(
                        artifact_path,
                        stage="assembly",
                        method_name="test",
                        artifact_type="assembly.result",
                    )
                ],
            )
            ledger = Ledger(paths.run_dir)
            ledger.create_session(paths.session_id, "control/manifest.yaml")
            ledger.record_session_terminal(
                paths.session_id,
                outcome="completed",
                stage="done",
                refs=[artifact_ref.model_dump(mode="json")],
            )
            graph, saver = self._terminal_graph(paths)
            config = {"configurable": {"thread_id": paths.session_id}}
            state = self._state(paths, "assembly")
            state["stage_outputs"] = {
                "assembly": {"result_ref": artifact_ref.model_dump(mode="json")}
            }
            result = graph.invoke(state, config=config)
            saver.conn.execute(
                "INSERT INTO checkpoints "
                "SELECT thread_id, ?, checkpoint_id || '-subgraph', "
                "parent_checkpoint_id, type, checkpoint, metadata "
                "FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' LIMIT 1",
                ("assembly_stage:subgraph", paths.session_id),
            )
            saver.conn.commit()
            self.assertGreater(len(list(saver.list(config))), 1)

            compacted = compact_terminal_checkpoint(saver, config, result)

            self.assertTrue(compacted)
            rows = list(saver.list(config))
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0].parent_config)
            self.assertEqual(
                saver.conn.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
                    (paths.session_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                graph.get_state(config).values["supervision_state"],
                "completed",
            )
            saver.conn.close()

    def test_compaction_round_trips_frozen_occurrence_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            ledger = Ledger(paths.run_dir)
            ledger.create_session(paths.session_id, "control/manifest.yaml")
            ledger.record_session_terminal(
                paths.session_id,
                outcome="completed",
                stage="done",
            )
            graph, saver = self._terminal_graph(paths)
            config = {"configurable": {"thread_id": paths.session_id}}
            state = self._state(paths, "read_qc", "read_qc")
            state["stage_seeds"] = {"read_qc": {"frozen_setting": "keep"}}

            terminal_state = graph.invoke(state, config=config)

            self.assertTrue(compact_terminal_checkpoint(saver, config, terminal_state))
            restored = dict(graph.get_state(config).values)

            self.assertEqual(
                restored["workflow_steps"],
                state["workflow_steps"],
            )
            self.assertEqual(
                restored["workflow_step_index"],
                len(state["workflow_steps"]),
            )
            self.assertEqual(
                restored["entered_workflow_step_index"],
                len(state["workflow_steps"]) - 1,
            )
            self.assertEqual(restored["stage_plan_ref"], "control/run_config.json")
            self.assertEqual(restored["stage_seeds"], state["stage_seeds"])
            saver.conn.close()

    def test_refuses_active_or_blocked_state_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            ledger = Ledger(paths.run_dir)
            ledger.create_session(paths.session_id, "control/manifest.yaml")
            graph, saver = self._terminal_graph(paths)
            config = {"configurable": {"thread_id": paths.session_id}}
            state = self._state(paths, "assembly")
            graph.invoke(state, config=config)
            before = len(list(saver.list(config)))
            blocked = {**state, "supervision_state": "blocked_human"}

            with self.assertRaisesRegex(TerminalCheckpointCompactionError, "terminal"):
                compact_terminal_checkpoint(saver, config, blocked)

            self.assertEqual(len(list(saver.list(config))), before)
            saver.conn.close()

    def test_refuses_compaction_when_checkpoint_artifact_is_not_durably_rooted(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            artifact_path = paths.workflow_step_artifact_path(
                self._step_id(1, "assembly"), "assembly", "orphan.json"
            )
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text('{"orphan": true}\n', encoding="utf-8")
            [artifact_ref] = register_artifact_records(
                paths.run_dir,
                paths.session_id,
                [
                    ArtifactMeta.from_file(
                        artifact_path,
                        stage="assembly",
                        method_name="test",
                        artifact_type="assembly.model_response",
                    )
                ],
            )
            ledger = Ledger(paths.run_dir)
            ledger.create_session(paths.session_id, "control/manifest.yaml")
            ledger.record_session_terminal(
                paths.session_id,
                outcome="completed",
                stage="done",
            )
            graph, saver = self._terminal_graph(paths)
            config = {"configurable": {"thread_id": paths.session_id}}
            state = self._state(paths, "assembly")
            state["stage_outputs"] = {
                "assembly": {"orphan_ref": artifact_ref.model_dump(mode="json")}
            }
            result = graph.invoke(state, config=config)
            before = len(list(saver.list(config)))

            with self.assertRaisesRegex(TerminalCheckpointCompactionError, "reachable"):
                compact_terminal_checkpoint(saver, config, result)

            self.assertEqual(len(list(saver.list(config))), before)
            saver.conn.close()

    def test_refuses_terminal_state_without_committed_terminal_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            Ledger(paths.run_dir).create_session(
                paths.session_id,
                "control/manifest.yaml",
            )
            graph, saver = self._terminal_graph(paths)
            config = {"configurable": {"thread_id": paths.session_id}}
            state = self._state(paths, "assembly")
            result = graph.invoke(state, config=config)
            before = len(list(saver.list(config)))

            with self.assertRaisesRegex(
                TerminalCheckpointCompactionError,
                "terminal event",
            ):
                compact_terminal_checkpoint(saver, config, result)

            self.assertEqual(len(list(saver.list(config))), before)
            saver.conn.close()

    def test_checkpoint_free_terminal_trace_reconstructs_route_review_failure_and_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            paths.manifest_path.write_text("session_id: s1\n", encoding="utf-8")

            artifact_specs = (
                (
                    "inventory.asset_registry",
                    "step-000-inventory",
                    "inventory",
                    "inventory",
                    "registry.json",
                    '{"assets": []}\n',
                ),
                (
                    "assembly.failure",
                    "step-002-assembly",
                    "assembly",
                    "assembler",
                    "failure.json",
                    '{"error": "assembler failed"}\n',
                ),
                (
                    "assembly_review.request",
                    "step-003-assembly_review",
                    "assembly_review",
                    "review",
                    "request.json",
                    '{"review_request_id": "review_001"}\n',
                ),
                (
                    "assembly_review.decision",
                    "step-003-assembly_review",
                    "assembly_review",
                    "review",
                    "decision.json",
                    '{"decision": "accept"}\n',
                ),
            )
            metas = []
            for artifact_type, step_id, stage, method, name, body in artifact_specs:
                path = paths.workflow_step_artifact_path(step_id, stage, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
                metadata = {"workflow_step_id": step_id}
                if artifact_type in {
                    "assembly_review.request",
                    "assembly_review.decision",
                }:
                    metadata["review_request_id"] = "review_001"
                metas.append(
                    ArtifactMeta.from_file(
                        path,
                        stage=stage,
                        method_name=method,
                        artifact_type=artifact_type,
                        metadata=metadata,
                    )
                )
            refs = {
                ref.artifact_type: ref
                for ref in register_artifact_records(
                    paths.run_dir,
                    paths.session_id,
                    metas,
                )
            }
            inventory_ref = refs["inventory.asset_registry"]
            failure_ref = refs["assembly.failure"]
            request_ref = refs["assembly_review.request"]
            decision_ref = refs["assembly_review.decision"]

            ledger = Ledger(paths.run_dir)
            ledger.create_session(paths.session_id, paths.to_ref(paths.manifest_path))
            ledger.record_stage_entered(
                paths.session_id,
                "inventory",
                workflow_step_id="step-000-inventory",
                route_node="inventory_stage",
                idempotency_key="stage-entered:inventory",
            )
            ledger.record_stage_completed(
                paths.session_id,
                "inventory",
                workflow_step_id="step-000-inventory",
                refs=[inventory_ref],
                idempotency_key="stage-completed:inventory",
            )
            ledger.record_stage_entered(
                paths.session_id,
                "genome_survey",
                workflow_step_id="step-001-genome_survey",
                route_node="genome_survey_stage",
                idempotency_key="stage-entered:genome_survey",
            )
            ledger.record_stage_skipped(
                paths.session_id,
                "genome_survey",
                workflow_step_id="step-001-genome_survey",
                reason="not_requested",
                idempotency_key="stage-skipped:genome_survey",
            )
            ledger.record_review_requested(
                paths.session_id,
                "assembly_review",
                reason="operator_required",
                review_request_artifact_ref=request_ref,
                review_request_id="review_001",
            )
            ledger.record_review_decision(
                paths.session_id,
                "assembly_review",
                decision="accept",
                decided_by="human",
                artifact_ref=decision_ref,
                review_request_id="review_001",
            )
            ledger.record_stage_entered(
                paths.session_id,
                "assembly",
                workflow_step_id="step-002-assembly",
                route_node="assembly_stage",
                idempotency_key="stage-entered:assembly",
            )
            script_path = paths.workflow_step_work_path(
                "step-002-assembly", "assembly", "run.sh"
            )
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
            attempt = FrozenAttemptSpec(
                attempt_id="assembly:attempt_000",
                session_id=paths.session_id,
                stage="assembly",
                method_name="assembly.candidate.test",
                script_path=script_path,
                script_sha256=sha256_file(script_path),
                working_dir=paths.workflow_step_work_dir(
                    "step-002-assembly", "assembly"
                ),
                expected_outputs=[
                    paths.workflow_step_work_path(
                        "step-002-assembly", "assembly", "draft.fasta"
                    )
                ],
                input_dependencies=[],
                metadata={"workflow_step_id": "step-002-assembly"},
            )
            ledger.record_attempt_failed(
                paths.session_id,
                attempt,
                return_code=2,
                error="assembler failed",
                artifacts=[failure_ref.model_dump(mode="json")],
            )
            ledger.record_stage_failed(
                paths.session_id,
                "assembly",
                workflow_step_id="step-002-assembly",
                error="assembler failed",
                refs=[failure_ref],
                idempotency_key="stage-failed:assembly",
            )
            all_refs = [inventory_ref, failure_ref, request_ref, decision_ref]
            ledger.record_session_terminal(
                paths.session_id,
                outcome="failed",
                stage="assembly",
                error="assembler failed",
                refs=all_refs,
            )

            rows_by_type = {
                row["event_type"]: row
                for row in ledger.read_event_rows(paths.session_id)
            }
            self.assertEqual(
                rows_by_type["stage_completed"]["refs"][0]["artifact_id"],
                inventory_ref.artifact_id,
            )
            self.assertEqual(
                rows_by_type["review_decision"]["refs"][0]["artifact_id"],
                decision_ref.artifact_id,
            )
            self.assertEqual(
                rows_by_type["attempt_failed"]["refs"][0]["artifact_id"],
                failure_ref.artifact_id,
            )
            self.assertEqual(
                {
                    ref["artifact_id"]
                    for ref in rows_by_type["session_terminal"]["refs"]
                },
                {ref.artifact_id for ref in all_refs},
            )

            graph, saver = self._terminal_graph(paths)
            config = {"configurable": {"thread_id": paths.session_id}}
            state = self._state(paths, "genome_survey", "assembly", "assembly_review")
            state["stage_outputs"] = {
                "inventory": {
                    "registry_ref": inventory_ref.model_dump(mode="json"),
                },
                "assembly": {
                    "failure_ref": failure_ref.model_dump(mode="json"),
                },
                "assembly_review": {
                    "request_ref": request_ref.model_dump(mode="json"),
                    "decision_ref": decision_ref.model_dump(mode="json"),
                },
            }
            graph.invoke(state, config=config)
            graph.update_state(
                config,
                {
                    "current_stage": "assembly",
                    "workflow_step_index": 2,
                    "entered_workflow_step_index": 2,
                    "supervision_state": "failed_terminal",
                    "last_error": "assembler failed",
                    "active_interrupt": None,
                },
            )
            terminal_state = dict(graph.get_state(config).values)
            self.assertGreater(len(list(saver.list(config))), 1)

            self.assertTrue(compact_terminal_checkpoint(saver, config, terminal_state))
            self.assertEqual(len(list(saver.list(config))), 1)

            saver.conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ?",
                (paths.session_id,),
            )
            saver.conn.commit()
            self.assertEqual(list(saver.list(config)), [])

            summary = inspect_session(paths.session_id, paths.run_dir)
            trace = build_session_trace(paths.run_dir, paths.session_id)
            retention = verify_checkpoint_free_artifact_retention(
                paths.run_dir,
                terminal_state,
            )

            self.assertEqual(summary["supervision_state"], "failed_terminal")
            self.assertEqual(summary["current_stage"], "assembly")
            self.assertEqual(summary["last_error"], "assembler failed")
            self.assertEqual(trace["supervision_state"], "failed_terminal")
            self.assertEqual(
                trace["final_or_current_stage"],
                {"stage": "assembly", "source": "ledger"},
            )
            self.assertEqual(
                [(item["event_type"], item["stage"]) for item in trace["route"]],
                [
                    ("stage_entered", "inventory"),
                    ("stage_completed", "inventory"),
                    ("stage_entered", "genome_survey"),
                    ("stage_skipped", "genome_survey"),
                    ("stage_entered", "assembly"),
                ],
            )
            skipped = next(
                stage for stage in trace["stages"] if stage["stage"] == "genome_survey"
            )
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(skipped["lifecycle"]["skip_reason"], "not_requested")
            self.assertEqual(trace["decisions"][0]["review_request_id"], "review_001")
            self.assertEqual(trace["decisions"][0]["decision"], "accept")
            self.assertEqual(
                trace["decisions"][0]["decision_artifact_ref"],
                decision_ref.path_ref,
            )
            self.assertEqual(trace["failures"][0]["stage"], "assembly")
            self.assertEqual(trace["failures"][0]["error"], "assembler failed")
            self.assertEqual(retention["missing_artifact_ids"], [])
            self.assertEqual(
                set(retention["required_artifact_ids"]),
                {ref.artifact_id for ref in all_refs},
            )
            saver.conn.close()


if __name__ == "__main__":
    unittest.main()
