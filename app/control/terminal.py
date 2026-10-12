"""Prove, settle, and persist authoritative workflow terminal outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.runnables import RunnableConfig

from app.control.occurrence_settlement import (
    _settled_method_names,
    settle_workflow_step_outcome,
)
from app.control.workflow_plan import (
    fail_workflow_step,
)
from app.execution.method_outcomes import MethodOutcome, settle_method_outcomes
from app.schemas.state import (
    StateUpdate,
    TopLevelState,
    validate_top_level_state,
)
from app.storage.ledger import (
    Ledger,
    LedgerConflictError,
    project_stage_terminal_events,
)
from app.storage.paths import SessionPaths


class TerminalFinalizationError(RuntimeError):
    """Raised when a terminal checkpoint cannot be proven authoritative."""


@dataclass(frozen=True)
class TerminalFinalizationResult:
    """Proven terminal checkpoint, singleton Ledger row, and soft warning."""

    state: TopLevelState
    terminal_event: dict[str, Any]
    compaction_warning: str | None = None


def reconcile_canonical_terminal_checkpoint(
    checkpoint_state: Mapping[str, Any],
    terminal_event: Mapping[str, Any],
    *,
    session_id: str,
) -> None:
    """Require one canonical terminal to describe the exact terminal checkpoint."""

    if checkpoint_state.get("session_id") != session_id:
        raise TerminalFinalizationError(
            "terminal checkpoint session_id conflicts with the requested session"
        )
    if terminal_event.get("session_id") != session_id:
        raise TerminalFinalizationError(
            "canonical Ledger terminal session_id conflicts with the checkpoint"
        )
    summary = terminal_event.get("summary")
    outcome = summary.get("outcome") if isinstance(summary, Mapping) else None
    outcome_key = outcome if isinstance(outcome, str) else ""
    expected_supervision = {
        "completed": "completed",
        "failed": "failed_terminal",
    }.get(outcome_key)
    if expected_supervision is None:
        raise TerminalFinalizationError(
            f"canonical Ledger terminal has unsupported outcome: {outcome!r}"
        )
    failure_scope = (
        summary.get("failure_scope") if isinstance(summary, Mapping) else None
    )
    if outcome_key == "completed" and failure_scope is not None:
        raise TerminalFinalizationError(
            "completed canonical Ledger terminal must not carry failure_scope"
        )
    if (
        outcome_key == "failed"
        and failure_scope is not None
        and failure_scope
        not in {
            "occurrence",
            "controller_boundary",
        }
    ):
        raise TerminalFinalizationError(
            "canonical Ledger terminal has an invalid failure_scope"
        )
    if checkpoint_state.get("supervision_state") != expected_supervision:
        raise TerminalFinalizationError(
            "canonical Ledger terminal outcome conflicts with the checkpoint"
        )
    if checkpoint_state.get("active_interrupt") is not None:
        raise TerminalFinalizationError(
            "terminal checkpoint still carries an active review interrupt"
        )
    terminal_stage = terminal_event.get("stage")
    if not isinstance(terminal_stage, str) or not terminal_stage:
        raise TerminalFinalizationError("canonical Ledger terminal has no valid stage")
    if checkpoint_state.get("current_stage") != terminal_stage:
        raise TerminalFinalizationError(
            "canonical Ledger terminal stage conflicts with the checkpoint"
        )


def finalize_authoritative_terminal(
    graph: Any,
    config: RunnableConfig,
    state: dict[str, Any],
    session_id: str,
    data_dir: str | Path,
    checkpointer: str,
    *,
    normalize_failure: bool = False,
) -> TerminalFinalizationResult:
    """Prove terminal checkpoint, append one terminal fact, then compact.

    Projection writers are deliberately absent.  An ambiguous failure update
    is always followed by a checkpoint reread; matching persisted values win.
    """

    supervision = state.get("supervision_state")
    if supervision not in {"completed", "failed_terminal"}:
        raise TerminalFinalizationError(
            "authoritative terminal finalization requires a terminal state"
        )
    if normalize_failure and supervision != "failed_terminal":
        raise TerminalFinalizationError(
            "normalize_failure requires supervision_state=failed_terminal"
        )

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    terminal_state = {
        **state,
        "session_id": session_id,
        "data_dir": str(paths.run_dir),
        "active_interrupt": None,
    }

    update_error: Exception | None = None
    between_occurrences = False
    if normalize_failure:
        pending_outcome = terminal_state.get("workflow_step_outcome")
        workflow_index = terminal_state.get("workflow_step_index")
        entered_index = terminal_state.get("entered_workflow_step_index")
        between_occurrences = (
            isinstance(pending_outcome, Mapping)
            and pending_outcome.get("outcome") in {"completed", "skipped"}
            and type(workflow_index) is int
            and type(entered_index) is int
            and workflow_index == entered_index + 1
        )
        if between_occurrences:
            occurrence_failure: StateUpdate = {
                "supervision_state": "failed_terminal",
                "last_error": str(
                    terminal_state.get("last_error") or "workflow execution failed"
                ),
            }
        else:
            try:
                occurrence_failure = fail_workflow_step(
                    cast(TopLevelState, terminal_state),
                    str(terminal_state.get("current_stage") or ""),
                    str(
                        terminal_state.get("last_error") or "workflow execution failed"
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise TerminalFinalizationError(
                    "cannot normalize a runtime failure without an active frozen "
                    f"workflow occurrence: {exc}"
                ) from exc
        terminal_state.update(occurrence_failure)
        try:
            graph.update_state(
                config,
                {
                    **occurrence_failure,
                    "active_interrupt": None,
                },
            )
        except Exception as exc:
            update_error = exc

    try:
        snapshot = graph.get_state(config)
        checkpoint_values = getattr(snapshot, "values", None)
        if not isinstance(checkpoint_values, dict):
            raise TerminalFinalizationError(
                "persisted terminal checkpoint has no state values"
            )
        checkpoint_state = validate_top_level_state(checkpoint_values)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if update_error is not None:
            detail = (
                f"update_state raised {type(update_error).__name__}: {update_error}; "
                f"checkpoint reread failed: {detail}"
            )
        raise TerminalFinalizationError(
            f"terminal checkpoint could not be verified: {detail}"
        ) from exc

    try:
        if normalize_failure and not between_occurrences:
            _settle_runtime_failure_method_dispositions(checkpoint_state, ledger)
        settle_workflow_step_outcome(
            checkpoint_state,
            data_dir=paths.run_dir,
        )
    except (OSError, TypeError, ValueError, LedgerConflictError) as exc:
        raise TerminalFinalizationError(
            f"terminal checkpoint occurrence settlement failed: {exc}"
        ) from exc

    stage_terminals, terminal_integrity = project_stage_terminal_events(
        ledger.read_event_rows(session_id)
    )
    if terminal_integrity:
        raise TerminalFinalizationError(
            "terminal checkpoint has non-authoritative occurrence history"
        )
    steps = list(checkpoint_state.get("workflow_steps") or [])
    failure_scope: Literal["occurrence", "controller_boundary"] | None = None
    if supervision == "completed":
        if checkpoint_state.get("workflow_step_index") != len(steps):
            raise TerminalFinalizationError(
                "completed terminal checkpoint has not reached the end of its plan"
            )
        unsettled = [
            step["step_id"]
            for step in steps
            if stage_terminals.get((step["step_id"], step["stage"]), {}).get(
                "event_type"
            )
            not in {"stage_completed", "stage_skipped"}
        ]
        if unsettled:
            raise TerminalFinalizationError(
                "completed terminal checkpoint has unsettled workflow occurrences: "
                + ", ".join(unsettled)
            )
    else:
        failed = [
            terminal
            for terminal in stage_terminals.values()
            if terminal.get("event_type") == "stage_failed"
        ]
        workflow_step_outcome = checkpoint_state.get("workflow_step_outcome")
        if (
            isinstance(workflow_step_outcome, Mapping)
            and workflow_step_outcome.get("outcome") == "failed"
        ):
            if (
                len(failed) != 1
                or failed[0].get("workflow_step_id")
                != workflow_step_outcome.get("workflow_step_id")
                or failed[0].get("stage") != workflow_step_outcome.get("stage")
            ):
                raise TerminalFinalizationError(
                    "failed terminal checkpoint lacks one matching failed occurrence"
                )
            failed_step_id = str(workflow_step_outcome.get("workflow_step_id") or "")
            failed_index = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step.get("step_id") == failed_step_id
                ),
                None,
            )
            prefix_settled = failed_index is not None and all(
                stage_terminals.get((step["step_id"], step["stage"]), {}).get(
                    "event_type"
                )
                in {"stage_completed", "stage_skipped"}
                for step in steps[:failed_index]
            )
            no_terminal_after_failed = failed_index is not None and all(
                (step["step_id"], step["stage"]) not in stage_terminals
                for step in steps[failed_index + 1 :]
            )
            if not prefix_settled or not no_terminal_after_failed:
                raise TerminalFinalizationError(
                    "failed occurrence is not the terminal edge of a fully "
                    "settled workflow prefix"
                )
            failure_scope = "occurrence"
        else:
            workflow_index = checkpoint_state.get("workflow_step_index")
            entered_index = checkpoint_state.get("entered_workflow_step_index")
            boundary_failure = (
                not failed
                and isinstance(workflow_step_outcome, Mapping)
                and workflow_step_outcome.get("outcome") in {"completed", "skipped"}
                and type(workflow_index) is int
                and type(entered_index) is int
                and workflow_index == entered_index + 1
            )
            prefix_settled = boundary_failure and all(
                stage_terminals.get((step["step_id"], step["stage"]), {}).get(
                    "event_type"
                )
                in {"stage_completed", "stage_skipped"}
                for step in steps[:workflow_index]
            )
            no_terminal_after_frontier = boundary_failure and all(
                (step["step_id"], step["stage"]) not in stage_terminals
                for step in steps[workflow_index:]
            )
            if not prefix_settled or not no_terminal_after_frontier:
                raise TerminalFinalizationError(
                    "failed terminal checkpoint has neither a failed occurrence "
                    "nor a fully settled inter-occurrence frontier"
                )
            failure_scope = "controller_boundary"

    expected = {
        "session_id": session_id,
        "supervision_state": supervision,
        "current_stage": terminal_state.get("current_stage"),
        "active_interrupt": None,
    }
    if supervision == "failed_terminal":
        expected["last_error"] = terminal_state.get("last_error")
    for field, value in expected.items():
        if checkpoint_state.get(field) != value:
            detail = (
                f"terminal checkpoint {field} does not match: "
                f"expected {value!r}, got {checkpoint_state.get(field)!r}"
            )
            if update_error is not None:
                detail += (
                    f" after update_state raised {type(update_error).__name__}: "
                    f"{update_error}"
                )
            raise TerminalFinalizationError(detail)

    outcome: Literal["completed", "failed"] = (
        "completed" if supervision == "completed" else "failed"
    )
    refs = ledger.references_from(
        checkpoint_state,
        role_prefix=f"terminal.{outcome}",
    )
    terminal_read = ledger.read_canonical_terminal(session_id)
    if terminal_read is not None:
        reconcile_canonical_terminal_checkpoint(
            checkpoint_state,
            terminal_read["canonical"],
            session_id=session_id,
        )
    try:
        terminal_event = ledger.record_session_terminal(
            session_id,
            outcome=outcome,
            stage=checkpoint_state.get("current_stage"),
            error=(checkpoint_state.get("last_error") if outcome == "failed" else None),
            refs=refs,
            failure_scope=failure_scope,
        )
    except LedgerConflictError as exc:
        raise TerminalFinalizationError(str(exc)) from exc
    reconcile_canonical_terminal_checkpoint(
        checkpoint_state,
        terminal_event,
        session_id=session_id,
    )

    saver = getattr(graph, "checkpointer", None)
    if checkpointer != "sqlite" or saver is None:
        return TerminalFinalizationResult(
            state=checkpoint_state,
            terminal_event=terminal_event,
        )

    compaction_warning: str | None = None
    try:
        from app.graph.checkpoint_compaction import compact_terminal_checkpoint

        compacted = compact_terminal_checkpoint(saver, config, checkpoint_state)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        ledger.record_event(
            session_id,
            "terminal_compaction_skipped",
            {"error": error},
            idempotency_key="terminal-compaction:skipped",
        )
        compaction_warning = error
        compacted = False

    if compacted:
        ledger.record_event(
            session_id,
            "terminal_compaction_completed",
            {"checkpoint_count": 1},
            idempotency_key="terminal-compaction:completed",
        )
    return TerminalFinalizationResult(
        state=checkpoint_state,
        terminal_event=terminal_event,
        compaction_warning=compaction_warning,
    )


def _settle_runtime_failure_method_dispositions(
    state: TopLevelState,
    ledger: Ledger,
) -> None:
    """Conservatively close roster gaps left by an unexpected graph exception."""

    outcome: Mapping[str, Any] = state.get("workflow_step_outcome") or {}
    step_id = str(outcome.get("workflow_step_id") or "")
    step = next(
        step for step in state.get("workflow_steps") or [] if step["step_id"] == step_id
    )
    stage = step["stage"]
    cursor = (state.get("stage_outputs") or {}).get(stage, {})
    roster = _settled_method_names(step, cursor) or []
    rows = ledger.read_event_rows(state["session_id"])
    disposed = {
        (row.get("summary") or {}).get("method_name")
        for row in rows
        if row.get("event_type") == "method_disposition_recorded"
        and row.get("workflow_step_id") == step_id
    }
    latest_attempts = {}
    unfinished: dict[str, set[str]] = {}
    for row in rows:
        if row.get("workflow_step_id") == step_id and row.get("event_type") in {
            "attempt_started",
            "attempt_succeeded",
            "attempt_failed",
        }:
            method = (row.get("summary") or {}).get("method_name")
            if not isinstance(method, str):
                continue
            latest_attempts[method] = row
            pending = unfinished.setdefault(method, set())
            if row["event_type"] == "attempt_started":
                pending.add(row["subject_id"])
            else:
                pending.discard(row["subject_id"])
    reason = str(outcome.get("reason") or state.get("last_error") or "runtime failure")
    outcomes = []
    for method_name in roster:
        if method_name in disposed:
            continue
        attempt = latest_attempts.get(method_name)
        succeeded = (
            attempt is not None
            and attempt["event_type"] == "attempt_succeeded"
            and not unfinished.get(method_name)
        )
        failed = attempt is not None and not succeeded
        outcomes.append(
            MethodOutcome(
                method_name,
                "completed" if succeeded else "failed" if failed else "not_started",
                reason=reason if failed else None,
            )
        )
    settle_method_outcomes(state, stage=stage, outcomes=outcomes)
