"""Immutable persistence boundary for assembly parameter recommendations.

The recommender owns domain decisions; this module only publishes their exact
inputs/outputs and returns compact, index-backed references suitable for a
checkpoint.  The same directory and manifest contract can be reused by later
stage-specific recommenders without sharing their recommendation logic.
"""

from __future__ import annotations

from typing import Any, Literal

from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_BASIS,
    ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    ArtifactRef,
)
from app.schemas.state import TopLevelState
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    publish_parameter_json,
    read_parameter_json,
    recover_parameter_json,
)


class ParameterPersistenceError(ValueError):
    """Raised when immutable parameter evidence is missing or inconsistent."""


_RECOMMENDATION_REF_TYPES = {
    "input_profile_ref": ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    "basis_snapshot_ref": ASSEMBLY_PARAMETER_BASIS,
    "model_request_ref": ASSEMBLY_PARAMETER_MODEL_REQUEST,
    "model_response_ref": ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    "recommendation_ref": ASSEMBLY_PARAMETER_RECOMMENDATION,
    "response_parse_ref": ASSEMBLY_PARAMETER_RESPONSE_PARSE,
}


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


def load_recommendation_bundle(
    state: TopLevelState,
    manifest_ref: ArtifactRef | dict[str, Any],
) -> dict[str, Any]:
    """Load the verified immutable facts bound by a recommendation manifest."""

    manifest = read_json_artifact(
        state,
        manifest_ref,
        expected_type=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    )
    if manifest.get("schema_version") not in {2, 3}:
        raise ParameterPersistenceError("unsupported recommendation manifest schema")
    refs = manifest.get("refs")
    if not isinstance(refs, dict):
        raise ParameterPersistenceError("recommendation manifest has no refs mapping")
    bundle: dict[str, Any] = {"manifest": manifest}
    for field, artifact_type in _RECOMMENDATION_REF_TYPES.items():
        ref = refs.get(field)
        if ref is None:
            raise ParameterPersistenceError(
                f"recommendation manifest is missing {field}"
            )
        bundle[field.removesuffix("_ref")] = read_json_artifact(
            state,
            ref,
            expected_type=artifact_type,
        )
    return bundle


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
