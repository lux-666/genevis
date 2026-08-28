#!/usr/bin/env python3
"""Download, install, or verify one configured NCBI reference snapshot."""

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
from app.reference.ncbi import (  # noqa: E402
    declared_reference_files,
    validate_reference_snapshot,
)

MANIFEST_FILENAME = "reference_manifest.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference",
        required=True,
        help="Snapshot child directory name under databases.ncbi.reference_dir",
    )
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
            Path(resolver.databases.ncbi.reference_dir)
            .expanduser()
            .resolve(strict=False)
            / args.reference
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise SystemExit(
            f"cannot resolve databases.ncbi.reference_dir from {config_path}: {exc}"
        ) from exc
    if args.verify_only:
        errors = _verify_destination(destination)
    elif args.source_dir:
        errors = _install_from_source(args.source_dir.resolve(), destination)
    else:
        assert args.source_url is not None
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "snapshot"
            _download_snapshot(args.source_url, source_dir)
            errors = _install_from_source(source_dir, destination)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Verified NCBI {args.reference} reference snapshot: {destination}")
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
    try:
        declared_reference_files(payload)
    except ValueError as exc:
        raise SystemExit(f"reference manifest is invalid: {path}: {exc}") from exc
    return payload


def _install_from_source(
    source: Path,
    destination: Path,
) -> list[str]:
    source_manifest = _load_manifest(source / MANIFEST_FILENAME)
    errors = _verify(source, source_manifest)
    if errors:
        return [f"source {error}" for error in errors]
    if source != destination:
        destination.mkdir(parents=True, exist_ok=True)
        for filename in declared_reference_files(source_manifest):
            shutil.copy2(source / filename, destination / filename)
        shutil.copy2(source / MANIFEST_FILENAME, destination / MANIFEST_FILENAME)
    return _verify_destination(destination)


def _verify_destination(destination: Path) -> list[str]:
    try:
        validate_reference_snapshot(destination)
    except (OSError, TypeError, ValueError) as exc:
        return [str(exc)]
    return []


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
            try:
                manifest_bytes = bundle.read(MANIFEST_FILENAME)
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                filenames = declared_reference_files(manifest)
            except (
                KeyError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                raise SystemExit(
                    f"snapshot ZIP has an invalid manifest: {exc}"
                ) from exc
            for filename in (*filenames, MANIFEST_FILENAME):
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
