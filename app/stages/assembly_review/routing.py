"""Assembly review stage routing.

Routes to:
- "human_gate"  : candidates available for review
- "fail"        : no assembly candidates available
"""

from __future__ import annotations

from app.schemas.state import TopLevelState


FAIL_STATUSES = {"failed", "blocked_env", "missing_tool"}


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("assembly_review") or {}


def route_after_plan_metrics(state: TopLevelState) -> str:
    """Route after planning review metrics."""
    ar_out = _output(state)
    if ar_out.get("status") in FAIL_STATUSES:
        return "fail"
    if ar_out.get("execution_mode") == "local":
        return "freeze_quast"
    return "evaluate_rules"


def route_after_quast_freeze(state: TopLevelState) -> str:
    """Route after freezing QUAST attempts."""
    ar_out = _output(state)
    if ar_out.get("status") in FAIL_STATUSES:
        return "fail"
    if ar_out.get("status") == "quast_frozen":
        return "run_quast"
    return "evaluate_rules"


def route_after_quast_run(state: TopLevelState) -> str:
    """Route after running QUAST attempts."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "collect_quast"


def route_after_quast_collect(state: TopLevelState) -> str:
    """Route after collecting QUAST artifacts."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "freeze_busco"


def route_after_busco_freeze(state: TopLevelState) -> str:
    """Route after freezing BUSCO attempts."""
    ar_out = _output(state)
    if ar_out.get("status") in FAIL_STATUSES:
        return "fail"
    if ar_out.get("status") == "busco_frozen":
        return "run_busco"
    return "evaluate_rules"


def route_after_busco_run(state: TopLevelState) -> str:
    """Route after running BUSCO attempts."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "collect_busco"


def route_after_busco_collect(state: TopLevelState) -> str:
    """Route after collecting BUSCO artifacts."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "evaluate_rules"


def route_from_evaluation(state: TopLevelState) -> str:
    """Return the next node after assembly review evaluation."""
    ar_out = _output(state)

    if ar_out.get("status") in FAIL_STATUSES:
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
    if ar_out.get("status") in FAIL_STATUSES:
        return "fail"
    return "human_gate"
