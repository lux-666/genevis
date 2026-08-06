"""Shared failure boundary for stage-level execution collection.

Collect callbacks are intentionally stage-specific, but storage and evidence
failures must have one bounded, auditable outcome.  This module keeps that
boundary small: business callbacks may still update signals and publish
artifacts, while expected collection errors become a failed stage cursor and
causal ledger events.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from app.control.workflow_plan import current_workflow_step_id
from app.execution.attempt_owners import MethodExecution
from app.schemas.state import JsonValue, StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, stage_output, update_stage_output
from app.storage.ledger import Ledger, LedgerConflictError, LedgerFormatError
from app.storage.paths import DataLayoutError

if TYPE_CHECKING:
    from app.execution.lifecycle import MethodExecutionLifecycle

# DataLayoutError and LedgerFormatError subclass ValueError, but are listed
# explicitly so an ordinary callback ValueError remains a visible programming
# error. LedgerConflictError subclasses RuntimeError and is likewise an
# expected storage failure at this boundary.
COLLECT_ERROR_TYPES = (
    OSError,
    DataLayoutError,
    LedgerFormatError,
    LedgerConflictError,
)
COLLECT_REASON_MAX_CHARS = 2048


def bounded_collect_reason(label: str, error: BaseException) -> str:
    """Return a checkpoint-safe collection failure summary."""

    detail = str(error).strip() or error.__class__.__name__
    reason = f"{label} collect failed: {detail}"
    if len(reason) > COLLECT_REASON_MAX_CHARS:
        reason = reason[: COLLECT_REASON_MAX_CHARS - 3] + "..."
    return reason


def collect_with_failure_boundary(
    state: TopLevelState,
    *,
    lifecycle: MethodExecutionLifecycle,
    collect: Callable[[TopLevelState], StateUpdate],
    update: Callable[[TopLevelState, dict[str, Any]], StateUpdate] | None = None,
) -> StateUpdate:
    """Run a stage collect callback behind one bounded failure boundary.

    The callback owns business output handling.  If expected filesystem,
    evidence, or layout validation fails, this function records the failure and
    returns a failed stage update instead of allowing the exception to escape
    the graph.  A best-effort attempt lookup adds the more specific
    ``attempt_failed`` event — but only while the attempt has not already
    succeeded, so one attempt never carries both terminal facts.
    ``stage_failed`` remains the causal event even when the execution set
    itself is malformed or unavailable.
    """

    try:
        return collect(state)
    except COLLECT_ERROR_TYPES as exc:
        reason = bounded_collect_reason(lifecycle.name, exc)
        _record_collect_failure(
            state,
            reason=reason,
            lifecycle=lifecycle,
        )
        payload: dict[str, JsonValue] = {
            "status": "failed",
            "failure_kind": "collect",
            "reason": reason,
        }
        if update is not None:
            return update(state, payload)
        return update_stage_output(state, lifecycle.stage, payload)


def _record_collect_failure(
    state: TopLevelState,
    *,
    reason: str,
    lifecycle: MethodExecutionLifecycle,
) -> None:
    """Append bounded collect failure events without masking the state update."""

    attempt: MethodExecution | None = None
    try:
        executions = lifecycle.owner.load_executions(
            state,
            stage_output(state, lifecycle.stage),
            lifecycle,
        )
        if len(executions) == 1:
            attempt = executions[0]
    except (OSError, ValueError, LedgerConflictError):
        # The stage event below is still useful when execution evidence is
        # itself corrupt or absent. This is the local evidence parsing point;
        # callback ValueError is deliberately not caught by the outer boundary.
        attempt = None

    try:
        ledger = Ledger(absolute_data_dir(state))
    except COLLECT_ERROR_TYPES:
        return

    # Terminal facts have one writer: collect adapters own attempt_succeeded,
    # and this boundary records attempt_failed only while the attempt has not
    # already succeeded.  A broken ledger read fails closed: the writes below
    # then fail the same way and are swallowed.
    if attempt is not None and attempt.evidence.success:
        try:
            already_succeeded = ledger.has_attempt_success(
                attempt.attempt.session_id,
                attempt.attempt.attempt_id,
            )
        except COLLECT_ERROR_TYPES:
            already_succeeded = False
        if not already_succeeded:
            try:
                ledger.record_attempt_failed(
                    attempt.attempt.session_id,
                    attempt.attempt,
                    return_code=None,
                    error=reason,
                )
            except COLLECT_ERROR_TYPES:
                # A prior attempt event must not suppress the stage event.
                pass

    reason_digest = sha256(reason.encode("utf-8")).hexdigest()[:16]
    try:
        workflow_step_id = current_workflow_step_id(
            state,
            expected_stage=lifecycle.stage,
        )
        ledger.record_stage_failed(
            state["session_id"],
            lifecycle.stage,
            workflow_step_id=workflow_step_id,
            error=reason,
            idempotency_key=(
                f"collect:{state['session_id']}:{workflow_step_id}:"
                f"{lifecycle.method_name}:failed:{reason_digest}"
            ),
        )
    except COLLECT_ERROR_TYPES:
        # The failed cursor is the primary recovery signal.  A broken ledger
        # must not turn a bounded collect failure back into an uncaught error.
        return
