"""Routing helpers for assembly candidate planning."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.assembly.constants import EXTRA_FAIL_STATUSES
from app.stages.base import is_fail_status


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving assembly inputs."""
    return "fail" if is_fail_status(_output(state).get("status"), EXTRA_FAIL_STATUSES) else "infer_profile"


def route_after_enter(state: TopLevelState) -> str:
    """Route retry states directly back into parameter recommendation."""
    if _output(state).get("status") in {
        "retry_parameters_after_execution_failure",
        "retry_parameters_after_review_failure",
    }:
        return "recommend"
    if _output(state).get("status") == "parameters_approved":
        return "plan"
    return "resolve"


def route_after_infer_profile(state: TopLevelState) -> str:
    """Route after writing the input profile."""
    status = _output(state).get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "select_route"


def route_after_select_route(state: TopLevelState) -> str:
    """Route after confirming or blocking on route selection."""
    status = _output(state).get("status")
    if status == "blocked_human":
        return "block"
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "build"


def route_after_candidate_set(state: TopLevelState) -> str:
    """Route after building candidate compatibility records."""
    return "fail" if is_fail_status(_output(state).get("status"), EXTRA_FAIL_STATUSES) else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route after selecting primary/fallback candidates."""
    status = _output(state).get("status")
    if status == "blocked_human":
        return "block"
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "fail"
    return "recommend"


def route_after_parameter_recommendation(state: TopLevelState) -> str:
    """Route after Phase10 parameter recommendation."""
    status = _output(state).get("status")
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
    status = _output(state).get("status")
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
    output = _output(state)
    status = output.get("status")
    if is_fail_status(status, EXTRA_FAIL_STATUSES):
        return "retry_gate" if _current_attempt_has_execution_result(output) else "fail"
    return "commit"


def route_after_execution_retry_gate(state: TopLevelState) -> str:
    """Route after checking whether a failed executable attempt can retry."""
    status = _output(state).get("status")
    if status == "retry_parameters_after_execution_failure":
        return "recommend"
    if status == "draft_ready":
        return "commit"
    return "fail"


def _current_attempt_has_execution_result(output: dict) -> bool:
    attempt_id = int(output.get("current_parameter_attempt_id") or 0)
    for item in output.get("parameter_attempts") or []:
        if isinstance(item, dict) and item.get("attempt_id") == attempt_id:
            return bool(item.get("execution_ref"))
    return False
