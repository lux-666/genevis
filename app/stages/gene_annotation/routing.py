"""Routing helpers for the gene annotation stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.gene_annotation.nodes import (
    GENE_EXECUTABLE_METHODS,
    GFFREAD_LIFECYCLE,
)

TERMINAL_STATES = frozenset({"blocked_human", "failed_terminal"})

# Built from GENE_EXECUTABLE_METHODS so adding a new method entry
# automatically includes its intermediate statuses here.
_METHOD_EXEC = GENE_EXECUTABLE_METHODS

_INTERMEDIATE_STATUSES = frozenset(
    {
        "pending_execution",
        GFFREAD_LIFECYCLE.frozen_status,
        GFFREAD_LIFECYCLE.ran_status,
        *(cfg["frozen_status"] for cfg in _METHOD_EXEC.values()),
        *(cfg["ran_status"] for cfg in _METHOD_EXEC.values()),
    }
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
    """Map a method-specific frozen/ran status to its lifecycle subgraph."""
    for cfg in _METHOD_EXEC.values():
        if status == cfg["frozen_status"]:
            return cfg["suffix"]
        if status == cfg["ran_status"]:
            return cfg["suffix"]
    if status in {GFFREAD_LIFECYCLE.frozen_status, GFFREAD_LIFECYCLE.ran_status}:
        return "materialize"
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
    # All intermediate execution statuses re-enter via the execution preparer.
    if status in _INTERMEDIATE_STATUSES:
        return "run_methods"
    if status == "candidates_ready":
        return "block_candidates"
    if status == "candidates_accepted":
        return "merge_prep"
    if status == "merge_ready":
        return "block_merge"
    if status in {"miniprot_collected", "augustus_collected", "evm_outputs_collected"}:
        return "materialize"
    if status == "merge_accepted":
        return "evm"
    if status in {"evm_frozen", "evm_ran"}:
        return "evm"
    if status == "evm_collected":
        return "busco"
    if status in {"busco_frozen", "busco_ran"}:
        return "busco"
    if status == "review_passed":
        return "commit"
    return "fail"


# --- after-gate routing (one per gate block node) -------------------------------

def route_after_method_planning_gate(state: TopLevelState) -> str:
    """After method-planning gate: continue to executable method preparation or stop."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    return "run_methods"


def route_after_run_methods(state: TopLevelState) -> str:
    """After method preparation: dispatch to next executable method or review."""
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
            return _METHOD_EXEC[next_method]["suffix"]
    if status == "candidates_ready":
        return "block_candidates"
    return "block_candidates"


def route_after_candidate_method(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if status in {"failed", "materialization_failed"}:
        return "fail"
    return "materialize" if status in {"miniprot_collected", "augustus_collected"} else "fail"


def route_after_materialization(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if status in {"failed", "materialization_failed"}:
        return "fail"
    if status == "pending_execution":
        return "run_methods"
    if status == "candidates_ready":
        return "block_candidates"
    if status == "evm_collected":
        return "busco"
    return "fail"


def route_after_evm(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if status in {"failed", "materialization_failed"}:
        return "fail"
    if status == "evm_outputs_collected":
        return "materialize"
    if status == "evm_collected":
        return "busco"
    return "fail"


def route_after_busco(state: TopLevelState) -> str:
    return "fail" if _output(state).get("status") == "failed" else "block_final"


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
    return "evm"


def route_after_final_gate(state: TopLevelState) -> str:
    """After final gene-set gate: commit, loop back on reject, or stop."""
    if _supervision(state) in TERMINAL_STATES:
        return "end" if _supervision(state) == "blocked_human" else "fail"
    if _output(state).get("status") == "candidates_accepted":
        return "merge_prep"
    return "commit"
