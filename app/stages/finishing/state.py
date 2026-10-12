"""Validated evidence requirements for each finishing-loop transition."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, ConfigDict, Field, TypeAdapter, with_config
from typing_extensions import NotRequired, TypedDict

from app.schemas.artifacts import ArtifactRefJson


@with_config(ConfigDict(extra="forbid", strict=True))
class FinishingCursorFields(TypedDict):
    phase: Literal["evaluate", "advise", "blocked", "run", "done", "failed"]
    source_ref: ArtifactRefJson
    config_ref: ArtifactRefJson
    history_refs: list[ArtifactRefJson]
    attempt: Annotated[int, Field(ge=0)]
    quast_ref: NotRequired[ArtifactRefJson]
    quast_execution_ref: NotRequired[ArtifactRefJson]
    reference_based: NotRequired[bool]
    recommendation_ref: NotRequired[ArtifactRefJson]
    decision_ref: NotRequired[ArtifactRefJson]
    candidate_ref: NotRequired[ArtifactRefJson]
    frozen_ref: NotRequired[ArtifactRefJson]
    parameters: NotRequired[dict[str, Any] | None]
    action: NotRequired[Literal["run", "accept", "keep_source", "stop"]]
    selected_attempt: NotRequired[int | None]
    execution_context: NotRequired[dict[str, Any]]


def _validate_phase(cursor: FinishingCursorFields) -> FinishingCursorFields:
    required = {
        "evaluate": (),
        "advise": ("quast_ref", "reference_based"),
        "blocked": ("quast_ref", "recommendation_ref"),
        "run": ("quast_ref", "recommendation_ref", "decision_ref", "parameters"),
        "done": ("decision_ref", "candidate_ref", "action"),
        "failed": (),
    }[cursor["phase"]]
    missing = [key for key in required if cursor.get(key) is None]
    if missing:
        raise ValueError(f"finishing {cursor['phase']} lacks evidence: {missing}")
    return cursor


FinishingCursor = Annotated[FinishingCursorFields, AfterValidator(_validate_phase)]
FINISHING_CURSOR_ADAPTER = TypeAdapter(FinishingCursor)
