"""Tests for the LangGraph checkpointer factory."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.graph.builder import build_graph
from app.graph.checkpointer import (
    DEFAULT_CHECKPOINTER_BACKEND,
    StateBudgetCheckpointer,
    bound_checkpointer,
    checkpoint_db_path,
    get_checkpointer,
)
from tests.data_v2 import v2_run_dir


class CheckpointerFactoryTests(unittest.TestCase):
    def test_default_backend_is_sqlite(self):
        self.assertEqual(DEFAULT_CHECKPOINTER_BACKEND, "sqlite")

    def test_sqlite_backend_creates_db_under_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            saver = get_checkpointer("sqlite", data_dir=run_dir)
            try:
                self.assertTrue(checkpoint_db_path(run_dir).exists())
                from langgraph.checkpoint.sqlite import SqliteSaver
                self.assertIsInstance(saver, StateBudgetCheckpointer)
                self.assertIsInstance(saver.inner, SqliteSaver)
            finally:
                saver.conn.close()

    def test_memory_backend_returns_in_memory_saver(self):
        from langgraph.checkpoint.memory import MemorySaver

        saver = get_checkpointer("memory")
        self.assertIsInstance(saver, StateBudgetCheckpointer)
        self.assertIsInstance(saver.inner, MemorySaver)

    def test_external_saver_is_wrapped_by_graph_builder(self):
        from langgraph.checkpoint.memory import MemorySaver

        external = MemorySaver()
        compiled = build_graph(checkpointer=external)
        self.assertIsInstance(compiled.checkpointer, StateBudgetCheckpointer)
        self.assertIs(compiled.checkpointer.inner, external)

    def test_bound_checkpointer_is_idempotent(self):
        from langgraph.checkpoint.memory import MemorySaver

        saver = bound_checkpointer(MemorySaver())
        self.assertIs(bound_checkpointer(saver), saver)

    def test_read_list_management_and_version_apis_delegate_to_inner(self):
        from langgraph.checkpoint.base import BaseCheckpointSaver
        from langgraph.checkpoint.memory import MemorySaver

        saver = bound_checkpointer(MemorySaver())
        delegated_names = (
            name
            for name in BaseCheckpointSaver.__dict__
            if not name.startswith("_")
            and name not in StateBudgetCheckpointer.__dict__
        )
        for name in delegated_names:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(saver, name),
                    getattr(saver.inner, name),
                )

    def test_allowlist_clone_remains_single_wrapped(self):
        from langgraph.checkpoint.memory import MemorySaver

        saver = bound_checkpointer(MemorySaver())
        strict_saver = saver.with_allowlist({("tests.test_checkpointer", "_State")})
        self.assertIsInstance(strict_saver, StateBudgetCheckpointer)
        self.assertNotIsInstance(strict_saver.inner, StateBudgetCheckpointer)

    def test_put_rejects_unbounded_complete_state_before_delegate(self):
        from langgraph.checkpoint.memory import MemorySaver

        from app.schemas.state import initial_state

        saver = bound_checkpointer(MemorySaver())
        config = {"configurable": {"thread_id": "s1", "checkpoint_ns": ""}}
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        state["shared_parameters"] = {"padding": "x" * (256 * 1024)}
        checkpoint = {"channel_values": state}
        with self.assertRaisesRegex(ValueError, "hard limit"):
            saver.put(config, checkpoint, {}, {})

    def test_put_writes_rejects_unbounded_state_delta_before_delegate(self):
        from langgraph.checkpoint.memory import MemorySaver

        saver = bound_checkpointer(MemorySaver())
        config = {"configurable": {"thread_id": "s1", "checkpoint_ns": ""}}
        with self.assertRaisesRegex(ValueError, "active_interrupt"):
            saver.put_writes(
                config,
                [("active_interrupt", {"stage": "assembly", "reason": "x" * 4096})],
                "task-1",
            )

    def test_memory_saver_rejects_oversized_complete_state(self):
        from langgraph.graph import StateGraph

        from app.schemas.state import TopLevelState, initial_state

        saver = get_checkpointer("memory")
        graph = StateGraph(TopLevelState)
        graph.add_node("noop", lambda _state: {})
        graph.set_entry_point("noop")
        compiled = graph.compile(checkpointer=saver)
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        state["shared_parameters"] = {"padding": "x" * (256 * 1024)}

        with self.assertRaisesRegex(ValueError, "hard limit"):
            compiled.invoke(
                state,
                config={"configurable": {"thread_id": "s1"}},
            )

    def test_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            get_checkpointer("postgres")  # type: ignore[arg-type]

    def test_checkpoint_db_path_is_under_data_dir(self):
        run_dir = Path("/tmp/example/runs/s1").resolve()
        path = checkpoint_db_path(run_dir)
        self.assertEqual(path, run_dir / "checkpoints" / "checkpoints.sqlite")


class CheckpointerAsyncBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_aput_rejects_unbounded_complete_state_before_delegate(self):
        from langgraph.checkpoint.memory import MemorySaver

        from app.schemas.state import initial_state

        saver = bound_checkpointer(MemorySaver())
        config = {"configurable": {"thread_id": "s1", "checkpoint_ns": ""}}
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        state["shared_parameters"] = {"padding": "x" * (256 * 1024)}
        checkpoint = {"channel_values": state}
        with self.assertRaisesRegex(ValueError, "hard limit"):
            await saver.aput(config, checkpoint, {}, {})

    async def test_aput_writes_rejects_unbounded_delta_before_delegate(self):
        from langgraph.checkpoint.memory import MemorySaver

        saver = bound_checkpointer(MemorySaver())
        config = {"configurable": {"thread_id": "s1", "checkpoint_ns": ""}}
        with self.assertRaisesRegex(ValueError, "active_interrupt"):
            await saver.aput_writes(
                config,
                [("active_interrupt", {"stage": "assembly", "reason": "x" * 4096})],
                "task-1",
            )

    async def test_async_graph_execution_uses_wrapped_memory_saver(self):
        from langgraph.graph import StateGraph

        from app.schemas.state import TopLevelState, initial_state

        saver = get_checkpointer("memory")
        graph = StateGraph(TopLevelState)
        graph.add_node("noop", lambda _state: {})
        graph.set_entry_point("noop")
        compiled = graph.compile(checkpointer=saver)
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        result = await compiled.ainvoke(
            state,
            config={"configurable": {"thread_id": "s1"}},
        )

        self.assertEqual(result["session_id"], "s1")
        snapshot = await compiled.aget_state(
            {"configurable": {"thread_id": "s1"}}
        )
        self.assertEqual(snapshot.values["session_id"], "s1")


class SqliteSaverApiContractTests(unittest.TestCase):
    """Pin the LangGraph SqliteSaver API surface this project relies on.

    These assertions are deliberately brittle: if a langgraph
    dependency upgrade changes any of them the test fails fast in CI
    instead of failing at resume time on the server. Update the
    pyproject.toml pin and this test together.
    """

    def test_sqlite_saver_has_required_interface(self):
        import inspect as _inspect
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        # Constructor takes a sqlite3.Connection.
        sig = _inspect.signature(SqliteSaver.__init__)
        self.assertIn("conn", sig.parameters)

        # setup() and the `.conn` attribute are part of the API.
        self.assertTrue(hasattr(SqliteSaver, "setup"))
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(str(Path(tmp) / "x.sqlite"), check_same_thread=False)
            try:
                saver = SqliteSaver(conn)
                saver.setup()
                self.assertIs(saver.conn, conn)
            finally:
                conn.close()

    def test_compiled_graph_exposes_get_state_and_update_state(self):
        # We rely on these on every resume call.
        from typing import TypedDict

        from langgraph.graph import StateGraph

        class _S(TypedDict, total=False):
            n: int

        graph = StateGraph(_S)
        graph.add_node("noop", lambda s: s)
        graph.set_entry_point("noop")
        compiled = graph.compile()
        self.assertTrue(hasattr(compiled, "get_state"))
        self.assertTrue(hasattr(compiled, "update_state"))
        self.assertTrue(hasattr(compiled, "invoke"))


if __name__ == "__main__":
    unittest.main()
