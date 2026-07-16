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

from app.execution.artifacts import (
    publish_artifact_record,
    recover_published_artifact,
    resolve_artifact_ref,
)
from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.storage.paths import SessionPaths

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

    serialized = (
        json.dumps(dict(payload), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    paths, _session_id = _session_paths(state)
    stage_component = _safe_component(stage, label="stage")
    file_component = _safe_component(filename, label="filename")
    temporary = paths.work_path(
        stage_component,
        "parameter_persistence",
        f".{file_component}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(serialized)
    try:
        return publish_parameter_file(
            state,
            stage=stage,
            scope=scope,
            identifier=identifier,
            filename=filename,
            source=temporary,
            artifact_type=artifact_type,
            method_name=method_name,
        )
    finally:
        temporary.unlink(missing_ok=True)


def publish_parameter_file(
    state: Mapping[str, Any],
    *,
    stage: str,
    scope: ParameterEvidenceScope,
    identifier: int,
    filename: str,
    source: str | Path,
    artifact_type: str,
    method_name: str,
) -> ArtifactRef:
    """Move one work file into immutable, indexed parameter evidence.

    Only files below the current run's mutable ``work/`` tree may be consumed.
    Publishing is replay-safe: identical bytes reuse the existing ref, while a
    conflicting snapshot at the same logical path fails closed.
    """

    if scope not in {"recommendation", "attempt"}:
        raise ParameterEvidenceError(f"unsupported parameter evidence scope: {scope!r}")
    if type(identifier) is not int or identifier < 0:
        raise ParameterEvidenceError(f"{scope}_id must be a non-negative integer")
    stage_component = _safe_component(stage, label="stage")
    file_component = _safe_component(filename, label="filename")
    paths, session_id = _session_paths(state)
    raw_source_path = Path(source).expanduser()
    if raw_source_path.is_symlink():
        raise ParameterEvidenceError(
            "parameter evidence source must not be a symbolic link"
        )
    source_path = raw_source_path.resolve(strict=False)
    try:
        source_path.relative_to(paths.work_dir.resolve(strict=False))
    except ValueError as exc:
        raise ParameterEvidenceError(
            "parameter evidence source must be below the run work directory"
        ) from exc
    directory = "parameter_recommendations" if scope == "recommendation" else "attempts"
    destination = paths.artifact_path(
        stage_component,
        directory,
        f"{identifier:03d}",
        file_component,
    )
    artifact = ArtifactMeta.from_file(
        source_path,
        stage=stage_component,
        method_name=method_name,
        artifact_type=artifact_type,
        metadata={f"{scope}_id": identifier},
    )
    return publish_artifact_record(
        paths.run_dir,
        session_id,
        artifact,
        destination=destination,
    ).ref


def read_parameter_json(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_type: str | None = None,
) -> dict[str, Any]:
    """Resolve and integrity-check one immutable parameter-evidence object."""

    try:
        ref = (
            value
            if isinstance(value, ArtifactRef)
            else ArtifactRef.model_validate(value)
        )
    except ValueError as exc:
        raise ParameterEvidenceError("invalid parameter artifact reference") from exc
    if expected_type is not None and ref.artifact_type != expected_type:
        raise ParameterEvidenceError(
            f"parameter artifact type mismatch: expected {expected_type!r}, "
            f"got {ref.artifact_type!r}"
        )
    paths, session_id = _session_paths(state)
    try:
        path = resolve_artifact_ref(paths.run_dir, session_id, ref)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ParameterEvidenceError(
            f"parameter artifact is unreadable: {ref.path_ref}"
        ) from exc
    if not isinstance(payload, dict):
        raise ParameterEvidenceError(
            f"parameter artifact must contain a JSON object: {ref.path_ref}"
        )
    return payload


def recover_parameter_json(
    state: Mapping[str, Any],
    *,
    stage: str,
    scope: ParameterEvidenceScope,
    identifier: int,
    filename: str,
    expected_type: str,
    method_name: str,
) -> tuple[ArtifactRef, dict[str, Any]] | None:
    """Recover deterministic parameter evidence after a stale checkpoint.

    A complete immutable file is sufficient to repair a crash between the
    atomic move and index append.  An existing row is integrity-checked.  A
    row whose file disappeared still fails closed because its response bytes
    cannot be reconstructed from metadata alone.
    """

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
    try:
        recovered = recover_published_artifact(
            paths.run_dir,
            session_id,
            destination,
            stage=stage_component,
            method_name=method_name,
            artifact_type=expected_type,
            metadata={f"{scope}_id": identifier},
        )
    except (OSError, ValueError) as exc:
        raise ParameterEvidenceError(
            f"parameter artifact recovery failed: {paths.to_ref(destination)}"
        ) from exc
    if recovered is None:
        return None
    ref = recovered.ref
    payload = read_parameter_json(state, ref, expected_type=expected_type)
    return ref, payload


def _session_paths(state: Mapping[str, Any]) -> tuple[SessionPaths, str]:
    data_dir = state.get("data_dir")
    session_id = state.get("session_id")
    if not isinstance(data_dir, (str, Path)) or not isinstance(session_id, str):
        raise ParameterEvidenceError(
            "parameter evidence requires run data_dir and session_id"
        )
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    return paths, session_id


def _safe_component(value: str, *, label: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or len(path.parts) != 1 or value in {".", ".."}:
        raise ParameterEvidenceError(f"{label} must be one safe path component")
    return value
