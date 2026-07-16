"""Test helpers for constructing clean Data Architecture v2 run roots."""

from __future__ import annotations

from pathlib import Path

from app.storage.paths import SessionPaths, ensure_data_layout, ensure_run_layout


def v2_run_dir(data_root: str | Path, session_id: str) -> Path:
    """Create and return ``<data-root>/runs/<session-id>`` for a test."""

    ensure_data_layout(data_root)
    paths = SessionPaths(data_root, session_id)
    ensure_run_layout(paths)
    if not paths.manifest_path.exists():
        paths.manifest_path.write_text(
            f"session_id: {session_id}\nassets: []\n",
            encoding="utf-8",
        )
    return paths.run_dir
