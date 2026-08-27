"""Exact-byte integrity primitives shared by artifact producers and storage."""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import TypeAlias

SHA256_PATTERN = r"^[0-9a-f]{64}$"

FileStatFingerprint: TypeAlias = tuple[int, int, int, int, int]


def validate_sha256(value: str) -> str:
    """Require the canonical textual form of a SHA-256 digest."""

    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("sha256 must be a lowercase SHA-256 digest")
    return value


def sha256_file(path: str | Path) -> str:
    """Return an exact digest, reusing it while the file identity is unchanged."""
    source = Path(path).expanduser().resolve(strict=False)
    fingerprint = _stat_fingerprint(source)
    digest = _sha256_file_for_fingerprint(str(source), fingerprint)
    if _stat_fingerprint(source) != fingerprint:
        raise OSError(f"file changed while hashing: {source}")
    return digest


def _stat_fingerprint(path: Path) -> FileStatFingerprint:
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@lru_cache(maxsize=1024)
def _sha256_file_for_fingerprint(
    path: str,
    fingerprint: FileStatFingerprint,
) -> str:
    # ponytail: process-local stat lease; rehash if any identity field changes.
    del fingerprint
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
