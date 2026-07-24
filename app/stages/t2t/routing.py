"""Routing helpers for the T2T telomere-finishing stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.workflow import is_terminal_failure


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("t2t") or {}


def route_after_enter(state: TopLevelState) -> str:
    status = _output(state).get("status")
    if is_terminal_failure(status):
        return "fail"
    if status == "parameters_approved":
        return "prepare_parameters"
    if status == "parameters_ready":
        return "scan_assembly"
    if status == "motif_resolved":
        output = _output(state)
        if not output.get("ont_read_refs"):
            return "prepare_reads"
        return "recommend"
    if status == "assembly_scanned":
        return "prepare_reads"
    if status == "reads_prepared":
        return "scan_reads"
    if status in {"reads_scanned", "alignment_frozen", "alignment_ran"}:
        return "alignment"
    return "resolve"


def route_after_resolve(state: TopLevelState) -> str:
    output = _output(state)
    if is_terminal_failure(output.get("status")):
        return "fail"
    if not output.get("execution_enabled"):
        return "report"
    if not output.get("ont_read_refs"):
        return "prepare_reads"
    return "resolve_motif"


def route_after_motif_resolution(state: TopLevelState) -> str:
    output = _output(state)
    return "recommend" if output.get("status") == "motif_resolved" else "fail"


def route_after_parameter_recommendation(state: TopLevelState) -> str:
    output = _output(state)
    if output.get("status") == "blocked_human":
        return "block"
    return "fail"


def route_after_parameter_freeze(state: TopLevelState) -> str:
    return (
        "fail" if is_terminal_failure(_output(state).get("status")) else "scan_assembly"
    )


def route_after_assembly_scan(state: TopLevelState) -> str:
    return (
        "fail" if is_terminal_failure(_output(state).get("status")) else "prepare_reads"
    )


def route_after_read_prepare(state: TopLevelState) -> str:
    output = _output(state)
    if is_terminal_failure(output.get("status")):
        return "fail"
    return "scan_reads" if output.get("ont_read_inputs") else "report"


def route_after_read_scan(state: TopLevelState) -> str:
    return "fail" if is_terminal_failure(_output(state).get("status")) else "alignment"


def route_after_alignment(state: TopLevelState) -> str:
    output = _output(state)
    if is_terminal_failure(output.get("status")):
        return "fail"
    return "apply_patches" if output.get("status") == "candidates_built" else "report"


def route_after_patches(state: TopLevelState) -> str:
    output = _output(state)
    if is_terminal_failure(output.get("status")):
        return "fail"
    return "validate_patched" if output.get("patched_assembly_ref") else "report"


def route_after_validation(state: TopLevelState) -> str:
    return "fail" if is_terminal_failure(_output(state).get("status")) else "report"


def route_after_report(state: TopLevelState) -> str:
    output = _output(state)
    if is_terminal_failure(output.get("status")):
        return "fail"
    return "gate_review" if output.get("status") == "blocked_human" else "commit"


def route_after_gate(state: TopLevelState) -> str:
    return "block" if state.get("supervision_state") == "blocked_human" else "commit"
