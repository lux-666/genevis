"""Stage-neutral persistence for parameter-recommendation evidence.

Recommendation logic remains owned by each biological stage.  This module
owns only the shared Data Architecture v2 boundary: safe run-local paths,
immutable publication, artifact-index registration, and integrity-checked
reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping
from uuid import uuid4

from app.execution.artifacts import append_artifact_records
from app.schemas.artifacts import ArtifactMeta, ArtifactRef, file_sha256
from app.storage.paths import SessionPaths
from app.storage.publish import publish_file


ParameterEvidenceScope = Literal["recommendation", "attempt"]


class ParameterEvidenceError(ValueError):
    """Raised when shared parameter evidence is unsafe or inconsistent."""


def publish_parameter_json(
    state: Mapping[str, Any],
    *,
    stage: str,
    scope: ParameterEvidenceScope,
    identifier: int,
    filename: str,
    payload: Mapping[str, Any],
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    """Publish one immutable, indexed parameter-evidence JSON object."""

    if scope not in {"recommendation", "attempt"}:
        raise ParameterEvidenceError(f"unsupported parameter evidence scope: {scope!r}")
    if type(identifier) is not int or identifier < 0:
        raise ParameterEvidenceError(f"{scope}_id must be a non-negative integer")
    stage_component = _safe_component(stage, label="stage")
    file_component = _safe_component(filename, label="filename")
    paths, session_id = _session_paths(state)
    directory = "parameter_recommendations" if scope == "recommendation" else "attempts"
    destination = paths.artifact_path(
        stage_component,
        directory,
        f"{identifier:03d}",
        file_component,
    )
    serialized = (
        json.dumps(dict(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    temporary = paths.work_path(
        stage_component,
        "parameter_persistence",
        f".{destination.name}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(serialized)
    try:
        published = publish_file(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    artifact = ArtifactMeta.from_file(
        published,
        stage=stage_component,
        method_name=method_name,
        artifact_type=artifact_type,
        metadata={f"{scope}_id": identifier},
    )
    refs = append_artifact_records(paths.run_dir, session_id, [artifact])
    if len(refs) != 1:
        raise ParameterEvidenceError(
            "artifact index did not return exactly one canonical parameter-evidence ref"
        )
    return refs[0]


def read_parameter_json(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    """Resolve and integrity-check one immutable parameter-evidence object."""

    try:
        ref = value if isinstance(value, ArtifactRef) else ArtifactRef.model_validate(value)
    except ValueError as exc:
        raise ParameterEvidenceError("invalid parameter artifact reference") from exc
    if expected_type is not None and ref.artifact_type != expected_type:
        raise ParameterEvidenceError(
            f"parameter artifact type mismatch: expected {expected_type!r}, "
            f"got {ref.artifact_type!r}"
        )
    paths, _session_id = _session_paths(state)
    path = paths.resolve_ref(ref.path_ref)
    try:
        size = path.stat().st_size
        digest = file_sha256(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParameterEvidenceError(
            f"parameter artifact is unreadable: {ref.path_ref}"
        ) from exc
    if size != ref.size_bytes or digest != ref.sha256:
        raise ParameterEvidenceError(
            f"parameter artifact integrity mismatch: {ref.path_ref}"
        )
    if not isinstance(payload, dict):
        raise ParameterEvidenceError(
            f"parameter artifact must contain a JSON object: {ref.path_ref}"
        )
    return payload


def _session_paths(state: Mapping[str, Any]) -> tuple[SessionPaths, str]:
    data_dir = state.get("data_dir")
    session_id = state.get("session_id")
    if not isinstance(data_dir, (str, Path)) or not isinstance(session_id, str):
        raise ParameterEvidenceError("parameter evidence requires run data_dir and session_id")
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    return paths, session_id


def _safe_component(value: str, *, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or len(path.parts) != 1 or value in {".", ".."}:
        raise ParameterEvidenceError(f"{label} must be one safe path component")
    return value
