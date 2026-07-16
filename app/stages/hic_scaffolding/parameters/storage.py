"""Immutable evidence persistence for AutoHiC parameter recommendations."""

from __future__ import annotations

from typing import Any, Literal

from app.schemas.artifacts import ArtifactRef
from app.schemas.state import TopLevelState
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    publish_parameter_file,
    publish_parameter_json,
    read_parameter_json,
    recover_parameter_json,
)

AUTOHIC_PARAMETER_CONFIG = "hic_scaffolding.parameter.config"
AUTOHIC_PARAMETER_SCRIPT = "hic_scaffolding.parameter.script"
AUTOHIC_PARAMETER_COMMAND = "hic_scaffolding.parameter.command"


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


def recover_autohic_parameter_artifact(
    state: TopLevelState,
    identifier: int,
    filename: str,
    *,
    expected_type: str,
    method_name: str,
    scope: Literal["recommendation", "attempt"] = "recommendation",
) -> tuple[ArtifactRef, dict[str, Any]] | None:
    """Recover one completed AutoHiC parameter transform by logical path."""

    try:
        return recover_parameter_json(
            state,
            stage="hic_scaffolding",
            scope=scope,
            identifier=identifier,
            filename=filename,
            expected_type=expected_type,
            method_name=method_name,
        )
    except ParameterEvidenceError as exc:
        raise AutoHiCParameterPersistenceError(str(exc)) from exc


def publish_autohic_parameter_file(
    state: TopLevelState,
    identifier: int,
    filename: str,
    source: str,
    *,
    artifact_type: str,
    method_name: str,
    scope: Literal["recommendation", "attempt"] = "attempt",
) -> ArtifactRef:
    """Publish one generated AutoHiC parameter file without copying its bytes."""

    try:
        return publish_parameter_file(
            state,
            stage="hic_scaffolding",
            scope=scope,
            identifier=identifier,
            filename=filename,
            source=source,
            artifact_type=artifact_type,
            method_name=method_name,
        )
    except ParameterEvidenceError as exc:
        raise AutoHiCParameterPersistenceError(str(exc)) from exc
