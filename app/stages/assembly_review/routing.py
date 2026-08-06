"""Assembly review stage routing.

Routes to:
- "human_gate"  : candidates available for review
- "fail"        : no assembly candidates available
"""

from __future__ import annotations

from typing import Literal

from app.schemas.state import TopLevelState
from app.stages.assembly_review.state import (
    AssemblyReviewCursor,
    assembly_review_output,
)
from app.workflow import is_terminal_failure

EnterRoute = Literal["fail", "resolve_candidates"]
PlanMetricsRoute = Literal["fail", "quast", "evaluate_rules"]
QuastRoute = Literal["fail", "busco", "evaluate_rules"]
EvaluationRoute = Literal["fail", "generate_comparison", "human_gate"]
QualityGateRoute = Literal[
    "fail",
    "parameter_quality_gate",
    "related_species_sanity_check",
]
ComparisonRoute = Literal["fail", "human_gate"]


def _output(state: TopLevelState) -> AssemblyReviewCursor:
    return assembly_review_output(state)


def route_after_enter(state: TopLevelState) -> EnterRoute:
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    return "resolve_candidates"


def route_after_plan_metrics(state: TopLevelState) -> PlanMetricsRoute:
    """Route after planning review metrics."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    if ar_out.get("execution_mode") == "local":
        return "quast"
    return "evaluate_rules"


def route_after_quast(state: TopLevelState) -> QuastRoute:
    """Route after the QUAST method subgraph returns."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    if ar_out.get("status") == "evaluating":
        return "busco"
    return "evaluate_rules"


def route_after_busco(state: TopLevelState) -> Literal["fail", "evaluate_rules"]:
    """Route after the BUSCO method subgraph returns."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    return "evaluate_rules"


def _has_available_review_metrics(ar_out: AssemblyReviewCursor) -> bool:
    """Check if any review metrics are available (not pending/failed)."""
    signals = ar_out.get("signals") or []
    for signal in signals:
        if signal.get("status") == "available" and signal.get("signal_name") in {
            "quast",
            "busco",
        }:
            return True
    return False


def route_after_parameter_quality_gate(state: TopLevelState) -> QualityGateRoute:
    """Route the review-owned quality diagnosis or continue normal review."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    assembly_out = (state.get("stage_outputs") or {}).get("assembly") or {}
    if assembly_out.get("parameter_attempts"):
        if ar_out.get("parameter_quality_checked"):
            return "related_species_sanity_check"
        # Only enter quality gate if metrics are available (not pending/unconfigured)
        if _has_available_review_metrics(ar_out):
            return "parameter_quality_gate"
        # Pending/unconfigured metrics -> skip gate, continue to human review
        return "related_species_sanity_check"
    return "related_species_sanity_check"


def route_from_evaluation(state: TopLevelState) -> EvaluationRoute:
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


def route_after_comparison(state: TopLevelState) -> ComparisonRoute:
    """Route after generating candidate comparison."""
    ar_out = _output(state)
    if is_terminal_failure(ar_out.get("status")):
        return "fail"
    return "human_gate"
