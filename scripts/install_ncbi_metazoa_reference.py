#!/usr/bin/env python3
"""Install or verify the pinned local NCBI Metazoa reference snapshot.

The workflow deliberately does not download biological reference data. This
utility copies an operator-provided snapshot into the project location and
checks it against the tracked manifest used by assembly review at runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE_DIR = PROJECT_ROOT / "data" / "reference" / "ncbi_metazoa"
MANIFEST_FILENAME = "reference_manifest.json"
COPYABLE_FILES = ("Metazoa.ncbi.tsv", "Metazoa.ncbi.lineage")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir", type=Path, help="Directory holding the pinned NCBI export files"
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_REFERENCE_DIR,
        help="Project reference directory",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only validate files already in destination",
    )
    args = parser.parse_args(argv)
    if args.verify_only and args.source_dir:
        parser.error("--verify-only cannot be combined with --source-dir")
    if not args.verify_only and args.source_dir is None:
        parser.error("provide --source-dir or use --verify-only")

    destination = args.destination.resolve()
    manifest = _load_manifest(destination / MANIFEST_FILENAME)
    if args.source_dir:
        source = args.source_dir.resolve()
        for filename in COPYABLE_FILES:
            source_path = source / filename
            if not source_path.is_file():
                raise SystemExit(f"source file is missing: {source_path}")
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination / filename)

    errors = _verify(destination, manifest)
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
    return payload


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
