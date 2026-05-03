"""Routing helpers for the genome survey stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("genome_survey") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving survey inputs."""
    return "fail" if _output(state).get("status") == "failed" else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route to the selected survey contract writer."""
    survey_out = _output(state)
    if survey_out.get("status") == "failed":
        return "fail"
    mode = survey_out.get("selected_mode")
    if mode == "full":
        return "full"
    if mode == "fallback_low_confidence":
        return "fallback"
    return "skip"


def route_after_plan(state: TopLevelState) -> str:
    """Route after writing a survey result or skip record."""
    return "fail" if _output(state).get("status") == "failed" else "commit"
