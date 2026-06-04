"""Routing helpers for the gene annotation stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.gene_annotation.nodes import GENE_EXECUTABLE_METHODS

TERMINAL_STATES = frozenset({"blocked_human", "failed_terminal"})

# Built from GENE_EXECUTABLE_METHODS so adding a new method entry
# automatically includes its intermediate statuses here.
_METHOD_EXEC = GENE_EXECUTABLE_METHODS

_INTERMEDIATE_STATUSES = frozenset({"pending_execution"}).union(
    *(cfg["frozen_status"] for cfg in _METHOD_EXEC.values()),
    *(cfg["ran_status"] for cfg in _METHOD_EXEC.values()),
)


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("gene_annotation") or {}


def _supervision(state: TopLevelState) -> str:
    return str(state.get("supervision_state") or "")


def _next_pending_method(state: TopLevelState) -> str | None:
    """Return the next confirmed executable method not yet executed."""
    gene_out = _output(state)
    confirmed = set(gene_out.get("confirmed_methods") or [])
    executed = set(gene_out.get("executed_methods") or [])
    pending = (confirmed & set(_METHOD_EXEC.keys())) - executed
    return sorted(pending)[0] if pending else None


def _route_method_status(status: str) -> str | None:
    """Map a method-specific frozen/ran status to the next routing label."""
    for cfg in _METHOD_EXEC.values():
        if status == cfg["frozen_status"]:
            return f"{cfg['suffix']}_run"
        if status == cfg["ran_status"]:
            return f"{cfg['suffix']}_collect"
    return None


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
    # All intermediate execution statuses re-enter via the planning stub.
    if status in _INTERMEDIATE_STATUSES:
        return "run_methods"
    if status == "candidates_ready":
        return "block_candidates"
    if status == "candidates_accepted":
        return "merge_prep"
    if status == "merge_ready":
        return "block_merge"
    if status == "merge_accepted":
        return "evm_freeze"
    if status == "evm_frozen":
        return "evm_run"
    if status == "evm_ran":
        return "evm_collect"
    if status == "evm_collected":
        return "busco_freeze"
    if status == "busco_frozen":
        return "busco_run"
    if status == "busco_ran":
        return "busco_collect"
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
    """After planning-only stub: dispatch to next executable method or review."""
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    # Crash recovery: intermediate method status → route to the right step.
    routed = _route_method_status(status)
    if routed:
        return routed
    # pending_execution → find the next unexecuted method.
    if status == "pending_execution":
        next_method = _next_pending_method(state)
        if next_method:
            return f"{_METHOD_EXEC[next_method]['suffix']}_freeze"
    if status == "candidates_ready":
        return "block_candidates"
    return "block_candidates"


# --- per-method freeze / run / collect routing ----------------------------------
# Each freeze and run has a per-method router (trivial fail-check), built by a
# factory so adding a method needs no new wrapper. The returned callables carry a
# descriptive ``__name__`` so LangGraph traces read clearly.
# All collect nodes share a single router that can loop back to the next
# pending method's freeze when multiple executable methods are confirmed.

def route_after_freeze_for(suffix: str):
    """Return a named router: fail on failure, else advance to ``{suffix}_run``."""
    def router(state: TopLevelState) -> str:
        return "fail" if _output(state).get("status") == "failed" else f"{suffix}_run"

    router.__name__ = f"route_after_{suffix}_freeze"
    router.__qualname__ = router.__name__
    return router


def route_after_run_for(suffix: str):
    """Return a named router: fail on failure, else advance to ``{suffix}_collect``."""
    def router(state: TopLevelState) -> str:
        return "fail" if _output(state).get("status") == "failed" else f"{suffix}_collect"

    router.__name__ = f"route_after_{suffix}_run"
    router.__qualname__ = router.__name__
    return router


def route_after_method_collect(state: TopLevelState) -> str:
    """After any method collect: loop to next pending method or proceed to review."""
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "pending_execution":
        return "run_methods"
    return "block_candidates"


# --- EVM / BUSCO routing (Phase 7 task 05) ------------------------------------

def route_after_evm_freeze(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "evm_collected":
        return "busco_freeze"
    return "evm_run"


def route_after_evm_run(state: TopLevelState) -> str:
    return "fail" if _output(state).get("status") == "failed" else "evm_collect"


def route_after_evm_collect(state: TopLevelState) -> str:
    return "fail" if _output(state).get("status") == "failed" else "busco_freeze"


def route_after_busco_freeze(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "merge_accepted":
        return "block_final"
    return "busco_run"


def route_after_busco_run(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status == "merge_accepted":
        return "block_final"
    return "busco_collect"


def route_after_busco_collect(state: TopLevelState) -> str:
    return "block_final"


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
    """After merge gate: proceed to EVM merge execution or stop."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    return "evm_freeze"


def route_after_final_gate(state: TopLevelState) -> str:
    """After final gene-set gate: commit, loop back on reject, or stop."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    if _output(state).get("status") == "candidates_accepted":
        return "merge_prep"
    return "commit"
