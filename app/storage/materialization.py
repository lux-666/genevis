"""Materialize canonical control inputs for one workflow session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from app.storage.paths import SessionPaths, ensure_run_layout


class RunMaterializationError(ValueError):
    """Raised when an entry file cannot become a canonical run input."""


def materialize_run_layout(
    paths: SessionPaths,
    *,
    manifest_path: Path,
    run_config_path: Path | None,
) -> None:
    """Create a run layout and materialize its canonical control inputs.

    Runtime manifests contain absolute asset paths. Relative paths are resolved
    exactly once against the source manifest directory before graph execution.
    Inventory therefore consumes one unambiguous contract and never falls back
    to the process working directory.
    """
    ensure_run_layout(paths)
    _materialize_manifest(manifest_path, paths.manifest_path)
    if run_config_path is not None:
        _materialize_run_config(run_config_path, paths.run_config_path)


def _materialize_manifest(source: Path, destination: Path) -> None:
    source = Path(source).expanduser().resolve(strict=False)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RunMaterializationError(f"manifest could not be read: {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunMaterializationError("manifest root must be a mapping")

    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        raise RunMaterializationError("manifest assets must be a list")
    normalized_assets: list[dict[str, Any]] = []
    for index, raw_asset in enumerate(assets):
        if not isinstance(raw_asset, dict):
            raise RunMaterializationError(f"manifest assets[{index}] must be a mapping")
        asset = dict(raw_asset)
        raw_paths = asset.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RunMaterializationError(
                f"manifest assets[{index}].paths must be a list"
            )
        asset["paths"] = [
            str(_resolve_asset_path(raw_path, source.parent, index=index))
            for raw_path in raw_paths
        ]
        normalized_assets.append(asset)
    payload["assets"] = normalized_assets
    destination.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _resolve_asset_path(raw_path: Any, source_dir: Path, *, index: int) -> Path:
    if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
        raise RunMaterializationError(
            f"manifest assets[{index}].paths contains an empty or invalid path"
        )
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (source_dir / path).resolve(strict=False)


_RUN_CONFIG_PATH_FIELDS = (
    ("assembly", "hifi_reads_ref"),
    ("assembly", "hic_reads_ref"),
    ("assembly_review", "busco", "lineage_path"),
    ("assembly_review", "related_species_reference_dir"),
    ("t2t", "chromosome_list_ref"),
    ("t2t", "assembly_fasta_ref"),
    ("t2t", "ont_reads_ref"),
)


def _materialize_run_config(source: Path, destination: Path) -> None:
    source = Path(source).expanduser().resolve(strict=False)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunMaterializationError(
            f"run config could not be read: {source}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise RunMaterializationError("run config root must be an object")

    for field_path in _RUN_CONFIG_PATH_FIELDS:
        _normalize_run_config_path(payload, field_path, source.parent)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _normalize_run_config_path(
    payload: dict[str, Any],
    field_path: tuple[str, ...],
    source_dir: Path,
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
        raise RunMaterializationError(
            f"run config {'.'.join(field_path)} must be a non-empty path"
        )
    path = Path(value).expanduser()
    parent[key] = str(path if path.is_absolute() else (source_dir / path).resolve(strict=False))
