"""Immutable evidence persistence for T2T parameter recommendations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from app.schemas.artifacts import ArtifactRef
from app.schemas.state import TopLevelState
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    publish_parameter_json,
    read_parameter_json,
    recover_parameter_json,
)


class T2TParameterPersistenceError(ValueError):
    pass


def write_t2t_parameter_artifact(
    state: TopLevelState,
    identifier: int,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    method_name: str,
    scope: Literal["recommendation", "attempt"] = "recommendation",
) -> ArtifactRef:
    try:
        return publish_parameter_json(
            state,
            stage="t2t",
            scope=scope,
            identifier=identifier,
            filename=filename,
            payload=payload,
            artifact_type=artifact_type,
            method_name=method_name,
        )
    except ParameterEvidenceError as exc:
        raise T2TParameterPersistenceError(str(exc)) from exc


def read_t2t_parameter_artifact(
    state: TopLevelState,
    value: ArtifactRef | Mapping[str, object],
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    try:
        return read_parameter_json(state, value, expected_type=expected_type)
    except ParameterEvidenceError as exc:
        raise T2TParameterPersistenceError(str(exc)) from exc


def recover_t2t_parameter_artifact(
    state: TopLevelState,
    identifier: int,
    filename: str,
    *,
    expected_type: str,
    method_name: str,
) -> tuple[ArtifactRef, dict[str, Any]] | None:
    try:
        return recover_parameter_json(
            state,
            stage="t2t",
            scope="recommendation",
            identifier=identifier,
            filename=filename,
            expected_type=expected_type,
            method_name=method_name,
        )
    except ParameterEvidenceError as exc:
        raise T2TParameterPersistenceError(str(exc)) from exc
