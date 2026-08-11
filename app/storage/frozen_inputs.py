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
from app.schemas.manifest import (
    AssetEntryInference,
    Compression,
    Format,
    InputAssetSpec,
    InputManifest,
    Layout,
    Platform,
    validate_manifest,
)
from app.schemas.run_config import (
    RunConfig,
    canonicalize_run_config_paths,
    validate_assembly_review_execution_plan,
    validate_genome_size_execution_plan,
)
from app.storage.paths import SessionPaths, ensure_run_layout

FROZEN_INPUTS_SCHEMA_VERSION = 1
INPUT_MANIFEST_SCHEMA_VERSION = 1
RUN_CONFIG_SCHEMA_VERSION = 2

_FILE_KEYS = ("input_manifest", "run_config", "dependency_manifest")
_FILE_NAMES = {
    "input_manifest": "input_manifest.yaml",
    "run_config": "run_config.json",
    "dependency_manifest": "dependency_manifest.json",
}
_FILE_SCHEMA_VERSIONS = {
    "input_manifest": INPUT_MANIFEST_SCHEMA_VERSION,
    "run_config": RUN_CONFIG_SCHEMA_VERSION,
    "dependency_manifest": 2,
}


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
    declarations: CanonicalInputDeclarations

    def file_sha256(self, key: str) -> str:
        try:
            return self.marker.files[key].sha256
        except KeyError as exc:
            raise FrozenInputIntegrityError(
                f"frozen input marker is missing {key!r}"
            ) from exc


@dataclass(frozen=True)
class ReadDeclarationSource:
    """One control-file field declaring a registered long-read path."""

    declaration_source: Literal["manifest", "run_config"]
    declaration_field: str
    declared_path: str


@dataclass(frozen=True)
class RegisteredReadDeclaration:
    """Canonical per-file facts shared by freeze validation and Inventory."""

    path: str
    asset_type: Literal["raw_long_reads", "clean_long_reads"]
    platform: Platform
    format: Format
    compression: Compression
    layout: Layout
    size_bytes: int | None
    sample_id: str | None
    metadata: dict[str, Any]
    manifest_entry_ordinal: int | None
    path_ordinal: int | None
    platform_resolution_source: str
    sources: tuple[ReadDeclarationSource, ...]


@dataclass(frozen=True)
class CanonicalInputDeclarations:
    """One canonical manifest plus its merged run-config read declarations."""

    manifest: InputManifest
    manifest_report: dict[str, Any]
    read_declarations: tuple[RegisteredReadDeclaration, ...]


@dataclass(frozen=True)
class PreparedSessionInputs:
    """Validated canonical inputs ready for a later create-only freeze.

    Preparation deliberately has no ``SessionPaths`` argument and never
    creates directories or control files.
    """

    manifest: InputManifest
    run_config: RunConfig
    declarations: CanonicalInputDeclarations


def canonicalize_input_declarations(
    manifest: InputManifest,
    run_config: RunConfig,
) -> CanonicalInputDeclarations:
    """Merge manifest and supported run-config reads without inventing identities.

    The manifest remains the schema authority for manifest-owned assets.  The
    run config may add an Assembly HiFi selection and explicit T2T read inputs.
    Compatible aliases reuse the manifest path; conflicting platform claims and
    duplicate canonical paths within one run-config field fail the returned
    validation report.
    """

    canonical_manifest = manifest.model_copy(deep=True)
    shared_platform = (
        run_config.shared_parameters.platform
        if run_config.shared_parameters is not None
        else None
    )
    shared_platform_entry = _apply_shared_long_read_platform(
        canonical_manifest,
        shared_platform,
    )
    report = validate_manifest(canonical_manifest)
    errors = [
        str(error)
        for error in report["errors"]
        if not _is_manifest_platform_relation_error(str(error))
    ]
    warnings = [
        str(warning)
        for warning in report["warnings"]
        if not _is_manifest_unknown_platform_warning(str(warning))
    ]
    inferred = [AssetEntryInference.model_validate(item) for item in report["inferred"]]

    manifest_owners: dict[str, tuple[int, int, str]] = {}
    declarations: dict[str, dict[str, Any]] = {}
    for entry_ordinal, (asset, entry) in enumerate(
        zip(canonical_manifest.assets, inferred, strict=True)
    ):
        for path_ordinal, fact in enumerate(entry.path_facts):
            canonical_path = str(
                Path(fact.canonical_path).expanduser().resolve(strict=False)
            )
            manifest_owners[canonical_path] = (
                entry_ordinal,
                path_ordinal,
                str(asset.asset_type),
            )
            if asset.asset_type not in {"raw_long_reads", "clean_long_reads"}:
                continue
            platform_source = (
                "shared_parameters.platform"
                if shared_platform_entry == entry_ordinal
                else fact.platform_resolution_source
            )
            declarations[canonical_path] = {
                "path": canonical_path,
                "asset_type": asset.asset_type,
                "platform": fact.platform,
                "format": fact.format,
                "compression": fact.compression,
                "layout": entry.inferred_layout,
                "size_bytes": fact.size_bytes,
                "sample_id": asset.sample_id or canonical_manifest.sample_id,
                "metadata": dict(asset.metadata),
                "manifest_entry_ordinal": entry_ordinal,
                "path_ordinal": path_ordinal,
                "platform_resolution_source": platform_source,
                "sources": [
                    ReadDeclarationSource(
                        declaration_source="manifest",
                        declaration_field=(
                            f"assets[{entry_ordinal}].paths[{path_ordinal}]"
                        ),
                        declared_path=fact.declared_path,
                    )
                ],
            }

    run_declarations: list[tuple[str, Platform, str, str]] = []
    if run_config.assembly and run_config.assembly.hifi_reads_ref:
        run_declarations.append(
            (
                run_config.assembly.hifi_reads_ref,
                "pacbio_hifi",
                "assembly.hifi_reads_ref",
                "assembly.hifi_reads_ref",
            )
        )
    if run_config.t2t:
        run_declarations.extend(
            (
                item.path,
                item.platform,
                f"t2t.read_inputs[{index}].path",
                "t2t.read_inputs",
            )
            for index, item in enumerate(run_config.t2t.read_inputs)
        )

    seen_by_field: set[tuple[str, str]] = set()
    for (
        declared_path,
        declared_platform,
        declaration_field,
        source_group,
    ) in run_declarations:
        path = Path(declared_path).expanduser()
        if not path.is_absolute():
            errors.append(
                f"run-config {declaration_field} is not canonical absolute: "
                f"{declared_path}"
            )
            continue
        canonical_path = str(path.resolve(strict=False))
        source_key = (source_group, canonical_path)
        if source_key in seen_by_field:
            errors.append(
                f"run-config {source_group} repeats canonical path {canonical_path}"
            )
            continue
        seen_by_field.add(source_key)

        owner = manifest_owners.get(canonical_path)
        if owner is not None and owner[2] not in {
            "raw_long_reads",
            "clean_long_reads",
        }:
            errors.append(
                f"run-config {declaration_field} collides with manifest "
                f"{owner[2]} path {canonical_path}"
            )
            continue

        source = ReadDeclarationSource(
            declaration_source="run_config",
            declaration_field=declaration_field,
            declared_path=declared_path,
        )
        existing = declarations.get(canonical_path)
        if existing is not None:
            existing_platform = str(existing["platform"])
            if existing_platform not in {"unknown", declared_platform}:
                errors.append(
                    f"conflicting read platforms for {canonical_path}: "
                    f"{existing_platform} vs {declared_platform} from "
                    f"{declaration_field}"
                )
                continue
            if existing_platform == "unknown":
                existing["platform"] = declared_platform
                existing["platform_resolution_source"] = declaration_field
            existing["sources"].append(source)
            continue

        synthetic_manifest = InputManifest(
            session_id=canonical_manifest.session_id,
            sample_id=canonical_manifest.sample_id,
            assets=[
                InputAssetSpec(
                    asset_type="raw_long_reads",
                    paths=[canonical_path],
                    platform=declared_platform,
                )
            ],
        )
        synthetic_report = validate_manifest(synthetic_manifest)
        errors.extend(
            f"{declaration_field}: {error}" for error in synthetic_report["errors"]
        )
        warnings.extend(
            f"{declaration_field}: {warning}"
            for warning in synthetic_report["warnings"]
        )
        synthetic_entry = AssetEntryInference.model_validate(
            synthetic_report["inferred"][0]
        )
        fact = synthetic_entry.path_facts[0]
        declarations[canonical_path] = {
            "path": canonical_path,
            "asset_type": "raw_long_reads",
            "platform": declared_platform,
            "format": fact.format,
            "compression": fact.compression,
            "layout": synthetic_entry.inferred_layout,
            "size_bytes": fact.size_bytes,
            "sample_id": canonical_manifest.sample_id,
            "metadata": {},
            "manifest_entry_ordinal": None,
            "path_ordinal": None,
            "platform_resolution_source": declaration_field,
            "sources": [source],
        }

    for entry_ordinal, asset in enumerate(canonical_manifest.assets):
        if asset.asset_type not in {"raw_long_reads", "clean_long_reads"}:
            continue
        entry_declarations = [
            declaration
            for declaration in declarations.values()
            if declaration["manifest_entry_ordinal"] == entry_ordinal
        ]
        platforms = {str(declaration["platform"]) for declaration in entry_declarations}
        if len(platforms) > 1:
            errors.append(
                f"{asset.asset_type}: one manifest entry resolves to conflicting "
                f"platforms {sorted(platforms)}; split platforms into separate "
                "entries"
            )
        if asset.asset_type == "raw_long_reads" and "unknown" in platforms:
            warnings.append(f"{asset.asset_type}: platform unknown for {asset.paths}")

    declaration_groups = {
        (
            "manifest",
            declaration["manifest_entry_ordinal"],
        )
        if declaration["manifest_entry_ordinal"] is not None
        else ("run_config", declaration["path"])
        for declaration in declarations.values()
    }
    if len(declaration_groups) > 1:
        unresolved_groups = {
            (
                "manifest",
                declaration["manifest_entry_ordinal"],
            )
            if declaration["manifest_entry_ordinal"] is not None
            else ("run_config", declaration["path"])
            for declaration in declarations.values()
            if declaration["platform"] == "unknown"
        }
        if unresolved_groups:
            unresolved = [
                str(declaration["path"])
                for declaration in declarations.values()
                if declaration["platform"] == "unknown"
            ]
            errors.append(
                "mixed_long_read_platform_unresolved: explicit platform required "
                f"for {unresolved}"
            )

    merged_report = {
        **report,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }
    return CanonicalInputDeclarations(
        manifest=canonical_manifest,
        manifest_report=merged_report,
        read_declarations=tuple(
            RegisteredReadDeclaration(
                path=str(item["path"]),
                asset_type=item["asset_type"],
                platform=item["platform"],
                format=item["format"],
                compression=item["compression"],
                layout=item["layout"],
                size_bytes=item["size_bytes"],
                sample_id=item["sample_id"],
                metadata=dict(item["metadata"]),
                manifest_entry_ordinal=item["manifest_entry_ordinal"],
                path_ordinal=item["path_ordinal"],
                platform_resolution_source=str(item["platform_resolution_source"]),
                sources=tuple(item["sources"]),
            )
            for item in declarations.values()
        ),
    )


def _is_manifest_platform_relation_error(message: str) -> bool:
    return message.startswith("mixed_long_read_platform_unresolved:") or (
        message.startswith(("raw_long_reads:", "clean_long_reads:"))
        and "one manifest entry resolves to conflicting platforms" in message
    )


def _is_manifest_unknown_platform_warning(message: str) -> bool:
    return message.startswith("raw_long_reads: platform unknown for ")


def _apply_shared_long_read_platform(
    manifest: InputManifest,
    platform: Platform | None,
) -> int | None:
    """Apply one shared hint only to one unresolved manifest read group."""

    if platform in {None, "unknown"}:
        return None
    long_read_entries = [
        (index, asset)
        for index, asset in enumerate(manifest.assets)
        if asset.asset_type in {"raw_long_reads", "clean_long_reads"}
    ]
    if len(long_read_entries) != 1:
        return None
    entry_ordinal, asset = long_read_entries[0]
    if asset.platform != "unknown":
        return None
    asset.platform = platform
    return entry_ordinal


def prepare_session_inputs(
    *,
    manifest_draft: str | Path | InputManifest | Mapping[str, Any],
    run_config_draft: str | Path | RunConfig | Mapping[str, Any],
) -> PreparedSessionInputs:
    """Validate and canonicalize drafts without creating any run-owned path."""

    try:
        manifest = _load_manifest_draft(manifest_draft)
        run_config = _load_run_config_draft(run_config_draft)
        declarations = canonicalize_input_declarations(manifest, run_config)
        if not declarations.manifest_report["valid"]:
            raise FrozenInputValidationError(
                "input declarations cannot be frozen: "
                + "; ".join(declarations.manifest_report["errors"])
            )
        manifest = declarations.manifest
        _validate_assembly_input_contract(
            manifest,
            run_config,
            declarations.read_declarations,
        )
        validate_assembly_review_execution_plan(run_config)
        validate_genome_size_execution_plan(run_config, manifest)
    except FrozenInputValidationError:
        raise
    except (ValidationError, ValueError) as exc:
        raise FrozenInputValidationError(
            f"input preparation failed validation: {exc}"
        ) from exc

    return PreparedSessionInputs(
        manifest=manifest,
        run_config=run_config,
        declarations=declarations,
    )


def freeze_session_inputs(
    paths: SessionPaths,
    *,
    manifest_draft: str | Path | InputManifest | Mapping[str, Any] | None = None,
    run_config_draft: str | Path | RunConfig | Mapping[str, Any] | None = None,
    prepared_inputs: PreparedSessionInputs | None = None,
    config_path: str | Path | None = None,
    parent_session_id: str | None = None,
    selected_methods: Mapping[str, str | Sequence[str]] | None = None,
    preflight_runner: Any | None = None,
    probe_environments: bool | None = None,
) -> FrozenInputBundle:
    """Validate and commit one run's execution inputs exactly once.

    Supply either raw manifest/run-config drafts or a prepared input bundle,
    but never both.  Raw drafts are prepared before layout creation; file-based
    drafts have their relative paths resolved once against their own directory.
    In-memory drafts must already use absolute paths, preventing accidental
    dependence on the process working directory.

    Repeating the same freeze returns the already verified bundle.  Any partial
    publication, a changed draft, a changed parent identity, or a tampered
    frozen file fails closed and never overwrites historical control files.
    """

    raw_mode = manifest_draft is not None or run_config_draft is not None
    if prepared_inputs is not None:
        if raw_mode:
            raise FrozenInputValidationError(
                "freeze_session_inputs accepts either prepared_inputs or raw drafts"
            )
        prepared = prepare_session_inputs(
            manifest_draft=prepared_inputs.manifest,
            run_config_draft=prepared_inputs.run_config,
        )
    elif manifest_draft is not None and run_config_draft is not None:
        prepared = prepare_session_inputs(
            manifest_draft=manifest_draft,
            run_config_draft=run_config_draft,
        )
    else:
        raise FrozenInputValidationError(
            "freeze_session_inputs requires prepared_inputs or both raw drafts"
        )

    if prepared.manifest.session_id != paths.session_id:
        raise FrozenInputValidationError(
            "prepared manifest session_id does not match its run directory"
        )
    _validate_parent_session_id(paths, parent_session_id)
    manifest = prepared.manifest
    run_config = prepared.run_config
    dependency_stages, dependency_methods = _dependency_scope(
        run_config,
        selected_methods=selected_methods,
    )
    ensure_run_layout(paths)
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
    declarations = canonicalize_input_declarations(manifest, run_config)
    if not declarations.manifest_report["valid"]:
        raise FrozenInputIntegrityError(
            "frozen manifest/run-config declarations are invalid: "
            + "; ".join(declarations.manifest_report["errors"])
        )
    if declarations.manifest != manifest:
        raise FrozenInputIntegrityError(
            "frozen manifest does not contain the canonical shared platform"
        )
    try:
        _validate_assembly_input_contract(
            manifest,
            run_config,
            declarations.read_declarations,
        )
        validate_genome_size_execution_plan(run_config, manifest)
    except (FrozenInputValidationError, ValueError) as exc:
        raise FrozenInputIntegrityError(
            f"frozen manifest/run-config biological validation is invalid: {exc}"
        ) from exc

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
        declarations=declarations,
    )


def has_frozen_input_contract(paths: SessionPaths) -> bool:
    """Whether any transaction-owned frozen-control file is present.

    ``run_config.json`` alone is not a commit signal. If any unique transaction
    file exists, the executor validates the complete marker and fails closed on
    an interrupted or tampered freeze.
    """

    return any(
        path.exists()
        for path in (
            paths.input_manifest_path,
            paths.dependency_manifest_path,
            paths.frozen_inputs_path,
        )
    )


@dataclass
class FrozenExecutionLease:
    """In-memory proof that one session's execution lock is currently held."""

    session_id: str
    run_dir: Path
    active: bool = False

    def __post_init__(self) -> None:
        self.run_dir = self.run_dir.expanduser().resolve(strict=False)

    def require_active(self, paths: SessionPaths) -> None:
        """Fail unless this live lease belongs to ``paths``."""

        if not self.active:
            raise ValueError("frozen execution lease is inactive")
        if (
            self.session_id != paths.session_id
            or self.run_dir != paths.run_dir.expanduser().resolve(strict=False)
        ):
            raise ValueError(
                "frozen execution lease does not match the requested "
                "session/run directory"
            )


@contextmanager
def frozen_execution_lock(paths: SessionPaths) -> Iterator[FrozenExecutionLease]:
    """Own one complete frozen execution attempt or fail immediately."""

    paths.control_dir.mkdir(parents=True, exist_ok=True)
    with paths.execution_lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FrozenExecutionActiveError(
                f"session {paths.session_id} is already executing"
            ) from exc
        lease = FrozenExecutionLease(
            session_id=paths.session_id,
            run_dir=paths.run_dir,
            active=True,
        )
        try:
            yield lease
        finally:
            lease.active = False
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def frozen_execution_lock_held(paths: SessionPaths) -> bool:
    """Return whether another open file description owns execution."""

    lock_path = paths.execution_lock_path
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
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
    selected_methods: Mapping[str, str | Sequence[str]] | None,
) -> tuple[list[str], list[str]]:
    # The frozen RunConfig is the sole workflow-plan authority.  Environment
    # resolution needs only the distinct stage types represented by that plan,
    # so repeated occurrences are intentionally deduplicated here and nowhere
    # in runtime routing.
    stages = list(
        dict.fromkeys(str(stage) for stage in run_config.requested_stage_names())
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
    return manifest


def _load_run_config_draft(
    source: str | Path | RunConfig | Mapping[str, Any],
) -> RunConfig:
    """Load and canonicalize the strict frozen RunConfig draft."""

    payload, source_dir = _load_json_draft(source, label="run-config draft")
    try:
        RunConfig.model_validate(payload)
        normalized = canonicalize_run_config_paths(
            payload,
            source_dir=source_dir,
        )
        return RunConfig.model_validate(normalized)
    except (ValidationError, ValueError) as exc:
        raise FrozenInputValidationError(
            f"run-config draft failed validation: {exc}"
        ) from exc


def _validate_assembly_input_contract(
    manifest: InputManifest,
    run_config: RunConfig,
    read_declarations: Sequence[RegisteredReadDeclaration],
) -> None:
    """Cross-validate method-owned read roles before freezing control files."""

    assembly = run_config.assembly
    if assembly is None:
        return
    from app.domains.assembly.selection import (
        is_method_read_type_compatible,
        resolve_read_type,
    )
    from app.schemas.run_config import infer_assembly_method

    method = infer_assembly_method(assembly)
    local_assembly = run_config.any_occurrence_allows_local_execution("assembly")
    shared_parameters = run_config.shared_parameters
    read_type = resolve_read_type(
        ("assembly.assembly_route", assembly.assembly_route),
        (
            "shared_parameters.read_type",
            shared_parameters.read_type if shared_parameters else None,
        ),
        (
            "shared_parameters.platform",
            None,
            shared_parameters.platform if shared_parameters else None,
        ),
        context="storage.frozen_inputs.assembly_input_contract",
    )
    method_requires_read_type = method is not None and (
        assembly.assembly_method is not None or local_assembly
    )
    if method_requires_read_type:
        assert method is not None
        if read_type is None:
            if assembly.assembly_method is not None:
                message = (
                    "run_config: assembly.assembly_method is set but no "
                    "read_type could be inferred"
                )
            else:
                message = (
                    f"run_config: local assembly method {method!r} was inferred "
                    "but no read_type could be inferred"
                )
            raise FrozenInputValidationError(
                message + "; set assembly.assembly_route, shared_parameters.read_type, "
                "or shared_parameters.platform"
            )
        if not is_method_read_type_compatible(method, read_type):
            raise FrozenInputValidationError(
                f"run_config: assembly method {method!r} is incompatible with "
                f"inferred read_type {read_type!r}"
            )
    if local_assembly and method in {"flye", "nextdenovo"}:
        registered_read_types = {
            declaration_read_type
            for declaration in read_declarations
            if (
                declaration_read_type := resolve_read_type(
                    (
                        "registered long-read declaration",
                        None,
                        declaration.platform,
                    ),
                    context="storage.frozen_inputs.registered_read_type",
                )
            )
            is not None
        }
        compatible_declaration = read_type in registered_read_types or (
            read_type in {"ont_raw", "ont_hq"}
            and bool(registered_read_types.intersection({"ont_raw", "ont_hq"}))
        )
        if not compatible_declaration:
            raise FrozenInputValidationError(
                f"{method} execution requires registered long-read declarations "
                f"compatible with read_type {read_type!r}"
            )
    hifi_paths = {
        declaration.path
        for declaration in read_declarations
        if declaration.platform == "pacbio_hifi"
    }
    ont_paths = {
        declaration.path
        for declaration in read_declarations
        if declaration.platform == "ont"
    }
    paired_entries = [asset for asset in manifest.assets if asset.paired_reads]
    if method == "hifiasm" and local_assembly:
        if not hifi_paths:
            raise FrozenInputValidationError(
                "HiFi Hifiasm execution requires registered pacbio_hifi reads"
            )
        params = assembly.hifiasm_parameters
        if params and params.use_ultralong_ont and not ont_paths:
            raise FrozenInputValidationError(
                "hifiasm use_ultralong_ont requires registered ONT reads"
            )
        if params and params.use_hic_phasing and not paired_entries:
            raise FrozenInputValidationError(
                "hifiasm use_hic_phasing requires registered paired Hi-C reads"
            )
    if method == "hifiasm_ont" and local_assembly:
        if not ont_paths:
            raise FrozenInputValidationError(
                "Hifiasm ONT execution requires registered ONT reads"
            )
    if assembly.hifi_reads_ref:
        selected = str(Path(assembly.hifi_reads_ref).resolve(strict=False))
        if selected not in hifi_paths:
            raise FrozenInputValidationError(
                "assembly.hifi_reads_ref must select a registered HiFi member"
            )
    if assembly.hic_reads_ref:
        selected = str(Path(assembly.hic_reads_ref).resolve(strict=False))
        matching_entries = [
            asset
            for asset in paired_entries
            if selected
            in {str(Path(path).resolve(strict=False)) for path in asset.paths}
        ]
        if len(matching_entries) != 1:
            raise FrozenInputValidationError(
                "assembly.hic_reads_ref must select exactly one registered "
                "paired-read group"
            )


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
