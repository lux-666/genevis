"""Routing helpers for the gene annotation stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState

TERMINAL_STATES = frozenset({"blocked_human", "failed_terminal"})


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("gene_annotation") or {}


def _supervision(state: TopLevelState) -> str:
    return str(state.get("supervision_state") or "")


def route_after_enter(state: TopLevelState) -> str:
    """Route after validating the annotation intake plan."""
    return "fail" if _output(state).get("status") == "failed" else "resolve"


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving gene annotation inputs."""
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "provided_annotation_resolved":
        return "commit"
    return "profile"


def route_after_evidence_profile(state: TopLevelState) -> str:
    """Route after selecting the evidence profile."""
    return "fail" if _output(state).get("status") == "failed" else "methods"


def route_after_method_plan(state: TopLevelState) -> str:
    """Route after plan_gene_methods — status-aware for crash-recovery re-entry."""
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "methods_planned":
        return "block_method"
    if status == "methods_confirmed":
        return "run_methods"
    if status == "candidates_ready":
        return "block_candidates"
    if status == "candidates_accepted":
        return "merge_prep"
    if status == "merge_ready":
        return "block_merge"
    if status == "merge_accepted":
        return "block_final"
    if status == "review_passed":
        return "commit"
    return "fail"


# --- after-gate routing (one per gate block node) -------------------------------

def route_after_method_planning_gate(state: TopLevelState) -> str:
    """After method-planning gate: continue to planning-only stub or stop."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    return "run_methods"


def route_after_run_methods(state: TopLevelState) -> str:
    """After planning-only stub: proceed to candidate-evidence gate."""
    return "fail" if _output(state).get("status") == "failed" else "block_candidates"


def route_after_candidate_gate(state: TopLevelState) -> str:
    """After candidate-evidence gate: re-route on rerun, continue to merge prep."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    if _output(state).get("status") == "methods_confirmed":
        return "run_methods"
    return "merge_prep"


def route_after_merge_prep(state: TopLevelState) -> str:
    """After merge planning: proceed to merge gate."""
    return "fail" if _output(state).get("status") == "failed" else "block_merge"


def route_after_merge_gate(state: TopLevelState) -> str:
    """After merge gate: continue to final gene-set gate or stop."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    return "block_final"


def route_after_final_gate(state: TopLevelState) -> str:
    """After final gene-set gate: commit, loop back on reject, or stop."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    if _output(state).get("status") == "candidates_accepted":
        return "merge_prep"
    return "commit"
