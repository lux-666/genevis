"""Tests for the LangGraph checkpointer factory."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.graph.checkpointer import (
    DEFAULT_CHECKPOINTER_BACKEND,
    checkpoint_db_path,
    get_checkpointer,
)


class CheckpointerFactoryTests(unittest.TestCase):
    def test_default_backend_is_sqlite(self):
        self.assertEqual(DEFAULT_CHECKPOINTER_BACKEND, "sqlite")

    def test_sqlite_backend_creates_db_under_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            saver = get_checkpointer("sqlite", data_dir=tmp)
            try:
                self.assertTrue(checkpoint_db_path(tmp).exists())
                from langgraph.checkpoint.sqlite import SqliteSaver
                self.assertIsInstance(saver, SqliteSaver)
            finally:
                saver.conn.close()

    def test_memory_backend_returns_in_memory_saver(self):
        from langgraph.checkpoint.memory import MemorySaver

        saver = get_checkpointer("memory")
        self.assertIsInstance(saver, MemorySaver)

    def test_unknown_backend_raises(self):
        with self.assertRaises(ValueError):
            get_checkpointer("postgres")  # type: ignore[arg-type]

    def test_checkpoint_db_path_is_under_data_dir(self):
        path = checkpoint_db_path("/tmp/example")
        self.assertEqual(path, Path("/tmp/example/checkpoints/checkpoints.sqlite"))


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
        from langgraph.graph import StateGraph
        from typing import TypedDict

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
