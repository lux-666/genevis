"""Create-only freeze transaction for immutable workflow inputs.

Intake owns mutable drafts.  Execution owns an immutable manifest, run config,
and machine-dependency snapshot committed together by ``frozen_inputs.json``.
The marker is deliberately written last: without it, a partial set of control
files is not executable and must be repaired explicitly rather than overwritten.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.integrity import sha256_file
from app.schemas.manifest import InputManifest, validate_manifest
from app.schemas.run_config import RunConfig
from app.storage.paths import SessionPaths, ensure_run_layout

FROZEN_INPUTS_SCHEMA_VERSION = 1
INPUT_MANIFEST_SCHEMA_VERSION = 1
RUN_CONFIG_SCHEMA_VERSION = 1

_FILE_KEYS = ("input_manifest", "run_config", "dependency_manifest")
_FILE_NAMES = {
    "input_manifest": "input_manifest.yaml",
    "run_config": "run_config.json",
    "dependency_manifest": "dependency_manifest.json",
}
_FILE_SCHEMA_VERSIONS = {
    "input_manifest": INPUT_MANIFEST_SCHEMA_VERSION,
    "run_config": RUN_CONFIG_SCHEMA_VERSION,
    "dependency_manifest": 1,
}
_RUN_CONFIG_PATH_FIELDS = (
    ("assembly", "hifi_reads_ref"),
    ("assembly", "hic_reads_ref"),
    ("assembly_review", "busco", "lineage_path"),
    ("assembly_review", "related_species_reference_dir"),
    ("t2t", "chromosome_list_ref"),
    ("t2t", "assembly_fasta_ref"),
)
_RUN_CONFIG_PATH_LIST_FIELDS = (("t2t", "ont_read_refs"),)


class FrozenInputError(ValueError):
    """Base error for frozen input creation or verification."""


class FrozenInputConflictError(FrozenInputError):
    """Raised when an existing frozen session is asked to change inputs."""


class FrozenInputIntegrityError(FrozenInputError):
    """Raised when the marker or one committed file is missing or changed."""


class FrozenInputValidationError(FrozenInputError):
    """Raised when a draft cannot become a valid frozen input contract."""


class FrozenExecutionActiveError(FrozenInputError):
    """Raised when another process owns the frozen execution lock."""


class FrozenExecutionRecoveryError(FrozenInputError):
    """Raised when partial execution has no resumable checkpoint."""


class FrozenInputFile(BaseModel):
    """Integrity metadata for one canonical control input."""

    model_config = ConfigDict(extra="forbid")

    path_ref: str
    sha256: str
    size_bytes: int = Field(ge=0)
    schema_version: int = Field(ge=1)

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


class FrozenInputsMarker(BaseModel):
    """Commit marker which makes a session eligible for execution."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = FROZEN_INPUTS_SCHEMA_VERSION
    session_id: str
    parent_session_id: str | None = None
    frozen_at: str
    files: dict[str, FrozenInputFile]

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != FROZEN_INPUTS_SCHEMA_VERSION:
            raise ValueError(
                "unsupported frozen input marker schema "
                f"{value!r}; expected {FROZEN_INPUTS_SCHEMA_VERSION}"
            )
        return value


@dataclass(frozen=True)
class FrozenInputBundle:
    """Verified immutable control inputs ready for an executor to consume."""

    paths: SessionPaths
    marker: FrozenInputsMarker
    manifest: InputManifest
    run_config: RunConfig

    def file_sha256(self, key: str) -> str:
        try:
            return self.marker.files[key].sha256
        except KeyError as exc:
            raise FrozenInputIntegrityError(
                f"frozen input marker is missing {key!r}"
            ) from exc


def freeze_session_inputs(
    paths: SessionPaths,
    *,
    manifest_draft: str | Path | InputManifest | Mapping[str, Any],
    run_config_draft: str | Path | RunConfig | Mapping[str, Any],
    config_path: str | Path | None = None,
    parent_session_id: str | None = None,
    selected_stages: Sequence[str] | None = None,
    selected_methods: Mapping[str, str | Sequence[str]] | None = None,
    preflight_runner: Any | None = None,
    probe_environments: bool | None = None,
) -> FrozenInputBundle:
    """Validate and commit one run's execution inputs exactly once.

    The manifest/run-config source can be a draft file or an already parsed
    mapping/model.  File-based drafts have their relative paths resolved once
    against their own directory.  In-memory drafts must already use absolute
    paths, preventing accidental dependence on the process working directory.

    Repeating the same freeze returns the already verified bundle.  Any partial
    publication, a changed draft, a changed parent identity, or a tampered
    frozen file fails closed and never overwrites historical control files.
    """

    ensure_run_layout(paths)
    _validate_parent_session_id(paths, parent_session_id)
    manifest = _load_manifest_draft(manifest_draft)
    run_config = _load_run_config_draft(run_config_draft)
    dependency_stages, dependency_methods = _dependency_scope(
        run_config,
        selected_stages=selected_stages,
        selected_methods=selected_methods,
    )
    manifest_bytes = _manifest_bytes(manifest)
    run_config_bytes = _run_config_bytes(run_config)

    lock_path = paths.control_dir / ".frozen-inputs.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if paths.frozen_inputs_path.exists():
                bundle = validate_frozen_inputs(paths)
                _verify_idempotent_request(
                    bundle,
                    manifest_bytes=manifest_bytes,
                    run_config_bytes=run_config_bytes,
                    parent_session_id=parent_session_id,
                    selected_stages=dependency_stages,
                    selected_methods=dependency_methods,
                )
                return bundle

            partial = _partial_freeze_paths(paths)
            if partial:
                raise FrozenInputIntegrityError(
                    "refusing to overwrite incomplete frozen input transaction: "
                    + ", ".join(str(path) for path in partial)
                )

            # Kept inside the lock so a successful freeze binds the exact
            # dependency snapshot used to calculate the commit marker.
            from app.execution.dependency_snapshot import (
                capture_dependency_snapshot,
                dependency_snapshot_bytes,
            )

            dependency_snapshot = capture_dependency_snapshot(
                config_path=config_path,
                stages=dependency_stages,
                methods=dependency_methods,
                preflight_runner=preflight_runner,
                probe_environments=probe_environments,
            )
            dependency_bytes = dependency_snapshot_bytes(dependency_snapshot)
            marker = _build_marker(
                paths,
                parent_session_id=parent_session_id,
                input_manifest_bytes=manifest_bytes,
                run_config_bytes=run_config_bytes,
                dependency_manifest_bytes=dependency_bytes,
            )
            _publish_freeze_transaction(
                paths,
                {
                    "input_manifest": manifest_bytes,
                    "run_config": run_config_bytes,
                    "dependency_manifest": dependency_bytes,
                    "frozen_inputs": _marker_bytes(marker),
                },
            )
            return validate_frozen_inputs(paths)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def validate_frozen_inputs(paths: SessionPaths) -> FrozenInputBundle:
    """Verify a complete frozen input transaction without mutating it."""

    marker = _load_marker(paths)
    if marker.session_id != paths.session_id:
        raise FrozenInputIntegrityError(
            "frozen input marker session_id does not match its run directory"
        )
    if set(marker.files) != set(_FILE_KEYS):
        raise FrozenInputIntegrityError(
            "frozen input marker must contain exactly " + ", ".join(_FILE_KEYS)
        )

    for key in _FILE_KEYS:
        _verify_frozen_file(paths, marker, key)

    manifest = _load_frozen_manifest(paths.input_manifest_path)
    if manifest.session_id != paths.session_id:
        raise FrozenInputIntegrityError(
            "frozen input manifest session_id does not match its run directory"
        )
    run_config = _load_frozen_run_config(paths.run_config_path)

    # Parse the dependency manifest using its own strict schema.  The local
    # import keeps storage usable without importing execution during module load.
    from app.execution.dependency_snapshot import load_dependency_snapshot

    try:
        load_dependency_snapshot(
            paths.dependency_manifest_path,
            expected_sha256=marker.files["dependency_manifest"].sha256,
        )
    except ValueError as exc:
        raise FrozenInputIntegrityError(
            f"frozen dependency manifest is invalid: {exc}"
        ) from exc
    return FrozenInputBundle(
        paths=paths,
        marker=marker,
        manifest=manifest,
        run_config=run_config,
    )


def has_frozen_input_contract(paths: SessionPaths) -> bool:
    """Whether any new-format frozen-control file is present.

    ``run_config.json`` alone is intentionally not a signal: legacy session
    layouts wrote that name before this transaction existed.  A new executor
    sees any of the unique new-format files and must validate the full marker
    rather than fall back to live machine configuration.
    """

    return any(
        path.exists()
        for path in (
            paths.input_manifest_path,
            paths.dependency_manifest_path,
            paths.frozen_inputs_path,
        )
    )


@contextmanager
def frozen_execution_lock(paths: SessionPaths) -> Iterator[None]:
    """Own one complete frozen execution attempt or fail immediately."""

    paths.control_dir.mkdir(parents=True, exist_ok=True)
    with paths.execution_lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FrozenExecutionActiveError(
                f"session {paths.session_id} is already executing"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def frozen_execution_lock_held(paths: SessionPaths) -> bool:
    """Return whether another open file description owns execution."""

    paths.control_dir.mkdir(parents=True, exist_ok=True)
    with paths.execution_lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def frozen_execution_progress(
    paths: SessionPaths,
) -> Literal["fresh", "checkpoint", "partial"]:
    """Classify durable execution evidence without consulting mutable inputs."""

    checkpoint_path = paths.checkpoint_db_path
    if checkpoint_path.exists():
        try:
            connection = sqlite3.connect(
                f"file:{checkpoint_path}?mode=ro",
                uri=True,
            )
            try:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'checkpoints'"
                ).fetchone()
                if table is not None:
                    checkpoint = connection.execute(
                        "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                        (paths.session_id,),
                    ).fetchone()
                    if checkpoint is not None:
                        return "checkpoint"
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise FrozenExecutionRecoveryError(
                f"checkpoint database is unreadable: {checkpoint_path}: {exc}"
            ) from exc
        return "partial"

    for evidence_path in (
        paths.ledger_events_path,
        paths.artifact_index_path,
        paths.report_path("current_status.json"),
        paths.report_path("session_summary.json"),
    ):
        try:
            if evidence_path.is_file() and evidence_path.stat().st_size > 0:
                return "partial"
        except OSError as exc:
            raise FrozenExecutionRecoveryError(
                f"execution evidence is unreadable: {evidence_path}: {exc}"
            ) from exc
    return "fresh"


def _dependency_scope(
    run_config: RunConfig,
    *,
    selected_stages: Sequence[str] | None,
    selected_methods: Mapping[str, str | Sequence[str]] | None,
) -> tuple[list[str], list[str]]:
    requested = list(run_config.requested_stages or [])
    stages = (
        [str(stage).strip() for stage in selected_stages]
        if selected_stages is not None
        else requested or [str(stage) for stage in run_config.local_exec]
    )
    if any(not stage for stage in stages):
        raise FrozenInputValidationError(
            "selected dependency stages must not contain empty names"
        )
    if len(stages) != len(set(stages)):
        raise FrozenInputValidationError(
            "selected dependency stages must not contain duplicates"
        )
    if requested and stages != requested:
        raise FrozenInputValidationError(
            "selected dependency stages differ from run-config requested_stages"
        )
    missing_local = sorted(set(run_config.local_exec).difference(stages))
    if missing_local:
        raise FrozenInputValidationError(
            "selected dependency stages omit local_exec stages: "
            + ", ".join(missing_local)
        )

    methods: list[str] = []
    for stage, selected in (selected_methods or {}).items():
        normalized_stage = str(stage).strip()
        if normalized_stage not in stages:
            raise FrozenInputValidationError(
                f"selected methods reference an unselected stage: {stage}"
            )
        values = [selected] if isinstance(selected, str) else list(selected)
        for method in values:
            normalized = str(method).strip()
            if not normalized:
                raise FrozenInputValidationError(
                    f"selected methods for {stage} contain an empty method"
                )
            if not normalized.startswith(f"{normalized_stage}."):
                raise FrozenInputValidationError(
                    f"selected method {normalized!r} does not belong to "
                    f"stage {normalized_stage!r}"
                )
            if normalized not in methods:
                methods.append(normalized)
    return stages, methods


def _validate_parent_session_id(
    paths: SessionPaths, parent_session_id: str | None
) -> None:
    if parent_session_id is None:
        return
    if parent_session_id == paths.session_id:
        raise FrozenInputValidationError(
            "parent_session_id must differ from session_id"
        )
    try:
        SessionPaths(paths.data_root, parent_session_id)
    except ValueError as exc:
        raise FrozenInputValidationError(
            f"invalid parent_session_id: {parent_session_id!r}"
        ) from exc


def _load_manifest_draft(
    source: str | Path | InputManifest | Mapping[str, Any],
) -> InputManifest:
    payload, source_dir = _load_yaml_draft(source, label="manifest draft")
    try:
        manifest = InputManifest.model_validate(payload)
    except ValidationError as exc:
        raise FrozenInputValidationError(
            f"manifest draft failed validation: {exc}"
        ) from exc
    normalized_assets: list[dict[str, Any]] = []
    for asset in manifest.assets:
        asset_payload = asset.model_dump(mode="json")
        if asset.paired_reads:
            normalized_pairs = [
                {
                    "r1": _normalize_draft_path(
                        pair.r1,
                        source_dir,
                        field="manifest paired-read R1 path",
                    ),
                    "r2": _normalize_draft_path(
                        pair.r2,
                        source_dir,
                        field="manifest paired-read R2 path",
                    ),
                }
                for pair in asset.paired_reads
            ]
            # ``paired_reads`` is the source of truth for Hi-C pairing.  Keep
            # the compatibility ``paths`` projection in exactly the same order.
            asset_payload["paired_reads"] = normalized_pairs
            asset_payload["paths"] = [
                path for pair in normalized_pairs for path in (pair["r1"], pair["r2"])
            ]
        else:
            asset_payload["paths"] = [
                _normalize_draft_path(path, source_dir, field="manifest asset path")
                for path in asset.paths
            ]
        normalized_assets.append(asset_payload)
    try:
        manifest = InputManifest.model_validate(
            {**manifest.model_dump(mode="json"), "assets": normalized_assets}
        )
    except ValidationError as exc:
        raise FrozenInputValidationError(
            f"normalized manifest draft failed validation: {exc}"
        ) from exc
    report = validate_manifest(manifest)
    if not report["valid"]:
        raise FrozenInputValidationError(
            "manifest draft cannot be frozen: " + "; ".join(report["errors"])
        )
    return manifest


def _load_run_config_draft(
    source: str | Path | RunConfig | Mapping[str, Any],
) -> RunConfig:
    payload, source_dir = _load_json_draft(source, label="run-config draft")
    normalized = json.loads(json.dumps(payload))
    for field_path in _RUN_CONFIG_PATH_FIELDS:
        _normalize_payload_path(normalized, field_path, source_dir)
    for field_path in _RUN_CONFIG_PATH_LIST_FIELDS:
        _normalize_payload_path_list(normalized, field_path, source_dir)
    try:
        return RunConfig.model_validate(normalized)
    except ValidationError as exc:
        raise FrozenInputValidationError(
            f"run-config draft failed validation: {exc}"
        ) from exc


def _load_yaml_draft(
    source: str | Path | InputManifest | Mapping[str, Any], *, label: str
) -> tuple[dict[str, Any], Path | None]:
    if isinstance(source, InputManifest):
        return source.model_dump(mode="json"), None
    if isinstance(source, Mapping):
        return dict(source), None
    source_path = Path(source).expanduser().resolve(strict=False)
    try:
        payload = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FrozenInputValidationError(f"{label} is not readable: {exc}") from exc
    if not isinstance(payload, dict):
        raise FrozenInputValidationError(f"{label} root must be a mapping")
    return payload, source_path.parent


def _load_json_draft(
    source: str | Path | RunConfig | Mapping[str, Any], *, label: str
) -> tuple[dict[str, Any], Path | None]:
    if isinstance(source, RunConfig):
        return source.model_dump(mode="json", exclude_none=True), None
    if isinstance(source, Mapping):
        return dict(source), None
    source_path = Path(source).expanduser().resolve(strict=False)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrozenInputValidationError(
            f"{label} is not readable JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise FrozenInputValidationError(f"{label} root must be an object")
    return payload, source_path.parent


def _normalize_draft_path(
    path_value: str, source_dir: Path | None, *, field: str
) -> str:
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return str(path.resolve(strict=False))
    if source_dir is None:
        raise FrozenInputValidationError(
            f"{field} must be absolute when freezing an in-memory draft: {path_value}"
        )
    return str((source_dir / path).resolve(strict=False))


def _normalize_payload_path(
    payload: dict[str, Any], field_path: tuple[str, ...], source_dir: Path | None
) -> None:
    parent: Any = payload
    for key in field_path[:-1]:
        if not isinstance(parent, dict) or key not in parent:
            return
        parent = parent[key]
    if not isinstance(parent, dict):
        return
    key = field_path[-1]
    value = parent.get(key)
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise FrozenInputValidationError(
            f"run-config {'.'.join(field_path)} must be a non-empty path"
        )
    parent[key] = _normalize_draft_path(
        value, source_dir, field=f"run-config {'.'.join(field_path)}"
    )


def _normalize_payload_path_list(
    payload: dict[str, Any], field_path: tuple[str, ...], source_dir: Path | None
) -> None:
    parent: Any = payload
    for key in field_path[:-1]:
        if not isinstance(parent, dict) or key not in parent:
            return
        parent = parent[key]
    if not isinstance(parent, dict):
        return
    key = field_path[-1]
    values = parent.get(key)
    if values is None:
        return
    if not isinstance(values, list):
        raise FrozenInputValidationError(
            f"run-config {'.'.join(field_path)} must be a list of paths"
        )
    parent[key] = [
        _normalize_draft_path(
            value,
            source_dir,
            field=f"run-config {'.'.join(field_path)}[{index}]",
        )
        if isinstance(value, str) and value.strip()
        else _raise_invalid_path_list_member(field_path, index)
        for index, value in enumerate(values)
    ]


def _raise_invalid_path_list_member(field_path: tuple[str, ...], index: int) -> str:
    raise FrozenInputValidationError(
        f"run-config {'.'.join(field_path)}[{index}] must be a non-empty path"
    )


def _manifest_bytes(manifest: InputManifest) -> bytes:
    return yaml.safe_dump(
        manifest.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")


def _run_config_bytes(run_config: RunConfig) -> bytes:
    return (
        json.dumps(
            run_config.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _build_marker(
    paths: SessionPaths,
    *,
    parent_session_id: str | None,
    input_manifest_bytes: bytes,
    run_config_bytes: bytes,
    dependency_manifest_bytes: bytes,
) -> FrozenInputsMarker:
    values = {
        "input_manifest": input_manifest_bytes,
        "run_config": run_config_bytes,
        "dependency_manifest": dependency_manifest_bytes,
    }
    files = {
        key: FrozenInputFile(
            path_ref=paths.to_ref(_path_for_key(paths, key)),
            sha256=_sha256_bytes(content),
            size_bytes=len(content),
            schema_version=_FILE_SCHEMA_VERSIONS[key],
        )
        for key, content in values.items()
    }
    return FrozenInputsMarker(
        session_id=paths.session_id,
        parent_session_id=parent_session_id,
        frozen_at=datetime.now(timezone.utc).isoformat(),
        files=files,
    )


def _marker_bytes(marker: FrozenInputsMarker) -> bytes:
    return (
        json.dumps(
            marker.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _publish_freeze_transaction(
    paths: SessionPaths, payloads: dict[str, bytes]
) -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix=".freeze-inputs-", dir=paths.control_dir))
    try:
        staged = {
            key: _stage_bytes(
                temp_dir / _FILE_NAMES.get(key, "frozen_inputs.json"), value
            )
            for key, value in payloads.items()
        }
        for key in _FILE_KEYS:
            _publish_staged_create_only(staged[key], _path_for_key(paths, key))
        _fsync_directory(paths.control_dir)
        _publish_staged_create_only(staged["frozen_inputs"], paths.frozen_inputs_path)
        _fsync_directory(paths.control_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _stage_bytes(path: Path, content: bytes) -> Path:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _publish_staged_create_only(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise FrozenInputConflictError(
            f"frozen input destination already exists: {destination}"
        ) from exc


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_idempotent_request(
    bundle: FrozenInputBundle,
    *,
    manifest_bytes: bytes,
    run_config_bytes: bytes,
    parent_session_id: str | None,
    selected_stages: Sequence[str],
    selected_methods: Sequence[str],
) -> None:
    if bundle.marker.parent_session_id != parent_session_id:
        raise FrozenInputConflictError(
            "frozen session parent_session_id differs from this freeze request"
        )
    expected = {
        "input_manifest": manifest_bytes,
        "run_config": run_config_bytes,
    }
    for key, content in expected.items():
        if bundle.file_sha256(key) != _sha256_bytes(content):
            raise FrozenInputConflictError(
                f"frozen session {key} differs from this freeze request"
            )

    from app.execution.dependency_snapshot import load_dependency_snapshot

    snapshot = load_dependency_snapshot(
        bundle.paths.dependency_manifest_path,
        expected_sha256=bundle.file_sha256("dependency_manifest"),
    )
    if snapshot.selected_stages != list(selected_stages):
        raise FrozenInputConflictError(
            "frozen session selected stages differ from this freeze request"
        )
    if snapshot.selected_methods != sorted(selected_methods):
        raise FrozenInputConflictError(
            "frozen session selected methods differ from this freeze request"
        )


def _partial_freeze_paths(paths: SessionPaths) -> list[Path]:
    return [
        path
        for path in (
            paths.input_manifest_path,
            paths.run_config_path,
            paths.dependency_manifest_path,
            paths.frozen_inputs_path,
        )
        if path.exists()
    ]


def _load_marker(paths: SessionPaths) -> FrozenInputsMarker:
    try:
        raw = json.loads(paths.frozen_inputs_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FrozenInputIntegrityError(
            f"frozen input marker is missing: {paths.frozen_inputs_path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise FrozenInputIntegrityError(
            f"frozen input marker is invalid: {paths.frozen_inputs_path}: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise FrozenInputIntegrityError("frozen input marker root must be an object")
    try:
        return FrozenInputsMarker.model_validate(raw)
    except ValidationError as exc:
        raise FrozenInputIntegrityError(
            f"frozen input marker failed validation: {exc}"
        ) from exc


def _verify_frozen_file(
    paths: SessionPaths, marker: FrozenInputsMarker, key: str
) -> None:
    metadata = marker.files[key]
    expected_path = _path_for_key(paths, key)
    expected_ref = paths.to_ref(expected_path)
    if metadata.path_ref != expected_ref:
        raise FrozenInputIntegrityError(
            f"frozen marker {key} path_ref is not canonical: {metadata.path_ref!r}"
        )
    if metadata.schema_version != _FILE_SCHEMA_VERSIONS[key]:
        raise FrozenInputIntegrityError(
            f"frozen marker {key} has unsupported schema version "
            f"{metadata.schema_version!r}"
        )
    if not expected_path.is_file() or expected_path.is_symlink():
        raise FrozenInputIntegrityError(
            f"frozen input file is missing: {expected_path}"
        )
    if sha256_file(expected_path) != metadata.sha256:
        raise FrozenInputIntegrityError(f"frozen input hash mismatch: {expected_path}")
    if expected_path.stat().st_size != metadata.size_bytes:
        raise FrozenInputIntegrityError(f"frozen input size mismatch: {expected_path}")


def _load_frozen_manifest(path: Path) -> InputManifest:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FrozenInputIntegrityError(
            f"frozen manifest is unreadable: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise FrozenInputIntegrityError("frozen manifest root must be a mapping")
    try:
        return InputManifest.model_validate(payload)
    except ValidationError as exc:
        raise FrozenInputIntegrityError(
            f"frozen manifest failed validation: {exc}"
        ) from exc


def _load_frozen_run_config(path: Path) -> RunConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrozenInputIntegrityError(
            f"frozen run config is unreadable JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise FrozenInputIntegrityError("frozen run config root must be an object")
    try:
        return RunConfig.model_validate(payload)
    except ValidationError as exc:
        raise FrozenInputIntegrityError(
            f"frozen run config failed validation: {exc}"
        ) from exc


def _path_for_key(paths: SessionPaths, key: str) -> Path:
    if key == "input_manifest":
        return paths.input_manifest_path
    if key == "run_config":
        return paths.run_config_path
    if key == "dependency_manifest":
        return paths.dependency_manifest_path
    raise KeyError(key)


def _sha256_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()
