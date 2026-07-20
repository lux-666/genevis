"""Checkpointer factory.

Phase 5/02 introduces a durable sqlite checkpointer so a server/HPC run can
stop at ``blocked_human``, exit the process, and later resume from a
decision file. The in-memory backend is preserved for tests and lightweight
runs that do not need cross-process resume.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.schemas.state import (
    TopLevelState,
    validate_state_field,
    validate_top_level_state,
)
from app.storage.paths import SessionPaths

CheckpointerBackend = Literal["sqlite", "memory"]
DEFAULT_CHECKPOINTER_BACKEND: CheckpointerBackend = "sqlite"


class StateBudgetCheckpointer(BaseCheckpointSaver):
    """Delegate saver operations through the one whole-state write boundary."""

    def __init__(self, inner: BaseCheckpointSaver) -> None:
        self.inner = inner

    def __getattribute__(self, name: str) -> Any:
        """Preserve the inner saver's complete BaseCheckpointSaver behavior."""
        if (
            name == "inner"
            or name.startswith("_")
            or name in StateBudgetCheckpointer.__dict__
        ):
            return object.__getattribute__(self, name)
        return getattr(object.__getattribute__(self, "inner"), name)

    def put(self, config, checkpoint, metadata, new_versions):
        _validate_checkpoint_state(checkpoint)
        return self.inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        _validate_pending_writes(writes)
        self.inner.put_writes(config, writes, task_id, task_path)

    async def aput(self, config, checkpoint, metadata, new_versions):
        _validate_checkpoint_state(checkpoint)
        return await self.inner.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        _validate_pending_writes(writes)
        await self.inner.aput_writes(config, writes, task_id, task_path)

    def with_allowlist(
        self, extra_allowlist: Collection[tuple[str, ...]]
    ) -> StateBudgetCheckpointer:
        """Keep strict-msgpack clones behind the same write boundary."""
        inner = self.inner.with_allowlist(extra_allowlist)
        return self if inner is self.inner else bound_checkpointer(inner)


def bound_checkpointer(checkpointer: BaseCheckpointSaver) -> StateBudgetCheckpointer:
    """Wrap any built-in or externally injected saver exactly once."""

    if isinstance(checkpointer, StateBudgetCheckpointer):
        return checkpointer
    return StateBudgetCheckpointer(checkpointer)


def _validate_checkpoint_state(checkpoint: Any) -> None:
    if not isinstance(checkpoint, dict):
        raise TypeError("checkpoint must be a JSON object")
    channel_values = checkpoint.get("channel_values")
    if not isinstance(channel_values, dict):
        raise TypeError("checkpoint.channel_values must be a JSON object")
    start_value = channel_values.get("__start__")
    if start_value is not None:
        if not isinstance(start_value, dict):
            raise TypeError(
                "checkpoint __start__ value must be a TopLevelState object"
            )
        validate_top_level_state(start_value)
        return
    state = {
        key: value
        for key, value in channel_values.items()
        if key in TopLevelState.__annotations__
    }
    if state:
        validate_top_level_state(state)


def _validate_pending_writes(writes: Sequence[tuple[str, Any]]) -> None:
    for channel, value in writes:
        if channel == "__start__":
            if not isinstance(value, dict):
                raise TypeError(
                    "pending __start__ write must be a TopLevelState object"
                )
            validate_top_level_state(value)
        elif channel in TopLevelState.__annotations__:
            validate_state_field(channel, value)


def checkpoint_db_path(data_dir: str | Path) -> Path:
    """Return the sqlite checkpoint file path under ``data_dir``."""
    return SessionPaths.from_run_dir(data_dir).checkpoint_db_path


def get_checkpointer(
    backend: CheckpointerBackend = DEFAULT_CHECKPOINTER_BACKEND,
    *,
    data_dir: str | Path = "data",
) -> StateBudgetCheckpointer:
    """Return a LangGraph checkpointer.

    ``sqlite`` persists checkpoints under ``data_dir/checkpoints/`` so the
    parent graph can resume across processes. ``memory`` is only suitable
    for in-process tests.

    Lifecycle: when ``backend == "sqlite"`` the wrapped ``SqliteSaver`` owns
    a ``sqlite3.Connection`` reachable as ``saver.conn``. CLI
    callers are short-lived processes and do not need to close it
    explicitly; long-lived callers (e.g. tests that build the graph
    repeatedly in one process) MUST close ``saver.conn`` themselves to
    avoid file-descriptor leaks. The factory does not return a context
    manager so callers can pass the saver into ``StateGraph.compile``
    without owning it.
    """
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return bound_checkpointer(MemorySaver())
    if backend == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        db_path = checkpoint_db_path(data_dir)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return bound_checkpointer(saver)
    raise ValueError(f"Unsupported checkpointer backend: {backend}")
