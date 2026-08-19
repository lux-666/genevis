"""Routing helpers for assembly candidate planning."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.schemas.state import TopLevelState
from app.stages.assembly.state import AssemblyStageCursor, assembly_stage_output
from app.workflow import is_terminal_failure

EXTRA_FAIL_STATUSES = frozenset({"blocked_param"})


def route_after_resolve(state: TopLevelState) -> Literal["fail", "infer_profile"]:
    """Route after resolving assembly inputs."""
    return (
        "fail"
        if is_terminal_failure(
            assembly_stage_output(state).get("status"),
            extra_statuses=EXTRA_FAIL_STATUSES,
        )
        else "infer_profile"
    )


def route_after_enter(
    state: TopLevelState,
) -> Literal["resolve", "recommend", "plan"]:
    """Route retry states directly back into parameter recommendation."""
    if assembly_stage_output(state).get("status") in {
        "retry_parameters_after_execution_failure",
    }:
        return "recommend"
    if assembly_stage_output(state).get("status") == "parameters_approved":
        return "plan"
    return "resolve"


def route_after_infer_profile(
    state: TopLevelState,
) -> Literal["fail", "select_route"]:
    """Route after writing the input profile."""
    status = assembly_stage_output(state).get("status")
    if is_terminal_failure(status, extra_statuses=EXTRA_FAIL_STATUSES):
        return "fail"
    return "select_route"


def route_after_select_route(
    state: TopLevelState,
) -> Literal["block", "fail", "build"]:
    """Route after confirming or blocking on route selection."""
    status = assembly_stage_output(state).get("status")
    if status == "blocked_human":
        return "block"
    if is_terminal_failure(status, extra_statuses=EXTRA_FAIL_STATUSES):
        return "fail"
    return "build"


def route_after_candidate_set(state: TopLevelState) -> Literal["fail", "select"]:
    """Route after building candidate compatibility records."""
    return (
        "fail"
        if is_terminal_failure(
            assembly_stage_output(state).get("status"),
            extra_statuses=EXTRA_FAIL_STATUSES,
        )
        else "select"
    )


def route_after_select(
    state: TopLevelState,
) -> Literal["block", "fail", "recommend"]:
    """Route after selecting primary/fallback candidates."""
    status = assembly_stage_output(state).get("status")
    if status == "blocked_human":
        return "block"
    if is_terminal_failure(status, extra_statuses=EXTRA_FAIL_STATUSES):
        return "fail"
    return "recommend"


def route_after_parameter_recommendation(
    state: TopLevelState,
) -> Literal["block", "fail", "plan"]:
    """Route after Phase10 parameter recommendation."""
    status = assembly_stage_output(state).get("status")
    if status == "blocked_human":
        return "block"
    if is_terminal_failure(status, extra_statuses=EXTRA_FAIL_STATUSES) or status in {
        "parameter_response_invalid",
        "parameter_recommendation_failed",
    }:
        return "fail"
    return "plan"


def route_after_plan(
    state: TopLevelState,
) -> Literal["fail", "flye", "nextdenovo", "hifiasm", "hifiasm_ont", "commit"]:
    """Route after writing planned candidate bundles."""
    status = assembly_stage_output(state).get("status")
    if is_terminal_failure(status, extra_statuses=EXTRA_FAIL_STATUSES):
        return "fail"
    if status == "flye_execution_planned":
        return "flye"
    if status == "nextdenovo_execution_planned":
        return "nextdenovo"
    if status == "hifiasm_execution_planned":
        return "hifiasm"
    if status == "hifiasm_ont_execution_planned":
        return "hifiasm_ont"
    return "commit"


def route_after_method_subgraph(
    state: TopLevelState,
) -> Literal["retry_gate", "fail", "commit"]:
    """Route after a method-specific assembly subgraph returns."""
    output = assembly_stage_output(state)
    status = output.get("status")
    if is_terminal_failure(status, extra_statuses=EXTRA_FAIL_STATUSES):
        return "retry_gate" if _current_attempt_has_execution_result(output) else "fail"
    return "commit"


def route_after_commit(state: TopLevelState) -> Literal["settle", "done"]:
    """Settle a failed commit before the outer Assembly graph terminates."""

    raw_outcome = state.get("workflow_step_outcome")
    outcome: Mapping[str, object] = (
        raw_outcome if isinstance(raw_outcome, Mapping) else {}
    )
    if (
        state.get("current_stage") == "assembly"
        and outcome.get("stage") == "assembly"
        and outcome.get("outcome") == "failed"
    ):
        return "settle"
    return "done"


def route_after_execution_retry_gate(
    state: TopLevelState,
) -> Literal["recommend", "commit", "fail"]:
    """Route after checking whether a failed executable attempt can retry."""
    status = assembly_stage_output(state).get("status")
    if status == "retry_parameters_after_execution_failure":
        return "recommend"
    if status == "draft_ready":
        return "commit"
    return "fail"


def _current_attempt_has_execution_result(output: AssemblyStageCursor) -> bool:
    try:
        attempt = current_parameter_attempt(
            output,
            label="Assembly",
        )
    except ParameterAttemptStateError:
        return False
    return attempt.execution_ref is not None
