"""Routing helpers for the function annotation stage."""

from __future__ import annotations

from app.schemas.state import TopLevelState
from app.stages.function_annotation.state import (
    FunctionAnnotationCursor,
    function_annotation_output,
)


def _output(state: TopLevelState) -> FunctionAnnotationCursor:
    return function_annotation_output(state)


def route_after_resolve(state: TopLevelState) -> str:
    """Route after resolving protein inputs."""
    return "fail" if _output(state).get("status") == "failed" else "methods"


def route_after_method_plan(state: TopLevelState) -> str:
    """Route after planning function annotation methods."""
    return "fail" if _output(state).get("status") == "failed" else "provenance"


def route_after_provenance_validation(state: TopLevelState) -> str:
    """Route after validating database provenance — dispatch to diamond \
execution or crash-recovery."""
    status = _output(state).get("status")
    if status == "failed":
        return "fail"
    if status in {"provenance_validated", "diamond_frozen", "diamond_ran"}:
        return (
            "diamond" if _output(state).get("execution_mode") == "local" else "commit"
        )
    if status == "diamond_completed":
        return "commit"
    return "commit"


def route_after_diamond(state: TopLevelState) -> str:
    return "fail" if _output(state).get("status") == "failed" else "commit"
