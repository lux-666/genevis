"""Frozen input-dependency construction and pre-execution verification."""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from app.integrity import sha256_file
from app.schemas.execution import (
    ArtifactInputDependency,
    AssetInputDependency,
    DirectoryInputDependency,
    FileInputDependency,
    InputDependency,
)
from app.schemas.state import TopLevelState
from app.storage.artifacts import resolve_artifact_ref
from app.storage.asset_registry import load_asset_registry


def asset_input_dependencies(
    state: TopLevelState,
    asset_ids: Sequence[str],
    paths: Sequence[str | Path],
) -> list[AssetInputDependency]:
    """Freeze ordered registry identities and their current byte evidence."""

    if len(asset_ids) != len(paths):
        raise ValueError(
            "asset input dependency cardinality mismatch: "
            f"{len(asset_ids)} IDs vs {len(paths)} paths"
        )
    records = {record.asset_id: record for record in load_asset_registry(state).assets}
    dependencies: list[AssetInputDependency] = []
    for asset_id, raw_path in zip(asset_ids, paths, strict=True):
        record = records.get(str(asset_id))
        if record is None:
            raise ValueError(f"input dependency asset is not registered: {asset_id!r}")
        if (
            record.checksum_status != "ready"
            or record.size_bytes is None
            or record.checksum is None
        ):
            raise ValueError(
                f"input dependency asset lacks ready byte evidence: {asset_id!r}"
            )
        canonical = _canonical(raw_path)
        registered = _canonical(record.path)
        if canonical != registered:
            raise ValueError(
                "input dependency path does not match its registered asset: "
                f"{asset_id!r}"
            )
        dependencies.append(
            AssetInputDependency(
                asset_id=str(asset_id),
                path=canonical,
                size_bytes=record.size_bytes,
                sha256=record.checksum,
            )
        )
    return dependencies


def artifact_input_dependency(
    state: TopLevelState,
    artifact_ref: object,
    path: str | Path,
) -> ArtifactInputDependency:
    """Freeze one run-owned artifact and require its command path to match."""

    dependency = ArtifactInputDependency.model_validate(
        {"artifact_ref": artifact_ref, "path": _canonical(path)}
    )
    resolved = resolve_artifact_ref(
        state.get("data_dir", "data"),
        state["session_id"],
        dependency.artifact_ref,
    )
    if _canonical(resolved) != dependency.path:
        raise ValueError("artifact dependency path does not match its ArtifactRef")
    return dependency


def directory_input_dependency(path: str | Path) -> DirectoryInputDependency:
    """Freeze deterministic byte evidence for one external directory tree."""

    canonical = _canonical(path)
    file_count, total_size_bytes, tree_sha256 = _directory_evidence(
        Path(canonical)
    )
    return DirectoryInputDependency(
        path=canonical,
        file_count=file_count,
        total_size_bytes=total_size_bytes,
        tree_sha256=tree_sha256,
    )


def file_input_dependency(path: str | Path) -> FileInputDependency:
    """Freeze byte evidence for one unregistered external file."""

    canonical = _canonical(path)
    source = Path(canonical)
    if not source.is_file():
        raise ValueError(f"file input dependency is missing: {source}")
    return FileInputDependency(
        path=canonical,
        size_bytes=source.stat().st_size,
        sha256=sha256_file(source),
    )


def verify_input_dependencies(
    state: TopLevelState,
    dependencies: Sequence[InputDependency],
) -> None:
    """Fail closed if any frozen command input drifted before Runner invocation."""

    registry_by_id = None
    digests: dict[str, str] = {}
    for dependency in dependencies:
        if isinstance(dependency, AssetInputDependency):
            if registry_by_id is None:
                registry_by_id = {
                    record.asset_id: record
                    for record in load_asset_registry(state).assets
                }
            record = registry_by_id.get(dependency.asset_id)
            if record is None:
                raise ValueError(
                    f"input dependency asset is no longer registered: "
                    f"{dependency.asset_id!r}"
                )
            if (
                record.checksum_status != "ready"
                or record.size_bytes is None
                or record.checksum is None
            ):
                raise ValueError(
                    f"input dependency asset lacks ready byte evidence: "
                    f"{dependency.asset_id!r}"
                )
            if (
                dependency.path != _canonical(record.path)
                or dependency.size_bytes != record.size_bytes
                or dependency.sha256 != record.checksum
            ):
                raise ValueError(
                    f"input dependency evidence drifted from registry: "
                    f"{dependency.asset_id!r}"
                )
            path = Path(dependency.path)
            if not path.is_file() or path.stat().st_size != dependency.size_bytes:
                raise ValueError(
                    f"input dependency bytes are missing or changed: "
                    f"{dependency.asset_id!r}"
                )
            actual = digests.get(dependency.path)
            if actual is None:
                actual = sha256_file(path)
                digests[dependency.path] = actual
            if actual != dependency.sha256:
                raise ValueError(
                    f"input dependency checksum changed: {dependency.asset_id!r}"
                )
            continue

        if isinstance(dependency, FileInputDependency):
            path = Path(dependency.path)
            if not path.is_file() or path.stat().st_size != dependency.size_bytes:
                raise ValueError(
                    f"file input dependency bytes are missing or changed: "
                    f"{dependency.path!r}"
                )
            actual = digests.get(dependency.path)
            if actual is None:
                actual = sha256_file(path)
                digests[dependency.path] = actual
            if actual != dependency.sha256:
                raise ValueError(
                    f"file input dependency checksum changed: "
                    f"{dependency.path!r}"
                )
            continue

        if isinstance(dependency, DirectoryInputDependency):
            evidence = _directory_evidence(Path(dependency.path))
            expected = (
                dependency.file_count,
                dependency.total_size_bytes,
                dependency.tree_sha256,
            )
            if evidence != expected:
                raise ValueError(
                    f"directory input dependency changed: {dependency.path!r}"
                )
            continue

        resolved = resolve_artifact_ref(
            state.get("data_dir", "data"),
            state["session_id"],
            dependency.artifact_ref,
        )
        if _canonical(resolved) != dependency.path:
            raise ValueError(
                "artifact dependency command path does not match its ArtifactRef: "
                f"{dependency.artifact_ref.artifact_id!r}"
            )


def _canonical(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _directory_evidence(path: Path) -> tuple[int, int, str]:
    if not path.is_dir():
        raise ValueError(f"directory input dependency is missing: {path}")
    digest = sha256()
    file_count = 0
    total_size_bytes = 0
    entries = sorted(
        path.rglob("*"),
        key=lambda item: item.relative_to(path).as_posix(),
    )
    for entry in entries:
        relative = entry.relative_to(path).as_posix()
        if entry.is_symlink():
            raise ValueError(
                f"directory input dependency contains a symlink: {relative}"
            )
        if entry.is_dir():
            digest.update(f"d\\0{relative}\\0".encode())
            continue
        if not entry.is_file():
            raise ValueError(
                f"directory input dependency contains a non-file entry: {relative}"
            )
        size_bytes = entry.stat().st_size
        file_sha256 = sha256_file(entry)
        digest.update(
            f"f\\0{relative}\\0{size_bytes}\\0{file_sha256}\\0".encode()
        )
        file_count += 1
        total_size_bytes += size_bytes
    return file_count, total_size_bytes, digest.hexdigest()
