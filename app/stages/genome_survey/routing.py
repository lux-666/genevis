"""Routing helpers for the genome survey stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.base import is_fail_status


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("genome_survey") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving survey inputs."""
    return "fail" if is_fail_status(_output(state).get("status")) else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route to the selected survey contract writer."""
    survey_out = _output(state)
    if is_fail_status(survey_out.get("status")):
        return "fail"
    mode = survey_out.get("selected_mode")
    if mode == "full":
        return "full"
    return "skip"


def route_after_full_plan(state: TopLevelState) -> str:
    """Route after planning full survey."""
    status = _output(state).get("status")
    if is_fail_status(status):
        return "fail"
    if status == "local_execution_planned":
        return "jellyfish"
    return "commit"


def route_after_jellyfish(state: TopLevelState) -> str:
    """Route after the Jellyfish method subgraph returns."""
    return "genomescope2" if _output(state).get("status") == "jellyfish_collected" else "fail"


def route_after_genomescope2(state: TopLevelState) -> str:
    """Route after the GenomeScope2 method subgraph returns."""
    return "write_result" if _output(state).get("status") == "genomescope2_collected" else "fail"


def route_after_plan(state: TopLevelState) -> str:
    """Route after writing a survey result or skip record."""
    return "fail" if is_fail_status(_output(state).get("status")) else "commit"
