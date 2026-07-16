"""Append-only index helpers for immutable Data Architecture v2 artifacts."""

from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any, TextIO
from uuid import NAMESPACE_URL, uuid5

from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.storage.integrity import sha256_file
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout, validate_data_layout
from app.storage.publish import publish_file


_ARTIFACT_NAMESPACE = uuid5(NAMESPACE_URL, "https://gene-agent.local/data-v2/artifact")
_INDEX_ID_FIELDS = (
    "recommendation_id",
    "attempt_id",
    "review_id",
    "review_request_id",
    "producer_event_id",
)
_REF_FIELDS = (
    "schema_version",
    "artifact_id",
    "path_ref",
    "sha256",
    "size_bytes",
    "artifact_type",
)
_INDEX_REQUIRED_FIELDS = (*_REF_FIELDS, "stage", "method_id", "created_at")
_ALLOWED_INDEX_FIELDS = frozenset(
    (*_INDEX_REQUIRED_FIELDS, *_INDEX_ID_FIELDS, "dependency_refs", "provenance")
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID_PATTERN = re.compile(r"^art_[0-9a-f]{32}$")


def canonical_artifact_id(session_id: str, path_ref: str, sha256: str) -> str:
    """Return the stable opaque ID for one immutable run-local artifact."""
    identity = json.dumps(
        [session_id, path_ref, sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"art_{uuid5(_ARTIFACT_NAMESPACE, identity).hex}"


def read_artifact_records(data_dir: Path | str, session_id: str) -> list[dict[str, Any]]:
    """Read and validate the run's v2 artifact index."""
    paths = _session_paths(data_dir, session_id, require_layout=True)
    index_path = paths.artifact_index_path
    if not index_path.exists():
        return []
    with index_path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            records = _load_records(handle, paths)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return records


def append_artifact_records(
    data_dir: Path | str,
    session_id: str,
    artifacts: list[ArtifactMeta],
) -> list[ArtifactRef]:
    """Publish artifacts and append their canonical v2 refs to the index.

    Returned refs preserve input order.  Re-registering the same path and
    bytes returns the existing ref; any path/hash or ID/record conflict fails
    closed.
    """
    paths = _session_paths(data_dir, session_id)
    ensure_run_layout(paths)
    candidates = [_registration_for(paths, artifact) for artifact in artifacts]

    index_path = paths.artifact_index_path
    index_path.parent.mkdir(parents=True, exist_ok=True)
    refs: list[ArtifactRef] = []
    with index_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            records = _load_records(handle, paths)
            by_path = {record["path_ref"]: record for record in records}
            by_id = {record["artifact_id"]: record for record in records}
            handle.seek(0, os.SEEK_END)

            selected_records: list[dict[str, Any]] = []
            new_records: list[dict[str, Any]] = []
            for candidate in candidates:
                existing = by_path.get(candidate["path_ref"])
                if existing is not None:
                    _require_same_registration(existing, candidate)
                    selected = existing
                else:
                    id_collision = by_id.get(candidate["artifact_id"])
                    if id_collision is not None:
                        raise DataLayoutError(
                            "artifact ID collision between distinct immutable records: "
                            f"{candidate['artifact_id']}"
                        )
                    by_path[candidate["path_ref"]] = candidate
                    by_id[candidate["artifact_id"]] = candidate
                    selected = candidate
                    new_records.append(candidate)
                selected_records.append(selected)

            for record in new_records:
                handle.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            for artifact, selected in zip(artifacts, selected_records, strict=True):
                _update_transient_meta(paths, artifact, selected)
                refs.append(_artifact_ref_from_record(selected))

            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return refs


def _session_paths(
    data_dir: Path | str,
    session_id: str,
    *,
    require_layout: bool = False,
) -> SessionPaths:
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    if require_layout:
        validate_data_layout(paths.data_root)
    return paths


def _registration_for(paths: SessionPaths, artifact: ArtifactMeta) -> dict[str, Any]:
    _publish_work_backed_artifact(paths, artifact)
    artifact_path = Path(artifact.file_path).expanduser().resolve(strict=False)
    if not artifact_path.is_file():
        raise FileNotFoundError(artifact_path)
    try:
        artifact_relative = artifact_path.relative_to(paths.artifacts_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"registered artifact must be published under {paths.artifacts_dir}: {artifact_path}"
        ) from exc
    if artifact_path == paths.artifact_index_path or artifact_relative.parts[0] == "index.jsonl":
        raise DataLayoutError("the artifact index cannot register itself")
    if paths.session_id in artifact_relative.parts:
        raise DataLayoutError(
            f"session_id must not be repeated below the run root: {artifact_path}"
        )

    path_ref = paths.to_ref(artifact_path)
    digest = sha256_file(artifact_path)
    size_bytes = artifact_path.stat().st_size
    if artifact.file_hash != digest:
        raise DataLayoutError(
            f"artifact changed after metadata was captured: {artifact_path}"
        )
    if artifact.file_size_bytes != size_bytes:
        raise DataLayoutError(
            f"artifact size changed after metadata was captured: {artifact_path}"
        )

    artifact_id = canonical_artifact_id(paths.session_id, path_ref, digest)
    dumped = artifact.model_dump(mode="json")
    metadata = dumped.get("metadata") or {}
    record: dict[str, Any] = {
        "schema_version": 2,
        "artifact_id": artifact_id,
        "path_ref": path_ref,
        "sha256": digest,
        "size_bytes": size_bytes,
        "artifact_type": artifact.artifact_type,
        "stage": artifact.stage,
        "method_id": artifact.method_name,
        "created_at": dumped["created_at"],
    }
    for field in _INDEX_ID_FIELDS:
        if metadata.get(field) is not None:
            record[field] = metadata[field]
    if metadata.get("dependency_refs") is not None:
        record["dependency_refs"] = metadata["dependency_refs"]
    if metadata.get("provenance") is not None:
        record["provenance"] = metadata["provenance"]
    return record


def _publish_work_backed_artifact(paths: SessionPaths, artifact: ArtifactMeta) -> None:
    """Publish a mutable stage work product into its immutable output tree."""
    source = Path(artifact.file_path).expanduser().resolve(strict=False)
    stage_work_dir = paths.stage_work_dir(artifact.stage)
    try:
        relative = source.relative_to(stage_work_dir)
    except ValueError:
        try:
            source.relative_to(paths.work_dir)
        except ValueError:
            return
        raise DataLayoutError(
            f"work artifact is outside the declared stage workspace {stage_work_dir}: {source}"
        )
    if paths.session_id in relative.parts:
        raise DataLayoutError(f"session_id must not be repeated below work/: {source}")
    destination = paths.artifact_path(artifact.stage, "outputs", *relative.parts)
    artifact.file_path = publish_file(source, destination)


def _load_records(handle: TextIO, paths: SessionPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DataLayoutError(
                f"malformed artifact index row {line_number}: {paths.artifact_index_path}"
            ) from exc
        _validate_record(record, paths, line_number)
        existing_path = by_path.get(record["path_ref"])
        if existing_path is not None:
            raise DataLayoutError(
                f"duplicate artifact path_ref in index: {record['path_ref']}"
            )
        existing_id = by_id.get(record["artifact_id"])
        if existing_id is not None:
            raise DataLayoutError(
                f"duplicate artifact_id in index: {record['artifact_id']}"
            )
        by_path[record["path_ref"]] = record
        by_id[record["artifact_id"]] = record
        records.append(record)
    return records


def _validate_record(record: Any, paths: SessionPaths, line_number: int) -> None:
    if not isinstance(record, dict):
        raise DataLayoutError(f"artifact index row {line_number} must be an object")
    missing = [field for field in _INDEX_REQUIRED_FIELDS if field not in record]
    if missing:
        raise DataLayoutError(
            f"artifact index row {line_number} is missing v2 fields: {', '.join(missing)}"
        )
    if record["schema_version"] != 2:
        raise DataLayoutError(f"artifact index row {line_number} is not schema v2")
    unexpected = sorted(set(record) - _ALLOWED_INDEX_FIELDS)
    if unexpected:
        raise DataLayoutError(
            f"artifact index row {line_number} contains non-metadata fields: "
            f"{', '.join(unexpected)}"
        )
    if not isinstance(record["artifact_id"], str) or not _ARTIFACT_ID_PATTERN.fullmatch(
        record["artifact_id"]
    ):
        raise DataLayoutError(f"artifact index row {line_number} has invalid artifact_id")
    if not isinstance(record["path_ref"], str):
        raise DataLayoutError(f"artifact index row {line_number} has invalid path_ref")
    resolved = paths.resolve_ref(record["path_ref"])
    try:
        resolved.relative_to(paths.artifacts_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"artifact index row {line_number} points outside artifacts/: {record['path_ref']}"
        ) from exc
    relative = resolved.relative_to(paths.run_dir)
    if len(relative.parts) < 2 or relative.parts[0] != "artifacts":
        raise DataLayoutError(f"artifact index row {line_number} has invalid path_ref")
    if resolved == paths.artifact_index_path:
        raise DataLayoutError("the artifact index cannot register itself")
    if paths.session_id in relative.parts[1:]:
        raise DataLayoutError(
            f"artifact index row {line_number} repeats session_id below the run root"
        )
    if not isinstance(record["sha256"], str) or not _SHA256_PATTERN.fullmatch(record["sha256"]):
        raise DataLayoutError(f"artifact index row {line_number} has invalid sha256")
    expected_id = canonical_artifact_id(
        paths.session_id,
        record["path_ref"],
        record["sha256"],
    )
    if record["artifact_id"] != expected_id:
        raise DataLayoutError(
            f"artifact index row {line_number} has a non-canonical artifact_id"
        )
    if (
        isinstance(record["size_bytes"], bool)
        or not isinstance(record["size_bytes"], int)
        or record["size_bytes"] < 0
    ):
        raise DataLayoutError(f"artifact index row {line_number} has invalid size_bytes")
    for field in ("artifact_type", "stage", "method_id", "created_at"):
        if not isinstance(record[field], str) or not record[field]:
            raise DataLayoutError(f"artifact index row {line_number} has invalid {field}")
    for field in _INDEX_ID_FIELDS:
        if field in record and (not isinstance(record[field], (str, int)) or record[field] == ""):
            raise DataLayoutError(f"artifact index row {line_number} has invalid {field}")
    if "dependency_refs" in record and not isinstance(record["dependency_refs"], list):
        raise DataLayoutError(f"artifact index row {line_number} has invalid dependency_refs")
    if "provenance" in record and not isinstance(record["provenance"], dict):
        raise DataLayoutError(f"artifact index row {line_number} has invalid provenance")


def _require_same_registration(existing: dict[str, Any], candidate: dict[str, Any]) -> None:
    existing_core = {key: value for key, value in existing.items() if key != "created_at"}
    candidate_core = {key: value for key, value in candidate.items() if key != "created_at"}
    if existing_core != candidate_core:
        raise FileExistsError(
            "immutable artifact path already has a conflicting registration: "
            f"{candidate['path_ref']}"
        )


def _artifact_ref_from_record(record: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef.model_validate({field: record[field] for field in _REF_FIELDS})


def _update_transient_meta(
    paths: SessionPaths,
    artifact: ArtifactMeta,
    record: dict[str, Any],
) -> None:
    artifact.artifact_id = record["artifact_id"]
    artifact.file_path = paths.resolve_ref(record["path_ref"])
    artifact.file_hash = record["sha256"]
    artifact.file_size_bytes = record["size_bytes"]
