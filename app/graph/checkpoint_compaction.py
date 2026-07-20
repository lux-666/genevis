"""Verified terminal-only compaction for the durable SQLite checkpoint chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from app.control.lifecycle import inspect_session
from app.graph.checkpointer import StateBudgetCheckpointer
from app.schemas.state import TopLevelState, validate_top_level_state
from app.storage.audit import verify_checkpoint_free_artifact_retention


class TerminalCheckpointCompactionError(RuntimeError):
    """Terminal compaction could not prove that deleting history is safe."""


def compact_terminal_checkpoint(
    checkpointer: Any,
    config: Mapping[str, Any],
    terminal_state: Mapping[str, Any],
) -> bool:
    """Compact one verified SQLite thread to its standalone terminal row.

    Memory and unknown external savers are left unchanged and return ``False``.
    Every rejection happens before the SQLite transaction, so active/blocked
    histories and insufficiently rooted terminal histories remain intact.
    """

    supplied = validate_top_level_state(terminal_state)
    _require_terminal_state(supplied)
    sqlite_saver = _sqlite_saver(checkpointer)
    if sqlite_saver is None:
        return False

    configurable = dict(config.get("configurable") or {})
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise TerminalCheckpointCompactionError(
            "terminal compaction requires configurable.thread_id"
        )
    if supplied.get("session_id") != thread_id:
        raise TerminalCheckpointCompactionError(
            "terminal state session_id does not match checkpoint thread_id"
        )
    checkpoint_ns = str(configurable.get("checkpoint_ns") or "")
    latest_config = {
        **dict(config),
        "configurable": {
            **configurable,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        },
    }
    latest_config["configurable"].pop("checkpoint_id", None)
    latest = checkpointer.get_tuple(latest_config)
    if latest is None:
        raise TerminalCheckpointCompactionError("no checkpoint exists to compact")
    latest_values = latest.checkpoint.get("channel_values")
    if not isinstance(latest_values, dict):
        raise TerminalCheckpointCompactionError(
            "latest checkpoint has no state channel values"
        )
    latest_state = {
        key: value
        for key, value in latest_values.items()
        if key in TopLevelState.__annotations__
    }
    validated_latest = validate_top_level_state(latest_state)
    _require_terminal_state(validated_latest)
    if (
        validated_latest.get("session_id") != supplied.get("session_id")
        or validated_latest.get("supervision_state")
        != supplied.get("supervision_state")
        or validated_latest.get("current_stage") != supplied.get("current_stage")
    ):
        raise TerminalCheckpointCompactionError(
            "supplied terminal state does not match the latest checkpoint"
        )

    run_root = Path(str(validated_latest.get("data_dir") or ""))
    _verify_durable_terminal_projection(validated_latest, run_root)
    retention = verify_checkpoint_free_artifact_retention(
        run_root,
        validated_latest,
    )
    missing = retention["missing_artifact_ids"]
    if missing:
        raise TerminalCheckpointCompactionError(
            "terminal checkpoint artifacts are not reachable without checkpoint roots: "
            + ", ".join(missing)
        )

    latest_checkpoint_id = latest.config.get("configurable", {}).get(
        "checkpoint_id"
    )
    if not isinstance(latest_checkpoint_id, str) or not latest_checkpoint_id:
        raise TerminalCheckpointCompactionError(
            "latest checkpoint is missing its checkpoint_id"
        )

    with sqlite_saver.lock:
        connection = sqlite_saver.conn
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT checkpoint_id FROM checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = ? "
                "ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id, checkpoint_ns),
            ).fetchone()
            if exists is None or exists[0] != latest_checkpoint_id:
                raise TerminalCheckpointCompactionError(
                    "latest checkpoint changed before compaction"
                )
            connection.execute(
                "DELETE FROM writes WHERE thread_id = ?",
                (thread_id,),
            )
            connection.execute(
                "DELETE FROM checkpoints "
                "WHERE thread_id = ? AND NOT (checkpoint_ns = ? AND checkpoint_id = ?)",
                (thread_id, checkpoint_ns, latest_checkpoint_id),
            )
            connection.execute(
                "UPDATE checkpoints SET parent_checkpoint_id = NULL "
                "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                (thread_id, checkpoint_ns, latest_checkpoint_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return True


def _sqlite_saver(checkpointer: Any) -> Any | None:
    inner = checkpointer
    while isinstance(inner, StateBudgetCheckpointer):
        inner = inner.inner
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        return None
    return inner if isinstance(inner, SqliteSaver) else None


def _require_terminal_state(state: Mapping[str, Any]) -> None:
    if state.get("supervision_state") not in {"completed", "failed_terminal"}:
        raise TerminalCheckpointCompactionError(
            "only a completed or failed terminal state can be compacted"
        )
    if state.get("active_interrupt"):
        raise TerminalCheckpointCompactionError(
            "terminal checkpoint cannot retain an active interrupt"
        )


def _verify_durable_terminal_projection(
    state: Mapping[str, Any],
    run_root: Path,
) -> None:
    session_id = str(state["session_id"])
    summary = inspect_session(session_id, run_root)
    terminal_event = summary.get("latest_terminal_event")
    if not isinstance(terminal_event, dict):
        raise TerminalCheckpointCompactionError(
            "ledger has no committed terminal event"
        )
    if summary.get("supervision_state") != state.get("supervision_state"):
        raise TerminalCheckpointCompactionError(
            "ledger cannot reconstruct the terminal supervision state"
        )
    if summary.get("current_stage") != state.get("current_stage"):
        raise TerminalCheckpointCompactionError(
            "ledger cannot reconstruct the terminal current stage"
        )
    if state.get("supervision_state") == "failed_terminal" and (
        summary.get("last_error") != state.get("last_error")
    ):
        raise TerminalCheckpointCompactionError(
            "ledger cannot reconstruct the terminal failure"
        )


__all__ = [
    "TerminalCheckpointCompactionError",
    "compact_terminal_checkpoint",
]
