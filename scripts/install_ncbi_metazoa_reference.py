#!/usr/bin/env python3
"""Download, install, or verify the pinned NCBI Metazoa snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.execution.env_resolver import (  # noqa: E402
    EnvResolver,
    active_config_path,
)

MANIFEST_FILENAME = "reference_manifest.json"
PINNED_REFERENCE_DIR = PROJECT_ROOT / "data" / "reference" / "ncbi_metazoa"
REFERENCE_FILES = (
    "Metazoa.ncbi.tsv",
    "Metazoa.ncbi.lineage",
    "species_alias.tsv",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--source-dir", type=Path, help="Directory holding the pinned NCBI export files"
    )
    source.add_argument(
        "--source-url",
        help="URL of a ZIP containing the pinned manifest and all reference files",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Active envs.yaml; defaults to GENE_AGENT_ENVS_CONFIG, then "
            "config/envs.yaml"
        ),
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only validate files already in destination",
    )
    args = parser.parse_args(argv)
    if args.verify_only and (args.source_dir or args.source_url):
        parser.error("--verify-only cannot be combined with a source")
    if not args.verify_only and args.source_dir is None and args.source_url is None:
        parser.error("provide --source-dir, --source-url, or use --verify-only")

    config_path = args.config or active_config_path()
    try:
        resolver = EnvResolver(config_path)
        if resolver.databases is None:
            raise ValueError("databases registry is missing")
        destination = (
            Path(resolver.databases.ncbi_metazoa.reference_dir)
            .expanduser()
            .resolve(strict=False)
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"cannot resolve databases.ncbi_metazoa.reference_dir from "
            f"{config_path}: {exc}"
        ) from exc
    pinned_manifest = _load_manifest(PINNED_REFERENCE_DIR / MANIFEST_FILENAME)
    if args.verify_only:
        errors = _verify_pinned_destination(destination, pinned_manifest)
    elif args.source_dir:
        errors = _install_from_source(
            args.source_dir.resolve(), destination, pinned_manifest
        )
    else:
        assert args.source_url is not None
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "snapshot"
            _download_snapshot(args.source_url, source_dir)
            errors = _install_from_source(source_dir, destination, pinned_manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Verified NCBI Metazoa reference snapshot: {destination}")
    return 0


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"reference manifest is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"reference manifest is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        raise SystemExit(f"reference manifest is invalid: {path}")
    if set(payload["files"]) != set(REFERENCE_FILES):
        raise SystemExit(
            f"reference manifest must declare exactly {', '.join(REFERENCE_FILES)}: "
            f"{path}"
        )
    return payload


def _install_from_source(
    source: Path,
    destination: Path,
    pinned_manifest: dict[str, Any],
) -> list[str]:
    source_manifest = _load_manifest(source / MANIFEST_FILENAME)
    if source_manifest != pinned_manifest:
        return ["source manifest does not match the repository-pinned snapshot"]
    errors = _verify(source, pinned_manifest)
    if errors:
        return [f"source {error}" for error in errors]
    if source != destination:
        destination.mkdir(parents=True, exist_ok=True)
        for filename in REFERENCE_FILES:
            shutil.copy2(source / filename, destination / filename)
        shutil.copy2(source / MANIFEST_FILENAME, destination / MANIFEST_FILENAME)
    return _verify_pinned_destination(destination, pinned_manifest)


def _verify_pinned_destination(
    destination: Path,
    pinned_manifest: dict[str, Any],
) -> list[str]:
    installed_manifest = _load_manifest(destination / MANIFEST_FILENAME)
    if installed_manifest != pinned_manifest:
        return ["installed manifest does not match the repository-pinned snapshot"]
    return _verify(destination, pinned_manifest)


def _download_snapshot(url: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / "snapshot.zip"
    try:
        with (
            urllib.request.urlopen(url, timeout=300) as response,
            archive.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle)
        with zipfile.ZipFile(archive) as bundle:
            for filename in (*REFERENCE_FILES, MANIFEST_FILENAME):
                try:
                    source = bundle.open(filename)
                except KeyError as exc:
                    raise SystemExit(
                        f"snapshot ZIP is missing root file: {filename}"
                    ) from exc
                with source, (destination / filename).open("wb") as target:
                    shutil.copyfileobj(source, target)
    except (OSError, urllib.error.URLError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"cannot download NCBI snapshot from {url}: {exc}") from exc


def _verify(destination: Path, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for filename, expected in manifest["files"].items():
        if not isinstance(expected, dict):
            errors.append(f"invalid manifest entry for {filename}")
            continue
        path = destination / filename
        if not path.is_file():
            errors.append(f"missing {filename}: {path}")
            continue
        if path.stat().st_size != expected.get("size_bytes"):
            errors.append(f"size mismatch for {filename}")
            continue
        if _sha256(path) != expected.get("sha256"):
            errors.append(f"SHA-256 mismatch for {filename}")
    return errors


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
