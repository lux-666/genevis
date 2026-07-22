"""Shared freeze, run, and collect lifecycle for executable methods."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

from langgraph.graph import END, START, StateGraph

from app.execution.attempt_owners import (
    AttemptOwner,
    DirectAttemptOwner,
    MethodExecution,
)
from app.execution.collect import collect_with_failure_boundary
from app.execution.evidence import persist_execution_result, persist_frozen_attempt
from app.execution.method_runner import (
    RunnerResultIdentityError,
    bounded_failure_reason,
    run_frozen_attempt,
)
from app.execution.runner import Runner
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import JsonValue, TopLevelState
from app.stages.base import stage_output, update_stage_output
from app.storage.ledger import LedgerConflictError

ProcessFailurePolicy = Literal["fail", "collect"]
StageNode = Callable[[TopLevelState], dict]


@dataclass(frozen=True, kw_only=True)
class MethodExecutionLifecycle:
    """Stage-neutral execution facts and cursor owner for one method."""

    stage: str
    name: str
    method_name: str
    frozen_status: str
    ran_status: str
    owner: AttemptOwner = field(default_factory=DirectAttemptOwner)
    cleanup_outputs: bool = True
    process_failure: ProcessFailurePolicy = "fail"
    failure_kind: str | None = None


def build_method_graph(
    lifecycle: MethodExecutionLifecycle,
    freeze: StageNode,
    collect: StageNode,
    *,
    runner: Runner | None = None,
):
    """Build the canonical freeze -> run -> collect method subgraph."""

    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", freeze)
    graph.add_node(
        "run",
        lambda state: run_method_attempts(state, lifecycle, runner=runner),
    )
    graph.add_node(
        "collect",
        lambda state: collect_with_failure_boundary(
            state,
            lifecycle=lifecycle,
            collect=collect,
        ),
    )
    graph.add_node("done", lambda _state: {})
    graph.add_conditional_edges(
        START,
        lambda state: _route_method_entry(state, lifecycle),
        {"freeze": "freeze", "run": "run", "collect": "collect"},
    )
    graph.add_conditional_edges(
        "freeze",
        lambda state: _route_after_freeze(state, lifecycle),
        {"run": "run", "collect": "collect", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: (
            "collect"
            if stage_output(state, lifecycle.stage).get("status")
            == lifecycle.ran_status
            else "done"
        ),
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def _route_method_entry(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
) -> str:
    """Resume at the first incomplete durable execution boundary."""

    status = stage_output(state, lifecycle.stage).get("status")
    if status == lifecycle.frozen_status:
        return "run"
    if status == lifecycle.ran_status:
        return "collect"
    return "freeze"


def _route_after_freeze(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
) -> str:
    status = stage_output(state, lifecycle.stage).get("status")
    if status == lifecycle.frozen_status:
        return "run"
    if status == lifecycle.ran_status:
        return "collect"
    return "done"


def freeze_method_attempts(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
    attempts: Sequence[FrozenAttemptSpec],
    *,
    owner_metadata: dict[str, JsonValue] | None = None,
    stage_updates: dict[str, JsonValue] | None = None,
) -> dict:
    """Persist frozen runner inputs once, then bind them through the owner."""

    if not attempts:
        return _failure_update(state, lifecycle, "no frozen attempts were prepared")
    seen_ids: set[str] = set()
    for attempt in attempts:
        if (
            attempt.stage != lifecycle.stage
            or attempt.method_name != lifecycle.method_name
        ):
            return _failure_update(
                state,
                lifecycle,
                f"{lifecycle.name} prepared an attempt with the wrong identity",
            )
        if attempt.attempt_id in seen_ids:
            return _failure_update(
                state,
                lifecycle,
                (
                    f"{lifecycle.name} prepared duplicate attempt_id "
                    f"{attempt.attempt_id!r}"
                ),
            )
        seen_ids.add(attempt.attempt_id)

    output = stage_output(state, lifecycle.stage)
    try:
        frozen_refs = [persist_frozen_attempt(state, attempt) for attempt in attempts]
        owner_updates = lifecycle.owner.record_frozen_attempts(
            output,
            lifecycle,
            attempts,
            frozen_refs,
            owner_metadata=owner_metadata,
        )
    except (OSError, ValueError) as exc:
        return _failure_update(
            state,
            lifecycle,
            f"{lifecycle.name} frozen attempt could not be persisted: {exc}",
        )
    return update_stage_output(
        state,
        lifecycle.stage,
        {
            **dict(stage_updates or {}),
            "status": lifecycle.frozen_status,
            **owner_updates,
        },
    )


def run_method_attempts(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
    *,
    runner: Runner | None = None,
) -> dict:
    """Run every owner-frozen attempt through one durable execution kernel."""

    output = stage_output(state, lifecycle.stage)
    if output.get("status") != lifecycle.frozen_status:
        return {}
    try:
        # Validate every owner cursor and frozen artifact before any process starts.
        attempts = lifecycle.owner.load_frozen_attempts(state, output, lifecycle)
    except (OSError, ValueError) as exc:
        return _failure_update(
            state,
            lifecycle,
            f"{lifecycle.name} frozen attempt is invalid: {exc}",
        )

    current_output = dict(output)
    owner_updates: dict[str, JsonValue] = {}
    for attempt in attempts:
        # run_frozen_attempt bounds every expected execution/storage error in
        # its RunOutcome.  What escapes here is either the explicit runner
        # identity contract or a programming bug; both must stay visible (the
        # identity error aborts the batch, everything else propagates).
        try:
            outcome = run_frozen_attempt(
                attempt,
                data_dir=state.get("data_dir", "data"),
                input_manifest_ref=state.get("input_manifest_ref", ""),
                fail_label=lifecycle.name,
                cleanup_outputs=lifecycle.cleanup_outputs,
                runner=runner,
            )
        except RunnerResultIdentityError as exc:
            return _failure_update(
                state,
                lifecycle,
                str(exc),
                owner_updates=owner_updates,
            )

        # The runner result is execution evidence for this exact frozen input.
        # Reject mismatched provenance before any evidence path or owner cursor
        # can be derived from the untrusted result identity.
        if outcome.result.attempt_id != attempt.attempt_id:
            return _failure_update(
                state,
                lifecycle,
                (
                    f"{lifecycle.name} runner result attempt_id mismatch: "
                    f"expected {attempt.attempt_id!r}, got "
                    f"{outcome.result.attempt_id!r}"
                ),
                owner_updates=owner_updates,
            )

        try:
            execution_ref = persist_execution_result(
                state,
                stage=lifecycle.stage,
                method_name=lifecycle.method_name,
                result=outcome.result,
                error=outcome.error,
            )
            update = lifecycle.owner.record_execution(
                current_output,
                lifecycle,
                attempt,
                execution_ref,
                success=outcome.success,
            )
        except (OSError, ValueError, LedgerConflictError) as exc:
            return _failure_update(
                state,
                lifecycle,
                f"{lifecycle.name} execution evidence could not be persisted: {exc}",
                owner_updates=owner_updates,
            )
        current_output.update(update)
        owner_updates.update(update)
        if outcome.boundary_failure or (
            not outcome.success and lifecycle.process_failure == "fail"
        ):
            return _failure_update(
                state,
                lifecycle,
                outcome.error,
                owner_updates=owner_updates,
            )

    return update_stage_output(
        state,
        lifecycle.stage,
        {
            "status": lifecycle.ran_status,
            **owner_updates,
        },
    )


def load_method_executions(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
) -> list[MethodExecution]:
    """Load the completed execution set required by a collect callback."""

    output = stage_output(state, lifecycle.stage)
    return lifecycle.owner.load_executions(state, output, lifecycle)


def _failure_update(
    state: TopLevelState,
    lifecycle: MethodExecutionLifecycle,
    reason: str,
    *,
    owner_updates: dict[str, JsonValue] | None = None,
) -> dict:
    payload: dict[str, JsonValue] = {
        "status": "failed",
        "reason": bounded_failure_reason(reason),
    }
    if lifecycle.failure_kind is not None:
        payload["failure_kind"] = lifecycle.failure_kind
    if owner_updates:
        payload.update(owner_updates)
    return update_stage_output(state, lifecycle.stage, payload)
