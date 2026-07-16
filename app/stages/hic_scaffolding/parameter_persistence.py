"""Immutable evidence persistence for AutoHiC parameter recommendations."""

from __future__ import annotations

from typing import Any, Literal

from app.schemas.artifacts import ArtifactRef
from app.schemas.state import TopLevelState
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    publish_parameter_json,
    read_parameter_json,
)


class AutoHiCParameterPersistenceError(ValueError):
    """Raised when persisted AutoHiC parameter evidence is inconsistent."""


def write_autohic_parameter_artifact(
    state: TopLevelState,
    identifier: int,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    method_name: str,
    scope: Literal["recommendation", "attempt"] = "recommendation",
) -> ArtifactRef:
    """Publish one immutable recommendation- or attempt-scoped artifact."""

    try:
        return publish_parameter_json(
            state,
            stage="hic_scaffolding",
            scope=scope,
            identifier=identifier,
            filename=filename,
            payload=payload,
            artifact_type=artifact_type,
            method_name=method_name,
        )
    except ParameterEvidenceError as exc:
        raise AutoHiCParameterPersistenceError(str(exc)) from exc


def read_autohic_parameter_artifact(
    state: TopLevelState,
    value: ArtifactRef | dict[str, Any],
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    """Read and integrity-check one immutable AutoHiC parameter artifact."""

    try:
        return read_parameter_json(
            state,
            value,
            expected_type=expected_type,
        )
    except ParameterEvidenceError as exc:
        raise AutoHiCParameterPersistenceError(str(exc)) from exc
