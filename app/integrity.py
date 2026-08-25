"""Exact-byte integrity primitives shared by artifact producers and storage."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

SHA256_PATTERN = r"^[0-9a-f]{64}$"


def validate_sha256(value: str) -> str:
    """Require the canonical textual form of a SHA-256 digest."""

    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("sha256 must be a lowercase SHA-256 digest")
    return value


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file's exact bytes."""
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
