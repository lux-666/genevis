"""Read-only storage audit for one strict Data Architecture v2 run.

The audit deliberately stops at evidence collection.  It never creates a
directory, rewrites the artifact index, compacts checkpoints, or removes a
candidate.  Cleanup is represented only as a dry-run manifest.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from pydantic import BaseModel

from app.integrity import sha256_file
from app.storage.artifacts import read_artifact_records
from app.storage.paths import DataLayoutError, SessionPaths, validate_data_layout


_LAYER_NAMES = (
    "control",
    "ledger",
    "checkpoints",
    "artifacts",
    "reports",
    "work",
)
_MAX_REFERENCE_DOCUMENT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class _FileInfo:
    path: Path
    path_ref: str
    size_bytes: int
    sha256: str
    device: int
    inode: int

    def public(self) -> dict[str, Any]:
        return {
            "path_ref": self.path_ref,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def audit_run_storage(
    run_root: str | Path,
    *,
    largest_checkpoint_fields: int = 20,
) -> dict[str, Any]:
    """Audit a strict ``<data-root>/runs/<session-id>`` root without writes.

    Artifact-index syntax, path containment, and registered artifact integrity
    are trust boundaries and therefore fail closed with :class:`DataLayoutError`.
    An unsupported or corrupt checkpoint is diagnostic data rather than an
    artifact-index breach, so it is reported as a warning.
    """

    if (
        isinstance(largest_checkpoint_fields, bool)
        or not isinstance(largest_checkpoint_fields, int)
        or largest_checkpoint_fields < 0
    ):
        raise ValueError("largest_checkpoint_fields must be a non-negative integer")

    paths = SessionPaths.from_run_dir(run_root)
    validate_data_layout(paths.data_root)
    if not paths.run_dir.is_dir():
        raise DataLayoutError(f"v2 run root does not exist: {paths.run_dir}")

    warnings: list[str] = []
    layer_files: dict[str, list[_FileInfo]] = {}
    all_files: list[_FileInfo] = []
    for layer_name in _LAYER_NAMES:
        layer_dir = getattr(paths, f"{layer_name}_dir")
        files = _inventory_layer(paths, layer_name, layer_dir)
        layer_files[layer_name] = files
        all_files.extend(files)

    unexpected = _unexpected_run_entries(paths)
    if unexpected:
        warnings.append(
            "run root contains entries outside the six v2 layers: "
            + ", ".join(unexpected)
        )

    checkpoint, latest_checkpoint_states = _audit_checkpoints(
        paths,
        largest_checkpoint_fields=largest_checkpoint_fields,
    )
    warnings.extend(checkpoint["warnings"])

    records = read_artifact_records(paths.run_dir, paths.session_id)
    artifact_index = _audit_artifact_index(
        paths,
        records,
        artifact_files=layer_files["artifacts"],
        control_files=layer_files["control"],
        ledger_files=layer_files["ledger"],
        latest_checkpoint_states=latest_checkpoint_states,
        warnings=warnings,
    )

    layers = {
        name: {
            "file_count": len(files),
            "bytes": sum(file.size_bytes for file in files),
        }
        for name, files in layer_files.items()
    }
    duplicates = _duplicate_content(all_files)
    reclaimable = _reclaimable_candidates(
        layer_files["reports"],
        layer_files["work"],
    )

    return {
        "schema_version": 2,
        "session_id": paths.session_id,
        "run_root": str(paths.run_dir),
        "layers": layers,
        "totals": {
            "file_count": len(all_files),
            "bytes": sum(file.size_bytes for file in all_files),
        },
        "checkpoint": checkpoint,
        "duplicates": duplicates,
        "artifact_index": artifact_index,
        "reclaimable": reclaimable,
        "warnings": sorted(set(warnings)),
    }


def build_cleanup_manifest(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Build a pure dry-run manifest from an audit result.

    Only regenerable reports enter the automatic-cleanup candidate set.
    ``work/`` remains reported as retained because Data v2 reserves it for
    resume and operator-directed cleanup. This function performs no filesystem
    access.
    """

    if audit.get("schema_version") != 2:
        raise ValueError("cleanup manifest requires a schema v2 storage audit")
    run_root = audit.get("run_root")
    session_id = audit.get("session_id")
    reclaimable = audit.get("reclaimable")
    if not isinstance(run_root, str) or not isinstance(session_id, str):
        raise ValueError("storage audit is missing run identity")
    if not isinstance(reclaimable, Mapping):
        raise ValueError("storage audit is missing reclaimable candidates")
    candidates = reclaimable.get("files")
    if not isinstance(candidates, list):
        raise ValueError("storage audit has invalid reclaimable candidates")

    entries: list[dict[str, Any]] = []
    retained_work: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ValueError("cleanup candidate must be an object")
        entry = {
            key: candidate.get(key)
            for key in (
                "path_ref",
                "sha256",
                "size_bytes",
                "reason",
                "requires_output_verification",
            )
        }
        path_parts = Path(entry["path_ref"]).parts if isinstance(entry["path_ref"], str) else ()
        if (
            not isinstance(entry["path_ref"], str)
            or entry["path_ref"].startswith("/")
            or ".." in path_parts
            or not path_parts
            or path_parts[0] not in {"reports", "work"}
            or not isinstance(entry["sha256"], str)
            or len(entry["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in entry["sha256"])
            or isinstance(entry["size_bytes"], bool)
            or not isinstance(entry["size_bytes"], int)
            or entry["size_bytes"] < 0
            or not isinstance(entry["reason"], str)
            or not isinstance(entry["requires_output_verification"], bool)
        ):
            raise ValueError("cleanup candidate is not a normalized audited file")
        if (path_parts[0] == "work") != entry["requires_output_verification"]:
            raise ValueError("cleanup candidate has an invalid layer safety classification")
        if path_parts[0] == "work":
            retained_work.append(entry)
        else:
            entries.append(entry)

    entries.sort(key=lambda item: item["path_ref"])
    retained_work.sort(key=lambda item: item["path_ref"])
    return {
        "schema_version": 2,
        "session_id": session_id,
        "run_root": run_root,
        "dry_run": True,
        "deletion_performed": False,
        "candidate_file_count": len(entries),
        "candidate_bytes": sum(item["size_bytes"] for item in entries),
        "entries": entries,
        "retained_work_file_count": len(retained_work),
        "retained_work_bytes": sum(item["size_bytes"] for item in retained_work),
    }


def verify_checkpoint_free_artifact_retention(
    run_root: str | Path,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove that state artifact refs remain reachable without checkpoint roots."""

    paths = SessionPaths.from_run_dir(run_root)
    validate_data_layout(paths.data_root)
    if not paths.run_dir.is_dir():
        raise DataLayoutError(f"v2 run root does not exist: {paths.run_dir}")

    warnings: list[str] = []
    artifact_files = _inventory_layer(paths, "artifacts", paths.artifacts_dir)
    control_files = _inventory_layer(paths, "control", paths.control_dir)
    ledger_files = _inventory_layer(paths, "ledger", paths.ledger_dir)
    records = read_artifact_records(paths.run_dir, paths.session_id)
    artifact_index = _audit_artifact_index(
        paths,
        records,
        artifact_files=artifact_files,
        control_files=control_files,
        ledger_files=ledger_files,
        latest_checkpoint_states=[],
        warnings=warnings,
    )
    by_id = {str(record["artifact_id"]): record for record in records}
    by_path = {str(record["path_ref"]): record for record in records}
    required = _collect_references(
        state,
        paths=paths,
        by_id=by_id,
        by_path=by_path,
        context="terminal checkpoint state",
    )
    unreachable = {
        str(item["artifact_id"])
        for item in artifact_index["unreachable_artifacts"]
    }
    reachable = set(by_id).difference(unreachable)
    missing = sorted(required.difference(reachable))
    return {
        "session_id": paths.session_id,
        "required_artifact_ids": sorted(required),
        "reachable_artifact_ids": sorted(reachable),
        "missing_artifact_ids": missing,
        "warnings": sorted(set(warnings)),
    }


def _inventory_layer(paths: SessionPaths, layer_name: str, root: Path) -> list[_FileInfo]:
    if root.is_symlink() or not root.is_dir():
        raise DataLayoutError(f"missing or invalid v2 {layer_name}/ layer: {root}")

    files: list[_FileInfo] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(directory_names):
            candidate = directory_path / name
            if candidate.is_symlink():
                raise DataLayoutError(f"symlink is not allowed inside a v2 run: {candidate}")
            if paths.session_id in candidate.relative_to(paths.run_dir).parts[1:]:
                raise DataLayoutError(
                    f"session_id is repeated below the run root: {candidate}"
                )
        directory_names.sort()
        for name in sorted(file_names):
            candidate = directory_path / name
            if candidate.is_symlink():
                raise DataLayoutError(f"symlink is not allowed inside a v2 run: {candidate}")
            stat_before = candidate.stat()
            if not candidate.is_file():
                raise DataLayoutError(f"non-regular entry inside a v2 run: {candidate}")
            path_ref = paths.to_ref(candidate)
            if paths.session_id in Path(path_ref).parts[1:]:
                raise DataLayoutError(
                    f"session_id is repeated below the run root: {candidate}"
                )
            digest = sha256_file(candidate)
            stat_after = candidate.stat()
            identity_before = (
                stat_before.st_dev,
                stat_before.st_ino,
                stat_before.st_size,
                stat_before.st_mtime_ns,
            )
            identity_after = (
                stat_after.st_dev,
                stat_after.st_ino,
                stat_after.st_size,
                stat_after.st_mtime_ns,
            )
            if identity_before != identity_after:
                raise DataLayoutError(f"file changed while storage audit was reading it: {candidate}")
            files.append(
                _FileInfo(
                    path=candidate,
                    path_ref=path_ref,
                    size_bytes=stat_after.st_size,
                    sha256=digest,
                    device=stat_after.st_dev,
                    inode=stat_after.st_ino,
                )
            )
    return sorted(files, key=lambda file: file.path_ref)


def _unexpected_run_entries(paths: SessionPaths) -> list[str]:
    allowed = set(_LAYER_NAMES)
    unexpected: list[str] = []
    for entry in paths.run_dir.iterdir():
        if entry.is_symlink():
            raise DataLayoutError(f"symlink is not allowed at the v2 run root: {entry}")
        if entry.name not in allowed:
            unexpected.append(entry.name)
    return sorted(unexpected)


def _audit_checkpoints(
    paths: SessionPaths,
    *,
    largest_checkpoint_fields: int,
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    database = paths.checkpoint_db_path
    result: dict[str, Any] = {
        "database_present": database.is_file(),
        "database_ref": paths.to_ref(database),
        "row_count": 0,
        "parsed_row_count": 0,
        "largest_state_fields": [],
        "warnings": [],
    }
    if not database.is_file():
        return result, []

    wal_path = Path(f"{database}-wal")
    if wal_path.is_file() and wal_path.stat().st_size:
        result["warnings"].append(
            "checkpoint database has an uncheckpointed WAL; immutable read-only "
            "inspection was skipped to avoid mutating live SQLite sidecars"
        )
        return result, []

    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        serializer = JsonPlusSerializer(pickle_fallback=False)
    except Exception as exc:  # pragma: no cover - dependency is required in production
        result["warnings"].append(
            f"checkpoint serializer is unavailable: {type(exc).__name__}"
        )
        return result, []

    rows: list[tuple[Any, ...]] = []
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                "SELECT rowid, thread_id, checkpoint_ns, checkpoint_id, type, checkpoint "
                "FROM checkpoints ORDER BY rowid"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        result["warnings"].append(
            f"checkpoint database could not be parsed safely: {type(exc).__name__}: {exc}"
        )
        return result, []

    result["row_count"] = len(rows)
    field_maxima: dict[tuple[str, str, str], dict[str, Any]] = {}
    latest_by_thread: dict[tuple[str, str], tuple[int, Mapping[str, Any]]] = {}
    safe_types = {"null", "bytes", "bytearray", "json", "msgpack"}
    for rowid, thread_id, namespace, checkpoint_id, serialization_type, blob in rows:
        if serialization_type not in safe_types:
            result["warnings"].append(
                f"checkpoint {checkpoint_id} uses unsupported serialization type "
                f"{serialization_type!r}; it was not decoded"
            )
            continue
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        if not isinstance(blob, bytes):
            result["warnings"].append(
                f"checkpoint {checkpoint_id} has a non-byte payload; it was not decoded"
            )
            continue
        try:
            decoded = serializer.loads_typed((serialization_type, blob))
        except Exception as exc:
            result["warnings"].append(
                f"checkpoint {checkpoint_id} could not be decoded safely: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        state = _checkpoint_state(decoded)
        if state is None:
            result["warnings"].append(
                f"checkpoint {checkpoint_id} has no mapping state/channel_values"
            )
            continue
        result["parsed_row_count"] += 1
        key = (str(thread_id), str(namespace))
        latest_by_thread[key] = (int(rowid), state)
        for field, value in state.items():
            try:
                value_type, serialized = serializer.dumps_typed(value)
                size = len(serialized)
            except Exception as exc:
                result["warnings"].append(
                    f"checkpoint {checkpoint_id} field {field!r} could not be measured: "
                    f"{type(exc).__name__}"
                )
                continue
            field_key = (str(thread_id), str(namespace), str(field))
            previous = field_maxima.get(field_key)
            if previous is None:
                field_maxima[field_key] = {
                    "thread_id": str(thread_id),
                    "checkpoint_namespace": str(namespace),
                    "checkpoint_id": str(checkpoint_id),
                    "field": str(field),
                    "serialized_bytes": size,
                    "serialization_type": value_type,
                    "checkpoint_occurrences": 1,
                }
            else:
                previous["checkpoint_occurrences"] += 1
                if size > previous["serialized_bytes"]:
                    previous.update(
                        checkpoint_id=str(checkpoint_id),
                        serialized_bytes=size,
                        serialization_type=value_type,
                    )

    field_sizes = list(field_maxima.values())
    field_sizes.sort(
        key=lambda item: (
            -item["serialized_bytes"],
            item["field"],
            item["checkpoint_id"],
        )
    )
    result["largest_state_fields"] = field_sizes[:largest_checkpoint_fields]
    latest = [value[1] for _, value in sorted(latest_by_thread.items())]
    return result, latest


def _checkpoint_state(decoded: Any) -> Mapping[str, Any] | None:
    if not isinstance(decoded, Mapping):
        return None
    channel_values = decoded.get("channel_values")
    if isinstance(channel_values, Mapping):
        return channel_values
    state = decoded.get("state")
    if isinstance(state, Mapping):
        return state
    return decoded


def _audit_artifact_index(
    paths: SessionPaths,
    records: list[dict[str, Any]],
    *,
    artifact_files: list[_FileInfo],
    control_files: list[_FileInfo],
    ledger_files: list[_FileInfo],
    latest_checkpoint_states: list[Mapping[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    by_id = {record["artifact_id"]: record for record in records}
    by_path = {record["path_ref"]: record for record in records}
    inventory = {file.path_ref: file for file in artifact_files}
    index_ref = paths.to_ref(paths.artifact_index_path)
    index_snapshot = inventory.get(index_ref)
    index_exists_now = paths.artifact_index_path.is_file()
    if (index_snapshot is None) != (not index_exists_now):
        raise DataLayoutError("artifact index changed while storage audit was reading it")
    if index_snapshot is not None and (
        paths.artifact_index_path.stat().st_size != index_snapshot.size_bytes
        or sha256_file(paths.artifact_index_path) != index_snapshot.sha256
    ):
        raise DataLayoutError("artifact index changed while storage audit was reading it")

    for record in records:
        file = inventory.get(record["path_ref"])
        if file is None:
            raise DataLayoutError(
                f"artifact index points to a missing file: {record['path_ref']}"
            )
        if file.size_bytes != record["size_bytes"] or file.sha256 != record["sha256"]:
            raise DataLayoutError(
                f"artifact index integrity mismatch: {record['path_ref']}"
            )

    edges: dict[str, set[str]] = {artifact_id: set() for artifact_id in by_id}
    roots: set[str] = set()

    for record in records:
        artifact_id = record["artifact_id"]
        for dependency in record.get("dependency_refs", []):
            edges[artifact_id].update(
                _resolve_declared_reference(
                    dependency,
                    paths=paths,
                    by_id=by_id,
                    by_path=by_path,
                    context=f"artifact index dependency for {artifact_id}",
                    require_artifact=True,
                )
            )
        artifact_file = inventory[record["path_ref"]]
        document = _load_reference_document(artifact_file, warnings)
        if document is not None:
            edges[artifact_id].update(
                _collect_references(
                    document,
                    paths=paths,
                    by_id=by_id,
                    by_path=by_path,
                    context=record["path_ref"],
                )
            )
        if _is_artifact_root(record):
            roots.add(artifact_id)

    for file in (*control_files, *ledger_files):
        document = _load_reference_document(file, warnings)
        if document is not None:
            roots.update(
                _collect_references(
                    document,
                    paths=paths,
                    by_id=by_id,
                    by_path=by_path,
                    context=file.path_ref,
                )
            )
    for index, state in enumerate(latest_checkpoint_states):
        roots.update(
            _collect_references(
                state,
                paths=paths,
                by_id=by_id,
                by_path=by_path,
                context=f"latest checkpoint state {index}",
            )
        )

    reachable: set[str] = set()
    queue = deque(sorted(roots))
    while queue:
        artifact_id = queue.popleft()
        if artifact_id in reachable:
            continue
        reachable.add(artifact_id)
        queue.extend(sorted(edges.get(artifact_id, ())))

    unreachable = [
        {
            "artifact_id": record["artifact_id"],
            "path_ref": record["path_ref"],
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
            "artifact_type": record["artifact_type"],
            "reason": "not reachable from a v2 retention root",
        }
        for record in records
        if record["artifact_id"] not in reachable
    ]
    unreachable.sort(key=lambda item: item["path_ref"])

    indexed_paths = set(by_path)
    unindexed = [
        {
            **file.public(),
            "reason": "file exists under artifacts/ but has no artifact-index row",
        }
        for file in artifact_files
        if file.path_ref != index_ref
        and file.path_ref not in indexed_paths
    ]
    unindexed.sort(key=lambda item: item["path_ref"])

    return {
        "path_ref": index_ref,
        "record_count": len(records),
        "verified_record_count": len(records),
        "indexed_bytes": sum(record["size_bytes"] for record in records),
        "root_artifact_ids": sorted(roots),
        "reachable_record_count": len(reachable),
        "unreachable_record_count": len(unreachable),
        "unreachable_artifacts": unreachable,
        "unindexed_file_count": len(unindexed),
        "unindexed_artifacts": unindexed,
    }


def _is_artifact_root(record: Mapping[str, Any]) -> bool:
    artifact_type = str(record.get("artifact_type", ""))
    return bool(
        "review_request_id" in record
        or "review_id" in record
        or artifact_type.endswith(("_manifest", ".manifest"))
        or artifact_type.endswith((".evidence_export", "_evidence_export", ".export"))
    )


def _load_reference_document(
    file: _FileInfo,
    warnings: list[str],
) -> Any | None:
    suffix = file.path.suffix.lower()
    if suffix not in {".json", ".jsonl", ".yaml", ".yml"}:
        return None
    if file.size_bytes > _MAX_REFERENCE_DOCUMENT_BYTES:
        warnings.append(
            f"reference scan skipped oversized structured file {file.path_ref} "
            f"({file.size_bytes} bytes)"
        )
        return None
    try:
        text = file.path.read_text(encoding="utf-8")
        if suffix == ".json":
            return json.loads(text)
        if suffix == ".jsonl":
            values: list[Any] = []
            for line_number, line in enumerate(text.splitlines(), start=1):
                if line.strip():
                    try:
                        values.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"line {line_number}: {exc.msg}") from exc
            return values
        return yaml.safe_load(text)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        warnings.append(
            f"structured file could not be scanned for refs: {file.path_ref}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None


def _collect_references(
    value: Any,
    *,
    paths: SessionPaths,
    by_id: Mapping[str, Mapping[str, Any]],
    by_path: Mapping[str, Mapping[str, Any]],
    context: str,
) -> set[str]:
    found: set[str] = set()
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        if "artifact_id" in value and "path_ref" in value:
            found.update(
                _resolve_declared_reference(
                    value,
                    paths=paths,
                    by_id=by_id,
                    by_path=by_path,
                    context=context,
                    require_artifact=True,
                )
            )
        for key, nested in value.items():
            key_text = str(key)
            if key_text.endswith("_ref") or key_text.endswith("_refs"):
                found.update(
                    _resolve_declared_reference(
                        nested,
                        paths=paths,
                        by_id=by_id,
                        by_path=by_path,
                        context=f"{context}:{key_text}",
                        require_artifact="artifact" in key_text,
                    )
                )
            found.update(
                _collect_references(
                    nested,
                    paths=paths,
                    by_id=by_id,
                    by_path=by_path,
                    context=context,
                )
            )
        return found
    if isinstance(value, (list, tuple)):
        for nested in value:
            found.update(
                _collect_references(
                    nested,
                    paths=paths,
                    by_id=by_id,
                    by_path=by_path,
                    context=context,
                )
            )
    return found


def _resolve_declared_reference(
    value: Any,
    *,
    paths: SessionPaths,
    by_id: Mapping[str, Mapping[str, Any]],
    by_path: Mapping[str, Mapping[str, Any]],
    context: str,
    require_artifact: bool,
) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple)):
        found: set[str] = set()
        for nested in value:
            found.update(
                _resolve_declared_reference(
                    nested,
                    paths=paths,
                    by_id=by_id,
                    by_path=by_path,
                    context=context,
                    require_artifact=require_artifact,
                )
            )
        return found
    if isinstance(value, Mapping):
        artifact_id = value.get("artifact_id")
        path_ref = value.get("path_ref")
        if artifact_id is None and path_ref is None:
            return _collect_references(
                value,
                paths=paths,
                by_id=by_id,
                by_path=by_path,
                context=context,
            )
        if not isinstance(artifact_id, str) or not isinstance(path_ref, str):
            raise DataLayoutError(f"malformed ArtifactRef in {context}")
        paths.resolve_ref(path_ref)
        id_record = by_id.get(artifact_id)
        path_record = by_path.get(path_ref)
        if id_record is None or path_record is None or id_record is not path_record:
            raise DataLayoutError(f"dangling or conflicting ArtifactRef in {context}")
        for key, expected in (
            ("sha256", path_record["sha256"]),
            ("size_bytes", path_record["size_bytes"]),
            ("artifact_type", path_record["artifact_type"]),
        ):
            if key in value and value[key] != expected:
                raise DataLayoutError(f"ArtifactRef {key} mismatch in {context}")
        return {artifact_id}
    if not isinstance(value, str):
        if require_artifact:
            raise DataLayoutError(f"invalid artifact dependency reference in {context}")
        return set()
    if value in by_id:
        return {value}
    if value in by_path:
        return {by_path[value]["artifact_id"]}

    path_value = Path(value)
    if path_value.is_absolute():
        try:
            value = path_value.resolve(strict=False).relative_to(
                paths.run_dir.resolve(strict=False)
            ).as_posix()
        except ValueError:
            if require_artifact:
                raise DataLayoutError(
                    f"artifact dependency is outside run root in {context}: {value}"
                )
            return set()
        if value in by_path:
            return {by_path[value]["artifact_id"]}
    elif ".." in path_value.parts:
        paths.resolve_ref(value)  # raises with the canonical containment error
    if value.startswith("artifacts/") or value.startswith("art_") or require_artifact:
        raise DataLayoutError(f"dangling artifact reference in {context}: {value}")
    # A named ref may point to control/ or another run-local non-artifact owner.
    if "/" in value:
        paths.resolve_ref(value)
    return set()


def _duplicate_content(files: Iterable[_FileInfo]) -> dict[str, Any]:
    by_hash: dict[str, list[_FileInfo]] = defaultdict(list)
    for file in files:
        by_hash[file.sha256].append(file)

    groups: list[dict[str, Any]] = []
    for digest, matches in by_hash.items():
        if len(matches) < 2:
            continue
        matches.sort(key=lambda file: file.path_ref)
        physical_copies = len({(file.device, file.inode) for file in matches})
        size = matches[0].size_bytes
        groups.append(
            {
                "sha256": digest,
                "size_bytes": size,
                "file_count": len(matches),
                "physical_copy_count": physical_copies,
                "path_refs": [file.path_ref for file in matches],
                "logical_duplicate_bytes": size * (len(matches) - 1),
                "physical_duplicate_bytes": size * max(0, physical_copies - 1),
            }
        )
    groups.sort(key=lambda group: (-group["logical_duplicate_bytes"], group["sha256"]))
    return {
        "group_count": len(groups),
        "logical_duplicate_bytes": sum(
            group["logical_duplicate_bytes"] for group in groups
        ),
        "physical_duplicate_bytes": sum(
            group["physical_duplicate_bytes"] for group in groups
        ),
        "groups": groups,
    }


def _reclaimable_candidates(
    report_files: Iterable[_FileInfo],
    work_files: Iterable[_FileInfo],
) -> dict[str, Any]:
    reports = list(report_files)
    work = list(work_files)
    candidates = [
        {
            **file.public(),
            "reason": "derived report can be regenerated",
            "requires_output_verification": False,
        }
        for file in reports
    ]
    candidates.extend(
        {
            **file.public(),
            "reason": "mutable work candidate; verify every required output was published",
            "requires_output_verification": True,
        }
        for file in work
    )
    candidates.sort(key=lambda item: item["path_ref"])
    report_bytes = sum(file.size_bytes for file in reports)
    work_bytes = sum(file.size_bytes for file in work)
    return {
        "confirmed_report_bytes": report_bytes,
        "conditional_work_bytes": work_bytes,
        "candidate_bytes": report_bytes + work_bytes,
        "file_count": len(candidates),
        "files": candidates,
    }


__all__ = [
    "audit_run_storage",
    "build_cleanup_manifest",
    "verify_checkpoint_free_artifact_retention",
]
