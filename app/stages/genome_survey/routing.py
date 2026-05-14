"""Routing helpers for the genome survey stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState


FAIL_STATUSES = {"failed", "blocked_env", "missing_tool"}


def _output(state: TopLevelState) -> dict:
    return (state.get("stage_outputs") or {}).get("genome_survey") or {}


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving survey inputs."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "select"


def route_after_select(state: TopLevelState) -> str:
    """Route to the selected survey contract writer."""
    survey_out = _output(state)
    if survey_out.get("status") in FAIL_STATUSES:
        return "fail"
    mode = survey_out.get("selected_mode")
    if mode == "full":
        return "full"
    if mode == "fallback_low_confidence":
        return "fallback"
    return "skip"


def route_after_full_plan(state: TopLevelState) -> str:
    """Route after planning full survey."""
    status = _output(state).get("status")
    if status in FAIL_STATUSES:
        return "fail"
    if status == "local_execution_planned":
        return "freeze_jellyfish"
    return "commit"


def route_after_freeze(state: TopLevelState) -> str:
    """Route after freezing a local attempt; short-circuit on preflight block."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "run"


def route_after_run(state: TopLevelState) -> str:
    """Route after a local attempt run."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "collect"


def route_after_collect_jellyfish(state: TopLevelState) -> str:
    """Route after Jellyfish artifact collection."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "freeze_genomescope2"


def route_after_collect_genomescope2(state: TopLevelState) -> str:
    """Route after GenomeScope2 artifact collection."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "write_result"


def route_after_plan(state: TopLevelState) -> str:
    """Route after writing a survey result or skip record."""
    return "fail" if _output(state).get("status") in FAIL_STATUSES else "commit"
