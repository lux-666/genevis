"""Routing helpers for assembly candidate planning."""

from __future__ import annotations

from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
)
from app.schemas.state import TopLevelState
from app.stages.assembly.constants import EXTRA_FAIL_STATUSES
from app.stages.base import is_fail_status, stage_output


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving assembly inputs."""
    return (
        "fail"
        if is_fail_status(
            stage_output(state, "assembly").get("status"), EXTRA_FAIL_STATUSES
        )
        else "infer_profile"
    )


def route_after_enter(state: TopLevelState) -> str:
    """Route retry states directly back into parameter recommendation."""
    if stage_output(state, "assembly").get("status") in {
        "retry_parameters_after_execution_failure",
        "retry_parameters_after_review_failure",
    }:
        return "recommend"
    if stage_output(state, "assembly").get("status") == "parameters_approved":
        return "plan"
    return "resolve"


def route_after_infer_profile(state: TopLevelState) -> str:
    """Route after writing the input profile."""
    status = stage_output(state, "assembly").get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "select_route"


def route_after_select_route(state: TopLevelState) -> str:
    """Route after confirming or blocking on route selection."""
    status = stage_output(state, "assembly").get("status")
    if status == "blocked_human":
        return "block"
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "build"


def route_after_candidate_set(state: TopLevelState) -> str:
    """Route after building candidate compatibility records."""
    return "fail" if is_fail_status(stage_output(state, "assembly").get("status"), EXTRA_FAIL_STATUSES) else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route after selecting primary/fallback candidates."""
    status = stage_output(state, "assembly").get("status")
    if status == "blocked_human":
        return "block"
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "recommend"


def route_after_parameter_recommendation(state: TopLevelState) -> str:
    """Route after Phase10 parameter recommendation."""
    status = stage_output(state, "assembly").get("status")
    if status == "blocked_human":
        return "block"
    if status in {"parameter_retry_stopped", "parameter_retry_expert_review"}:
        return "fail"
    if is_fail_status(status, EXTRA_FAIL_STATUSES) or status in {
        "parameter_response_invalid",
        "parameter_recommendation_failed",
    }:
        return "fail"
    return "plan"


def route_after_plan(state: TopLevelState) -> str:
    """Route after writing planned candidate bundles."""
    status = stage_output(state, "assembly").get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
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


def route_after_method_subgraph(state: TopLevelState) -> str:
    """Route after a method-specific assembly subgraph returns."""
    output = stage_output(state, "assembly")
    status = output.get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "retry_gate" if _current_attempt_has_execution_result(output) else "fail"
    return "commit"


def route_after_execution_retry_gate(state: TopLevelState) -> str:
    """Route after checking whether a failed executable attempt can retry."""
    status = stage_output(state, "assembly").get("status")
    if status == "retry_parameters_after_execution_failure":
        return "recommend"
    if status == "draft_ready":
        return "commit"
    return "fail"


def _current_attempt_has_execution_result(output: dict) -> bool:
    try:
        attempt = current_parameter_attempt(
            output,
            label="Assembly",
        )
    except ParameterAttemptStateError:
        return False
    return attempt.execution_ref is not None
