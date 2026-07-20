from __future__ import annotations

import tempfile
import unittest

from langgraph.graph import StateGraph

from app.graph.checkpoint_compaction import (
    TerminalCheckpointCompactionError,
    compact_terminal_checkpoint,
)
from app.graph.checkpointer import get_checkpointer
from app.schemas.artifacts import ArtifactMeta
from app.schemas.state import TopLevelState, initial_state
from app.storage.artifacts import register_artifact_records
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths, ensure_run_layout


class TerminalCheckpointCompactionTests(unittest.TestCase):
    def _terminal_graph(self, paths: SessionPaths):
        saver = get_checkpointer("sqlite", data_dir=paths.run_dir)
        graph = StateGraph(TopLevelState)
        graph.add_node(
            "finish",
            lambda _state: {
                "current_stage": "done",
                "supervision_state": "completed",
            },
        )
        graph.set_entry_point("finish")
        return graph.compile(checkpointer=saver), saver

    def test_compacts_verified_terminal_sqlite_history_to_one_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            artifact_path = paths.artifact_path("assembly", "result.json")
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
            state = initial_state(
                session_id=paths.session_id,
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(paths.run_dir),
            )
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

    def test_refuses_active_or_blocked_state_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = SessionPaths(tmp, "s1")
            ensure_run_layout(paths)
            ledger = Ledger(paths.run_dir)
            ledger.create_session(paths.session_id, "control/manifest.yaml")
            graph, saver = self._terminal_graph(paths)
            config = {"configurable": {"thread_id": paths.session_id}}
            state = initial_state(
                session_id=paths.session_id,
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(paths.run_dir),
            )
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
            artifact_path = paths.artifact_path("assembly", "orphan.json")
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
            state = initial_state(
                session_id=paths.session_id,
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(paths.run_dir),
            )
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
            state = initial_state(
                session_id=paths.session_id,
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(paths.run_dir),
            )
            result = graph.invoke(state, config=config)
            before = len(list(saver.list(config)))

            with self.assertRaisesRegex(
                TerminalCheckpointCompactionError,
                "terminal event",
            ):
                compact_terminal_checkpoint(saver, config, result)

            self.assertEqual(len(list(saver.list(config))), before)
            saver.conn.close()


if __name__ == "__main__":
    unittest.main()
