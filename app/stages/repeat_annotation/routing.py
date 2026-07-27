"""Routing helpers for the repeat annotation stage."""

from __future__ import annotations

from typing import Literal

from app.schemas.state import TopLevelState
from app.stages.repeat_annotation.state import (
    RepeatAnnotationStageCursor,
    repeat_annotation_output,
)
from app.workflow import is_terminal_failure


def _output(state: TopLevelState) -> RepeatAnnotationStageCursor:
    return repeat_annotation_output(state)


def route_after_resolve(state: TopLevelState) -> Literal["fail", "select"]:
    """Route after resolving repeat annotation inputs."""
    return "fail" if is_terminal_failure(_output(state).get("status")) else "select"


def route_after_select(state: TopLevelState) -> Literal["fail", "library"]:
    """Route after selecting repeat annotation strategy."""
    return "fail" if is_terminal_failure(_output(state).get("status")) else "library"


def route_after_library_plan(state: TopLevelState) -> Literal["fail", "masking"]:
    """Route after planning repeat library handling."""
    return "fail" if is_terminal_failure(_output(state).get("status")) else "masking"


def route_after_masking_plan(
    state: TopLevelState,
) -> Literal["fail", "repeatmasker", "commit"]:
    """Route after planning repeat masking."""
    repeat_out = _output(state)
    if is_terminal_failure(repeat_out.get("status")):
        return "fail"
    if repeat_out.get("execution_enabled"):
        return "repeatmasker"
    return "commit"


def route_after_repeatmasker_collect(
    state: TopLevelState,
) -> Literal["fail", "block"]:
    """Route after collecting RepeatMasker artifacts."""
    return "fail" if is_terminal_failure(_output(state).get("status")) else "block"
