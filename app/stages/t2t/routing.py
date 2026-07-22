"""Routing helpers for the T2T telomere-finishing stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.workflow import is_terminal_failure


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("t2t") or {}


def route_after_resolve(state: TopLevelState) -> str:
    output = _output(state)
    if is_terminal_failure(output.get("status")):
        return "fail"
    return "scan_assembly" if output.get("execution_enabled") else "report"


def route_after_assembly_scan(state: TopLevelState) -> str:
    return "fail" if is_terminal_failure(_output(state).get("status")) else "prepare_reads"


def route_after_read_prepare(state: TopLevelState) -> str:
    output = _output(state)
    if is_terminal_failure(output.get("status")):
        return "fail"
    return "scan_reads" if output.get("ont_reads_path") else "report"


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
