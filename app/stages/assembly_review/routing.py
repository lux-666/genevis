"""Assembly review stage routing.

Routes to:
- "human_gate"  : candidates available for review
- "fail"        : no assembly candidates available
"""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.base import is_fail_status


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly_review") or {}


def route_after_plan_metrics(state: TopLevelState) -> str:
    """Route after planning review metrics."""
    ar_out = _output(state)
    if is_fail_status(ar_out.get("status")):
        return "fail"
    if ar_out.get("execution_mode") == "local":
        return "quast"
    return "evaluate_rules"


def route_after_quast(state: TopLevelState) -> str:
    """Route after the QUAST method subgraph returns."""
    ar_out = _output(state)
    if is_fail_status(ar_out.get("status")):
        return "fail"
    if ar_out.get("status") == "evaluating":
        return "busco"
    return "evaluate_rules"


def route_after_busco(state: TopLevelState) -> str:
    """Route after the BUSCO method subgraph returns."""
    ar_out = _output(state)
    if is_fail_status(ar_out.get("status")):
        return "fail"
    return "evaluate_rules"


def route_from_evaluation(state: TopLevelState) -> str:
    """Return the next node after assembly review evaluation."""
    ar_out = _output(state)

    if is_fail_status(ar_out.get("status")):
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
    if is_fail_status(ar_out.get("status")):
        return "fail"
    return "human_gate"
