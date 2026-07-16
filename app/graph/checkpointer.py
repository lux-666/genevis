"""Checkpointer factory.

Phase 5/02 introduces a durable sqlite checkpointer so a server/HPC run can
stop at ``blocked_human``, exit the process, and later resume from a
decision file. The in-memory backend is preserved for tests and lightweight
runs that do not need cross-process resume.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from app.storage.paths import SessionPaths

CheckpointerBackend = Literal["sqlite", "memory"]
DEFAULT_CHECKPOINTER_BACKEND: CheckpointerBackend = "sqlite"


def checkpoint_db_path(data_dir: str | Path) -> Path:
    """Return the sqlite checkpoint file path under ``data_dir``."""
    return SessionPaths.from_run_dir(data_dir).checkpoint_db_path


def get_checkpointer(
    backend: CheckpointerBackend = DEFAULT_CHECKPOINTER_BACKEND,
    *,
    data_dir: str | Path = "data",
):
    """Return a LangGraph checkpointer.

    ``sqlite`` persists checkpoints under ``data_dir/checkpoints/`` so the
    parent graph can resume across processes. ``memory`` is only suitable
    for in-process tests.

    Lifecycle: when ``backend == "sqlite"`` the returned ``SqliteSaver``
    owns a ``sqlite3.Connection`` reachable as ``saver.conn``. CLI
    callers are short-lived processes and do not need to close it
    explicitly; long-lived callers (e.g. tests that build the graph
    repeatedly in one process) MUST close ``saver.conn`` themselves to
    avoid file-descriptor leaks. The factory does not return a context
    manager so callers can pass the saver into ``StateGraph.compile``
    without owning it.
    """
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    if backend == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        db_path = checkpoint_db_path(data_dir)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return saver
    raise ValueError(f"Unsupported checkpointer backend: {backend}")
