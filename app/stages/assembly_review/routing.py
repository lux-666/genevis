"""Assembly review stage routing.

Routes to:
- "commit"      : deterministic safe path (existing assembly only)
- "human_gate"  : multiple candidates or assembly-stage candidates
- "fail"        : no assembly candidates available
"""

from __future__ import annotations

from app.schemas.state import TopLevelState


def route_from_evaluation(state: TopLevelState) -> str:
    """Return the next node after assembly review evaluation."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})

    if ar_out.get("status") == "failed":
        return "fail"

    if ar_out.get("deterministic_safe"):
        return "commit"

    if ar_out.get("needs_human"):
        return "human_gate"

    # Fallback: if there is at least one candidate but no clear rule,
    # block for human review rather than auto-advancing
    if ar_out.get("candidate_count", 0) > 0:
        return "human_gate"

    return "fail"
