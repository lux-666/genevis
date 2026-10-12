"""Graph invocation and crash reconciliation, independent of CLI presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.control import terminal
from app.schemas.state import TopLevelState, validate_top_level_state
from app.storage.ledger import Ledger


class SessionExecutionError(RuntimeError):
    """Invocation cannot be reconciled with authoritative persisted evidence."""


@dataclass(frozen=True)
class SessionInvocation:
    state: TopLevelState
    resumed_from_blocked_checkpoint: bool = False
    invoke_error: str | None = None
    preserved_terminal: bool = False
    compaction_warning: str | None = None


def invoke_session_graph(
    graph: Any,
    config: RunnableConfig,
    seed: TopLevelState,
    *,
    ledger: Ledger,
    resume_checkpoint: bool,
    checkpointer: str,
) -> SessionInvocation:
    """Invoke once or retain an authoritative blocked/terminal checkpoint.

    A terminal checkpoint wins over an invoke/observer error. Failure is only
    synthesized when neither checkpoint nor Ledger proves an existing terminal.
    The caller owns the execution lease throughout this operation.
    """

    session_id = seed["session_id"]
    run_dir = seed["data_dir"]
    try:
        if resume_checkpoint:
            get_state = getattr(graph, "get_state", None)
            if callable(get_state):
                checkpoint = get_state(config)
                values = getattr(checkpoint, "values", None)
                canonical = ledger.read_canonical_terminal(session_id)
                if canonical is not None:
                    if not isinstance(values, dict):
                        raise terminal.TerminalFinalizationError(
                            "canonical Ledger terminal has no resumable checkpoint"
                        )
                    state = validate_top_level_state(values)
                    terminal.reconcile_canonical_terminal_checkpoint(
                        state, canonical["canonical"], session_id=session_id
                    )
                    return SessionInvocation(state)
                if isinstance(values, dict) and values.get("session_id") == session_id:
                    supervision = values.get("supervision_state")
                    if supervision in {"blocked_human", "completed", "failed_terminal"}:
                        return SessionInvocation(
                            validate_top_level_state(values),
                            resumed_from_blocked_checkpoint=supervision
                            == "blocked_human",
                        )
        return SessionInvocation(
            validate_top_level_state(
                graph.invoke(None if resume_checkpoint else seed, config=config)
            )
        )
    except Exception as exc:
        latest = dict(seed)
        try:
            snapshot = graph.get_state(config)
            values = getattr(snapshot, "values", None)
            if isinstance(values, dict):
                latest.update(values)
        except Exception:
            pass
        error = f"{type(exc).__name__}: {exc}"
        canonical = ledger.read_canonical_terminal(session_id)
        preserved = latest.get("supervision_state") in {"completed", "failed_terminal"}
        if not preserved and canonical is not None:
            raise SessionExecutionError(
                "canonical Ledger terminal conflicts with a nonterminal "
                f"checkpoint after invoke raised: {error}"
            ) from exc
        if not preserved:
            latest.update(
                session_id=session_id,
                data_dir=run_dir,
                input_manifest_ref=seed["input_manifest_ref"],
                supervision_state="failed_terminal",
                last_error=error,
            )
        try:
            finalized = terminal.finalize_authoritative_terminal(
                graph,
                config,
                latest,
                session_id,
                run_dir,
                checkpointer,
                **({"normalize_failure": True} if not preserved else {}),
            )
        except terminal.TerminalFinalizationError as terminal_exc:
            message = (
                "terminal checkpoint could not be reconciled after invoke raised"
                if preserved
                else "failed to persist terminal failure checkpoint"
            )
            raise SessionExecutionError(f"{message}: {terminal_exc}") from terminal_exc
        return SessionInvocation(
            finalized.state,
            invoke_error=error,
            preserved_terminal=preserved,
            compaction_warning=finalized.compaction_warning,
        )
