"""Artifact metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def file_sha256(path: Path) -> str:
    """Return a file SHA256 digest."""
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactMeta(BaseModel):
    """Metadata stored in state/ledger instead of large file contents."""

    artifact_id: str
    file_path: Path
    file_hash: str
    file_size_bytes: int
    artifact_type: str
    stage: str
    method_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        stage: str,
        method_name: str,
        artifact_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> "ArtifactMeta":
        """Build artifact metadata for an existing file."""
        return cls(
            artifact_id=f"{stage}:{method_name}:{path.name}",
            file_path=path,
            file_hash=file_sha256(path),
            file_size_bytes=path.stat().st_size,
            artifact_type=artifact_type,
            stage=stage,
            method_name=method_name,
            metadata=metadata or {},
        )
