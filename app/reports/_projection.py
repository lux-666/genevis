"""Small shared primitives for regenerable report projections."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from app.control.ledger import Ledger
from app.storage.paths import SessionPaths


def source_watermark(paths: SessionPaths, ledger: Ledger) -> dict[str, object]:
    """Identify the exact durable inputs used by one report generation."""
    index_bytes = (
        paths.artifact_index_path.read_bytes()
        if paths.artifact_index_path.exists()
        else b""
    )
    return {
        "last_ledger_sequence": ledger.last_sequence(paths.session_id),
        "artifact_index_sha256": hashlib.sha256(index_bytes).hexdigest(),
    }


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace one derived report inside its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
