"""Assembly review stage routing.

Routes to:
- "human_gate"  : candidates available for review
- "fail"        : no assembly candidates available
"""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.workflow import is_terminal_failure


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly_review") or {}


def _assembly_output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly") or {}


def route_after_enter(state: TopLevelState) -> str:
    ar_out = _output(state)
    assembly_out = _assembly_output(state)
    status = assembly_out.get("status")
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    if assembly_out.get("rerun_request") is not None or status in {
        "flye_rerun_planned",
        "flye_rerun_frozen",
        "flye_rerun_ran",
    }:
        return "fail"
    return "resolve_candidates"


def route_after_plan_metrics(state: TopLevelState) -> str:
    """Route after planning review metrics."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    if ar_out.get("execution_mode") == "local":
        return "quast"
    return "evaluate_rules"


def route_after_quast(state: TopLevelState) -> str:
    """Route after the QUAST method subgraph returns."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    if ar_out.get("status") == "evaluating":
        return "busco"
    return "evaluate_rules"


def route_after_busco(state: TopLevelState) -> str:
    """Route after the BUSCO method subgraph returns."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    return "evaluate_rules"


def _has_available_review_metrics(ar_out: dict) -> bool:
    """Check if any review metrics are available (not pending/failed)."""
    signals = ar_out.get("signals") or []
    for signal in signals:
        if signal.get("status") == "available" and signal.get("signal_name") in {
            "quast",
            "busco",
        }:
            return True
    return False


def route_after_parameter_quality_gate(state: TopLevelState) -> str:
    """Route Phase10 parameter-quality gate or continue normal review."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    if (
        state.get("current_stage") == "assembly"
        and ar_out.get("status") == "parameter_replan_requested"
    ):
        return "replan"
    assembly_out = (state.get("stage_outputs") or {}).get("assembly") or {}
    if (
        assembly_out.get("parameter_attempts")
        and ar_out.get("status") != "parameter_replan_requested"
    ):
        if ar_out.get("parameter_quality_checked"):
            return "related_species_sanity_check"
        if ar_out.get("parameter_retries_exhausted"):
            return "related_species_sanity_check"
        # Only enter quality gate if metrics are available (not pending/unconfigured)
        if _has_available_review_metrics(ar_out):
            return "parameter_quality_gate"
        # Pending/unconfigured metrics -> skip gate, continue to human review
        return "related_species_sanity_check"
    return "related_species_sanity_check"


def route_from_evaluation(state: TopLevelState) -> str:
    """Return the next node after assembly review evaluation."""
    ar_out = _output(state)

    if is_terminal_failure(ar_out.get("status")):
        return "fail"

    candidates = ar_out.get("candidates", [])
    if len(candidates) >= 2:
        return "generate_comparison"

    if ar_out.get("candidate_count", 0) > 0:
        return "human_gate"

    return "fail"


def route_after_comparison(state: TopLevelState) -> str:
    """Route after generating candidate comparison."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    return "human_gate"
