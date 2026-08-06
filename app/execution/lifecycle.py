"""Shared freeze, run, and collect lifecycle for executable methods."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.control.workflow_plan import (
    bind_attempt_to_current_workflow_step,
    current_workflow_step_id,
)
from app.execution.attempt_owners import (
    AttemptOwner,
    DirectAttemptOwner,
    MethodExecution,
)
from app.execution.collect import collect_with_failure_boundary
from app.execution.evidence import (
    persist_execution_result,
    persist_frozen_attempt,
    recover_execution_result,
)
from app.execution.input_integrity import verify_input_dependencies
from app.execution.method_runner import (
    RunnerResultIdentityError,
    bounded_failure_reason,
    run_frozen_attempt,
)
from app.execution.runner import Runner
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import JsonValue, StateUpdate, TopLevelState
from app.stages.base import stage_output, update_stage_output
from app.storage.ledger import LedgerConflictError
from app.storage.paths import DataLayoutError

ProcessFailurePolicy = Literal["fail", "collect"]
StageNode = Callable[[TopLevelState], StateUpdate]
CompiledMethodGraph = CompiledStateGraph[
    TopLevelState,
    None,
    TopLevelState,
    TopLevelState,
]


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
) -> CompiledMethodGraph:
    """Build the canonical freeze -> run -> collect method subgraph."""

    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", _as_langgraph_node(freeze))
    graph.add_node(
        "run",
        _as_langgraph_node(
            lambda state: run_method_attempts(state, lifecycle, runner=runner)
        ),
    )
    graph.add_node(
        "collect",
        _as_langgraph_node(
            lambda state: collect_with_failure_boundary(
                state,
                lifecycle=lifecycle,
                collect=collect,
            )
        ),
    )
    graph.add_node("done", _as_langgraph_node(_empty_update))
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
        lambda state: _route_after_run(state, lifecycle),
        {"run": "run", "collect": "collect", "done": "done"},
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


def _route_after_run(
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
) -> StateUpdate:
    """Persist frozen runner inputs once, then bind them through the owner."""

    if not attempts:
        return _failure_update(state, lifecycle, "no frozen attempts were prepared")
    bound_attempts = [
        bind_attempt_to_current_workflow_step(state, attempt) for attempt in attempts
    ]
    seen_ids: set[str] = set()
    for attempt in bound_attempts:
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
        frozen_refs = [
            persist_frozen_attempt(state, attempt) for attempt in bound_attempts
        ]
        owner_updates = lifecycle.owner.record_frozen_attempts(
            output,
            lifecycle,
            bound_attempts,
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
) -> StateUpdate:
    """Advance at most one owner-frozen attempt through the run boundary."""

    output = stage_output(state, lifecycle.stage)
    if output.get("status") != lifecycle.frozen_status:
        return {}
    try:
        pending = lifecycle.owner.load_frozen_attempts(state, output, lifecycle)
        complete = lifecycle.owner.is_complete(output, lifecycle)
    except (OSError, ValueError) as exc:
        return _failure_update(
            state,
            lifecycle,
            f"{lifecycle.name} frozen attempt is invalid: {exc}",
        )

    if not pending:
        if not complete:
            return _failure_update(
                state,
                lifecycle,
                f"{lifecycle.name} has no pending execution attempt",
            )
        return update_stage_output(
            state,
            lifecycle.stage,
            {"status": lifecycle.ran_status},
        )

    try:
        active_workflow_step_id = current_workflow_step_id(
            state,
            expected_stage=lifecycle.stage,
        )
        for owned_attempt in pending:
            attempt_step_id = (owned_attempt.attempt.metadata or {}).get(
                "workflow_step_id"
            )
            if not isinstance(attempt_step_id, str) or not attempt_step_id:
                raise ValueError("frozen attempt is missing workflow_step_id metadata")
            if attempt_step_id != active_workflow_step_id:
                raise ValueError(
                    "frozen attempt workflow_step_id does not match the active "
                    f"occurrence: expected {active_workflow_step_id!r}, got "
                    f"{attempt_step_id!r}"
                )
    except ValueError as exc:
        return _failure_update(
            state,
            lifecycle,
            f"{lifecycle.name} frozen attempt is invalid: {exc}",
        )

    owned_attempt = pending[0]
    attempt = owned_attempt.attempt
    try:
        recovered = recover_execution_result(
            state,
            stage=lifecycle.stage,
            method_name=lifecycle.method_name,
            attempt_id=attempt.attempt_id,
            frozen_attempt_ref=owned_attempt.frozen_attempt_ref,
        )
    except (OSError, ValueError) as exc:
        return _failure_update(
            state,
            lifecycle,
            f"{lifecycle.name} execution evidence is invalid: {exc}",
        )

    if recovered is None:
        try:
            verify_input_dependencies(state, attempt.input_dependencies)
        except (OSError, ValueError) as exc:
            return _failure_update(
                state,
                lifecycle,
                f"{lifecycle.name} input integrity check failed: {exc}",
            )

        # run_frozen_attempt bounds every expected execution/storage error in
        # its RunOutcome. What escapes is either the explicit runner identity
        # contract or a programming bug, both of which must stay visible.
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
            )
        except (OSError, DataLayoutError) as exc:
            return _failure_update(
                state,
                lifecycle,
                f"{lifecycle.name} frozen attempt changed before execution: {exc}",
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
            )

        try:
            execution_ref = persist_execution_result(
                state,
                stage=lifecycle.stage,
                method_name=lifecycle.method_name,
                frozen_attempt_ref=owned_attempt.frozen_attempt_ref,
                result=outcome.result,
                error=outcome.error,
                boundary_failure=outcome.boundary_failure,
            )
        except (OSError, ValueError, LedgerConflictError) as exc:
            return _failure_update(
                state,
                lifecycle,
                f"{lifecycle.name} execution evidence could not be persisted: {exc}",
            )
        success = outcome.success
        error = outcome.error
        boundary_failure = outcome.boundary_failure
    else:
        execution_ref = recovered.execution_ref
        success = recovered.evidence.success
        error = recovered.evidence.error
        boundary_failure = recovered.boundary_failure

    try:
        owner_updates = lifecycle.owner.record_execution(
            output,
            lifecycle,
            attempt,
            execution_ref,
            success=success,
        )
        current_output = {**dict(output), **owner_updates}
        complete = lifecycle.owner.is_complete(current_output, lifecycle)
    except (OSError, ValueError, LedgerConflictError) as exc:
        return _failure_update(
            state,
            lifecycle,
            f"{lifecycle.name} execution cursor could not be advanced: {exc}",
        )

    if boundary_failure or (not success and lifecycle.process_failure == "fail"):
        return _failure_update(
            state,
            lifecycle,
            error or f"{lifecycle.name} execution failed",
            owner_updates=owner_updates,
        )

    return update_stage_output(
        state,
        lifecycle.stage,
        {
            "status": (
                lifecycle.ran_status if complete else lifecycle.frozen_status
            ),
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
) -> StateUpdate:
    payload: dict[str, JsonValue] = {
        "status": "failed",
        "reason": bounded_failure_reason(reason),
    }
    if lifecycle.failure_kind is not None:
        payload["failure_kind"] = lifecycle.failure_kind
    if owner_updates:
        payload.update(owner_updates)
    return update_stage_output(state, lifecycle.stage, payload)


def _empty_update(_state: TopLevelState) -> StateUpdate:
    return {}


def _as_langgraph_node(
    node: StageNode,
) -> RunnableLambda[TopLevelState, dict[str, Any]]:
    """Adapt the typed partial-state contract to LangGraph's dict protocol."""

    def invoke(state: TopLevelState) -> dict[str, Any]:
        return dict(node(state))

    return RunnableLambda(invoke)
