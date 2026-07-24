"""Immutable persistence boundary for assembly parameter recommendations.

The recommender owns domain decisions; this module only publishes their exact
inputs/outputs and returns compact, index-backed references suitable for a
checkpoint.  The same directory and manifest contract can be reused by later
stage-specific recommenders without sharing their recommendation logic.
"""

from __future__ import annotations

from typing import Any, Literal

from app.schemas.artifacts import ArtifactRef
from app.schemas.state import TopLevelState
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    publish_parameter_json,
    read_parameter_json,
    recover_parameter_json,
)


class ParameterPersistenceError(ValueError):
    """Raised when immutable parameter evidence is missing or inconsistent."""


def write_recommendation_json_artifact(
    state: TopLevelState,
    recommendation_id: int,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    """Publish one immutable recommendation-scoped JSON artifact."""

    return _write_json_artifact(
        state,
        scope="recommendation",
        identifier=recommendation_id,
        filename=filename,
        payload=payload,
        artifact_type=artifact_type,
        method_name=method_name,
    )


def write_attempt_json_artifact(
    state: TopLevelState,
    attempt_id: int,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    """Publish one immutable execution-attempt JSON artifact."""

    return _write_json_artifact(
        state,
        scope="attempt",
        identifier=attempt_id,
        filename=filename,
        payload=payload,
        artifact_type=artifact_type,
        method_name=method_name,
    )


def read_json_artifact(
    state: TopLevelState,
    artifact_ref: ArtifactRef | dict[str, Any],
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    """Read and verify one immutable JSON object through its v2 reference."""

    try:
        return read_parameter_json(
            state,
            artifact_ref,
            expected_type=expected_type,
        )
    except ParameterEvidenceError as exc:
        raise ParameterPersistenceError(str(exc)) from exc


def recover_recommendation_json_artifact(
    state: TopLevelState,
    recommendation_id: int,
    filename: str,
    *,
    expected_type: str,
    method_name: str,
) -> tuple[ArtifactRef, dict[str, Any]] | None:
    """Recover an already-published recommendation transform by logical path."""

    try:
        return recover_parameter_json(
            state,
            stage="assembly",
            scope="recommendation",
            identifier=recommendation_id,
            filename=filename,
            expected_type=expected_type,
            method_name=method_name,
        )
    except ParameterEvidenceError as exc:
        raise ParameterPersistenceError(str(exc)) from exc


def _write_json_artifact(
    state: TopLevelState,
    *,
    scope: Literal["recommendation", "attempt"],
    identifier: int,
    filename: str,
    payload: dict[str, Any],
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    try:
        return publish_parameter_json(
            state,
            stage="assembly",
            scope=scope,
            identifier=identifier,
            filename=filename,
            payload=payload,
            artifact_type=artifact_type,
            method_name=method_name,
        )
    except ParameterEvidenceError as exc:
        raise ParameterPersistenceError(str(exc)) from exc
