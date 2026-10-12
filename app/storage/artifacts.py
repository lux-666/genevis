"""Append-only index helpers for immutable Data Architecture v2 artifacts."""

from __future__ import annotations

import fcntl
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, TextIO
from uuid import NAMESPACE_URL, uuid5

from app.integrity import sha256_file
from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.storage.paths import (
    DataLayoutError,
    SessionPaths,
    ensure_run_layout,
    validate_data_layout,
    validate_path_component,
)
from app.storage.publish import publish_file

_ARTIFACT_NAMESPACE = uuid5(NAMESPACE_URL, "https://gene-agent.local/data-v2/artifact")
_INDEX_ID_FIELDS = (
    "workflow_step_id",
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


@dataclass(frozen=True)
class PublishedArtifact:
    """One explicitly published, indexed artifact and its durable path."""

    ref: ArtifactRef
    path: Path


@dataclass(frozen=True, slots=True)
class ArtifactIndexView:
    """One validated, read-only view of an artifact index.

    The view is intentionally scoped to one caller invocation.  It is safe to
    use for several by-id/by-path lookups without reparsing the append-only
    index, but it must be rebuilt after a publication.  Resolving a reference
    still checks the durable file size and SHA-256; the view only removes
    duplicate index parsing, never the integrity boundary.
    """

    paths: SessionPaths
    records: tuple[Mapping[str, Any], ...]
    by_id: Mapping[str, Mapping[str, Any]]
    by_path: Mapping[str, Mapping[str, Any]]

    def record_for_ref(self, value: ArtifactRef | Mapping[str, Any]) -> dict[str, Any]:
        ref = (
            value
            if isinstance(value, ArtifactRef)
            else ArtifactRef.model_validate(value)
        )
        record = self.by_id.get(ref.artifact_id)
        if record is None:
            raise DataLayoutError(
                f"artifact ref is not present in the index: {ref.artifact_id}"
            )
        indexed_ref = _artifact_ref_from_record(record)
        if indexed_ref != ref:
            raise DataLayoutError(
                f"artifact ref does not match its index record: {ref.artifact_id}"
            )
        # Do not leak the index's validated record object.  Callers may enrich
        # the returned snapshot for a report, but cannot mutate this view's
        # facts or bypass a later integrity check.
        return _thaw_mapping(record)

    def ref_for_path(
        self,
        path: Path | str,
        *,
        expected_type: str | None = None,
    ) -> ArtifactRef:
        path_ref = self.paths.to_ref(path)
        record = self.by_path.get(path_ref)
        if record is None:
            raise DataLayoutError(
                f"artifact path is not present in the index: {path_ref}"
            )
        ref = _artifact_ref_from_record(record)
        if expected_type is not None and ref.artifact_type != expected_type:
            raise DataLayoutError(
                f"artifact path has type {ref.artifact_type!r}, "
                f"expected {expected_type!r}: {path_ref}"
            )
        return ref

    def resolve(self, value: ArtifactRef | Mapping[str, Any]) -> Path:
        ref = (
            value
            if isinstance(value, ArtifactRef)
            else ArtifactRef.model_validate(value)
        )
        self.record_for_ref(ref)
        path = self.paths.resolve_ref(ref.path_ref)
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size != ref.size_bytes or sha256_file(path) != ref.sha256:
            raise DataLayoutError(f"artifact ref integrity mismatch: {ref.path_ref}")
        return path


def canonical_artifact_id(session_id: str, path_ref: str, sha256: str) -> str:
    """Return the stable opaque ID for one immutable run-local artifact."""
    identity = json.dumps(
        [session_id, path_ref, sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"art_{uuid5(_ARTIFACT_NAMESPACE, identity).hex}"


def read_artifact_records(
    data_dir: Path | str, session_id: str
) -> list[dict[str, Any]]:
    """Read and validate the run's v2 artifact index."""
    return [
        _thaw_mapping(record)
        for record in read_artifact_index_view(data_dir, session_id).records
    ]


def read_artifact_index_view(
    data_dir: Path | str, session_id: str
) -> ArtifactIndexView:
    """Read and validate the artifact index once for one caller scope."""

    paths = _session_paths(data_dir, session_id, require_layout=True)
    index_path = paths.artifact_index_path
    if not index_path.exists():
        empty: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
        return ArtifactIndexView(paths, (), empty, empty)
    with index_path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            loaded = _load_records(handle, paths)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    records = tuple(_freeze_mapping(record) for record in loaded)
    by_id = MappingProxyType({record["artifact_id"]: record for record in records})
    by_path = MappingProxyType({record["path_ref"]: record for record in records})
    return ArtifactIndexView(
        paths=paths,
        records=records,
        by_id=by_id,
        by_path=by_path,
    )


def publish_artifact_records(
    data_dir: Path | str,
    session_id: str,
    artifacts: list[ArtifactMeta],
) -> list[ArtifactRef]:
    """Move stage-work files into immutable storage and register them.

    Sources must live below the matching
    ``work/steps/<workflow_step_id>/<stage>/`` tree.  Files that have already
    crossed the publication boundary must be passed to
    :func:`register_artifact_records` instead.  Keeping the two operations
    distinct prevents a producer from overwriting a fixed occurrence-scoped
    artifact path and only discovering the conflict while appending the index
    row.
    """

    return [
        publish_artifact_record(data_dir, session_id, artifact).ref
        for artifact in artifacts
    ]


def publish_artifact_record(
    data_dir: Path | str,
    session_id: str,
    artifact: ArtifactMeta,
    *,
    destination: Path | str | None = None,
) -> PublishedArtifact:
    """Explicitly publish and index one artifact, returning its durable identity.

    Without an explicit destination, the source must live below the matching
    ``work/steps/<workflow_step_id>/<stage>/`` tree and is published to
    ``artifacts/steps/<workflow_step_id>/<stage>/outputs/``.  An explicit
    destination accepts a source from that same occurrence's work tree and
    must point below its ``artifacts/steps/<workflow_step_id>/<stage>/`` tree;
    this supports fixed contract paths without allowing cross-occurrence
    attribution or artifact-to-artifact rewrites.
    """

    paths = _session_paths(data_dir, session_id)
    ensure_run_layout(paths)
    if destination is None:
        published_path = _publish_work_backed_artifact(paths, artifact)
    else:
        published_path = _publish_to_destination(paths, artifact, destination)
    artifact.file_path = published_path
    refs = register_artifact_records(paths.run_dir, session_id, [artifact])
    if len(refs) != 1:
        raise DataLayoutError("artifact publication did not return exactly one ref")
    ref = refs[0]
    return PublishedArtifact(ref=ref, path=paths.resolve_ref(ref.path_ref))


def recover_published_artifact(
    data_dir: Path | str,
    session_id: str,
    destination: Path | str,
    *,
    stage: str,
    method_name: str,
    artifact_type: str,
    metadata: dict[str, Any] | None = None,
) -> PublishedArtifact | None:
    """Recover one deterministic artifact path and repair a missing index row.

    An existing destination is re-registered through the strict registration
    boundary, so metadata conflicts still fail closed.  If an index row exists
    but its file disappeared, resolution raises instead of treating the effect
    as absent and allowing a nondeterministic producer to run again.
    """

    paths = _session_paths(data_dir, session_id)
    ensure_run_layout(paths)
    destination_path = _artifact_destination_path(paths, destination)
    if destination_path.is_file():
        artifact = ArtifactMeta.from_file(
            destination_path,
            stage=stage,
            method_name=method_name,
            artifact_type=artifact_type,
            metadata=metadata,
        )
        [ref] = register_artifact_records(paths.run_dir, session_id, [artifact])
        return PublishedArtifact(ref=ref, path=destination_path)

    indexed_ref = _indexed_artifact_ref_for_path(
        paths,
        session_id,
        destination_path,
    )
    if indexed_ref is not None:
        index_view = read_artifact_index_view(paths.run_dir, session_id)
        resolved = index_view.resolve(indexed_ref)
        if indexed_ref.artifact_type != artifact_type:
            raise DataLayoutError(
                f"artifact path has type {indexed_ref.artifact_type!r}, "
                f"expected {artifact_type!r}: {indexed_ref.path_ref}"
            )
        return PublishedArtifact(ref=indexed_ref, path=resolved)
    return None


def publish_or_recover_work_artifact(
    data_dir: Path | str,
    session_id: str,
    source: Path | str,
    *,
    stage: str,
    method_name: str,
    artifact_type: str,
    metadata: dict[str, Any] | None = None,
) -> PublishedArtifact:
    """Publish a stage-work output or recover its deterministic snapshot.

    A collect node can be replayed after publication but before its checkpoint
    update.  In that crash window the mutable source has already been consumed,
    while the immutable destination is complete.  This helper reconstructs the
    same metadata from that destination and idempotently ensures its index row.
    """

    paths = _session_paths(data_dir, session_id)
    ensure_run_layout(paths)
    source_path = Path(source).expanduser().resolve(strict=False)
    destination = _work_artifact_destination(paths, source_path, stage)
    if not source_path.is_file():
        recovered = recover_published_artifact(
            paths.run_dir,
            session_id,
            destination,
            stage=stage,
            method_name=method_name,
            artifact_type=artifact_type,
            metadata=metadata,
        )
        if recovered is not None:
            return recovered
        raise FileNotFoundError(source_path)
    artifact = ArtifactMeta.from_file(
        source_path,
        stage=stage,
        method_name=method_name,
        artifact_type=artifact_type,
        metadata=metadata,
    )
    return publish_artifact_record(
        paths.run_dir,
        session_id,
        artifact,
        destination=destination,
    )


def register_artifact_records(
    data_dir: Path | str,
    session_id: str,
    artifacts: list[ArtifactMeta],
) -> list[ArtifactRef]:
    """Append already-published artifacts to the canonical v2 index.

    Returned refs preserve input order.  Re-registering the same path and
    bytes returns the existing ref; any path/hash or ID/record conflict fails
    closed.  This function never publishes or moves files.
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


def record_registered_artifacts(
    data_dir: Path | str,
    session_id: str,
    artifacts: Iterable[ArtifactRef | Mapping[str, Any] | ArtifactMeta],
    *,
    attempt_id: str | None = None,
) -> None:
    """Append causal registration facts only after a producer's success boundary.

    Index registration itself intentionally has no implicit Ledger side effect:
    a later collection failure may leave an index-only orphan. Successful
    producers call this one boundary once their semantic publish is complete.
    """

    from app.storage.ledger import Ledger

    ledger = Ledger(data_dir)
    for artifact in artifacts:
        if isinstance(artifact, ArtifactMeta):
            artifact = artifact.model_dump(mode="json")
        elif isinstance(artifact, ArtifactRef):
            artifact = artifact.model_dump(mode="json")
        ledger.record_artifact_registered(
            session_id,
            artifact,
            attempt_id=attempt_id,
        )


def resolve_artifact_ref(
    data_dir: Path | str,
    session_id: str,
    value: ArtifactRef | dict[str, Any],
    *,
    index_view: ArtifactIndexView | None = None,
) -> Path:
    """Verify an indexed artifact ref and return its durable absolute path."""

    ref = value if isinstance(value, ArtifactRef) else ArtifactRef.model_validate(value)
    return (index_view or read_artifact_index_view(data_dir, session_id)).resolve(ref)


def artifact_ref_for_path(
    data_dir: Path | str,
    session_id: str,
    path: Path | str,
    *,
    expected_type: str | None = None,
    index_view: ArtifactIndexView | None = None,
) -> ArtifactRef:
    """Return and verify the canonical indexed ref for one durable path."""

    view = index_view or read_artifact_index_view(data_dir, session_id)
    ref = view.ref_for_path(path, expected_type=expected_type)
    view.resolve(ref)
    return ref


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
    artifact_path = Path(artifact.file_path).expanduser().resolve(strict=False)
    if not artifact_path.is_file():
        raise FileNotFoundError(artifact_path)
    try:
        artifact_relative = artifact_path.relative_to(paths.artifacts_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"registered artifact must be published under {paths.artifacts_dir}: "
            f"{artifact_path}"
        ) from exc
    if (
        artifact_path == paths.artifact_index_path
        or artifact_relative.parts[0] == "index.jsonl"
    ):
        raise DataLayoutError("the artifact index cannot register itself")
    if paths.session_id in artifact_relative.parts:
        raise DataLayoutError(
            f"session_id must not be repeated below the run root: {artifact_path}"
        )

    workflow_step_id = _workflow_step_id_from_artifact_path(paths, artifact_path)
    metadata = dict(artifact.metadata or {})
    supplied_step_id = metadata.get("workflow_step_id")
    if supplied_step_id is not None and supplied_step_id != workflow_step_id:
        raise DataLayoutError(
            "artifact workflow_step_id does not match its step-scoped path"
        )
    artifact.metadata = {**metadata, "workflow_step_id": workflow_step_id}

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


def _publish_work_backed_artifact(paths: SessionPaths, artifact: ArtifactMeta) -> Path:
    """Publish a mutable stage work product into its immutable output tree."""
    source = Path(artifact.file_path).expanduser().resolve(strict=False)
    destination = _work_artifact_destination(paths, source, artifact.stage)
    return publish_file(source, destination)


def _work_artifact_destination(paths: SessionPaths, source: Path, stage: str) -> Path:
    """Return the one durable output path for a declared stage-work file."""

    workflow_step_id = _workflow_step_id_from_work_path(paths, source)
    stage_work_dir = paths.workflow_step_work_dir(workflow_step_id, stage)
    try:
        relative = source.relative_to(stage_work_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"work artifact is outside the declared stage workspace {stage_work_dir}: "
            f"{source}"
        ) from exc
    if paths.session_id in relative.parts:
        raise DataLayoutError(f"session_id must not be repeated below work/: {source}")
    return paths.workflow_step_artifact_path(
        workflow_step_id, stage, "outputs", *relative.parts
    )


def _publish_to_destination(
    paths: SessionPaths,
    artifact: ArtifactMeta,
    destination: Path | str,
) -> Path:
    source = Path(artifact.file_path).expanduser().resolve(strict=False)
    source_step_id = _require_work_source(paths, source)
    destination_path = _artifact_destination_path(paths, destination)
    destination_step_id = _workflow_step_id_from_artifact_path(paths, destination_path)
    if source_step_id != destination_step_id:
        raise DataLayoutError(
            "publication source and destination must belong to the same workflow step"
        )
    return publish_file(source, destination_path)


def _require_work_source(paths: SessionPaths, source: Path) -> str:
    try:
        relative = source.relative_to(paths.work_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"publication source must be below {paths.work_dir}: {source}"
        ) from exc
    if paths.session_id in relative.parts:
        raise DataLayoutError(f"session_id must not be repeated below work/: {source}")
    return _workflow_step_id_from_work_path(paths, source)


def _artifact_destination_path(
    paths: SessionPaths,
    destination: Path | str,
) -> Path:
    destination_path = Path(destination).expanduser().resolve(strict=False)
    try:
        relative = destination_path.relative_to(paths.artifacts_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"artifact destination must be below {paths.artifacts_dir}: "
            f"{destination_path}"
        ) from exc
    if not relative.parts or relative.parts[0] == "index.jsonl":
        raise DataLayoutError("the artifact index cannot be a publication destination")
    if paths.session_id in relative.parts:
        raise DataLayoutError(
            f"session_id must not be repeated below the run root: {destination_path}"
        )
    return destination_path


def _workflow_step_id_from_work_path(paths: SessionPaths, path: Path) -> str:
    try:
        relative = path.relative_to(paths.work_dir)
    except ValueError as exc:
        raise DataLayoutError(f"work path is outside {paths.work_dir}: {path}") from exc
    if len(relative.parts) < 3 or relative.parts[0] != "steps":
        raise DataLayoutError(
            "workflow-owned work paths must be below work/steps/<workflow_step_id>/"
        )
    return validate_path_component(relative.parts[1], "workflow_step_id")


def _workflow_step_id_from_artifact_path(paths: SessionPaths, path: Path) -> str:
    try:
        relative = path.relative_to(paths.artifacts_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"artifact path is outside {paths.artifacts_dir}: {path}"
        ) from exc
    if len(relative.parts) < 3 or relative.parts[0] != "steps":
        raise DataLayoutError(
            "workflow-owned artifacts must be below artifacts/steps/<workflow_step_id>/"
        )
    return validate_path_component(relative.parts[1], "workflow_step_id")


def _indexed_artifact_ref_for_path(
    paths: SessionPaths,
    session_id: str,
    path: Path | str,
    *,
    index_view: ArtifactIndexView | None = None,
) -> ArtifactRef | None:
    path_ref = paths.to_ref(path)
    view = index_view or read_artifact_index_view(paths.run_dir, session_id)
    record = view.by_path.get(path_ref)
    return None if record is None else _artifact_ref_from_record(record)


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
                f"malformed artifact index row {line_number}: "
                f"{paths.artifact_index_path}"
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
            f"artifact index row {line_number} is missing v2 fields: "
            f"{', '.join(missing)}"
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
        raise DataLayoutError(
            f"artifact index row {line_number} has invalid artifact_id"
        )
    if not isinstance(record["path_ref"], str):
        raise DataLayoutError(f"artifact index row {line_number} has invalid path_ref")
    resolved = paths.resolve_ref(record["path_ref"])
    try:
        resolved.relative_to(paths.artifacts_dir)
    except ValueError as exc:
        raise DataLayoutError(
            f"artifact index row {line_number} points outside artifacts/: "
            f"{record['path_ref']}"
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
    workflow_step_id = record.get("workflow_step_id")
    if not isinstance(workflow_step_id, str) or not workflow_step_id:
        raise DataLayoutError(
            f"artifact index row {line_number} has no workflow_step_id"
        )
    path_step_id = _workflow_step_id_from_artifact_path(paths, resolved)
    if path_step_id != workflow_step_id:
        raise DataLayoutError(
            f"artifact index row {line_number} workflow_step_id does not match path"
        )
    if not isinstance(record["sha256"], str) or not _SHA256_PATTERN.fullmatch(
        record["sha256"]
    ):
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
        raise DataLayoutError(
            f"artifact index row {line_number} has invalid size_bytes"
        )
    for field in ("artifact_type", "stage", "method_id", "created_at"):
        if not isinstance(record[field], str) or not record[field]:
            raise DataLayoutError(
                f"artifact index row {line_number} has invalid {field}"
            )
    for field in _INDEX_ID_FIELDS:
        if field in record and (
            not isinstance(record[field], (str, int)) or record[field] == ""
        ):
            raise DataLayoutError(
                f"artifact index row {line_number} has invalid {field}"
            )
    if "dependency_refs" in record and not isinstance(record["dependency_refs"], list):
        raise DataLayoutError(
            f"artifact index row {line_number} has invalid dependency_refs"
        )
    if "provenance" in record and not isinstance(record["provenance"], dict):
        raise DataLayoutError(
            f"artifact index row {line_number} has invalid provenance"
        )


def _require_same_registration(
    existing: dict[str, Any], candidate: dict[str, Any]
) -> None:
    existing_core = {
        key: value for key, value in existing.items() if key != "created_at"
    }
    candidate_core = {
        key: value for key, value in candidate.items() if key != "created_at"
    }
    if existing_core != candidate_core:
        raise FileExistsError(
            "immutable artifact path already has a conflicting registration: "
            f"{candidate['path_ref']}"
        )


def _artifact_ref_from_record(record: Mapping[str, Any]) -> ArtifactRef:
    return ArtifactRef.model_validate({field: record[field] for field in _REF_FIELDS})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen = _freeze_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("artifact index record must be a mapping")
    return frozen


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    thawed = _thaw_value(value)
    if not isinstance(thawed, dict):
        raise TypeError("artifact index record must be a mapping")
    return thawed


def _update_transient_meta(
    paths: SessionPaths,
    artifact: ArtifactMeta,
    record: dict[str, Any],
) -> None:
    artifact.artifact_id = record["artifact_id"]
    artifact.file_path = paths.resolve_ref(record["path_ref"])
    artifact.file_hash = record["sha256"]
    artifact.file_size_bytes = record["size_bytes"]
