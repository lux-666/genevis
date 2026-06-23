"""Single source of truth for data-root and per-session paths.

New CLI runs live below ``<data-root>/runs/<session-id>``.  Code that still
uses the legacy flat layout can be inspected and resumed through
``locate_session_data_dir``; no existing session is moved or rewritten.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_ROOT = "data"


def default_data_root() -> str:
    """Return the configured root for newly created workflow sessions."""
    return os.environ.get("GENE_AGENT_DATA_ROOT", DEFAULT_DATA_ROOT)


@dataclass(frozen=True)
class SessionPaths:
    """Paths owned by one new-layout workflow session."""

    data_root: Path
    session_id: str

    def __init__(self, data_root: str | Path, session_id: str) -> None:
        object.__setattr__(self, "data_root", Path(data_root).expanduser().resolve(strict=False))
        object.__setattr__(self, "session_id", session_id)

    @property
    def run_dir(self) -> Path:
        return self.data_root / "runs" / self.session_id

    @property
    def control_dir(self) -> Path:
        return self.run_dir / "control"

    @property
    def artifacts_dir(self) -> Path:
        return self.run_dir / "artifacts"

    @property
    def work_dir(self) -> Path:
        return self.run_dir / "work"

    @property
    def ledger_dir(self) -> Path:
        return self.run_dir / "ledger"

    @property
    def checkpoints_dir(self) -> Path:
        return self.run_dir / "checkpoints"

    @property
    def manifest_path(self) -> Path:
        return self.control_dir / "manifest.yaml"

    @property
    def ask_plan_path(self) -> Path:
        return self.control_dir / "ask_plan.json"

    @property
    def run_config_path(self) -> Path:
        return self.control_dir / "run_config.json"


def locate_session_data_dir(data_root: str | Path, session_id: str) -> Path | None:
    """Find a session's active layout without relocating legacy records.

    New-layout sessions take precedence.  The legacy flat root remains a
    read-only fallback so old ``inspect`` and ``resume`` invocations keep
    working after the layout change.
    """
    if is_session_run_dir(data_root, session_id):
        path = Path(data_root).expanduser().resolve(strict=False)
        if path.exists():
            return path
        return None
    paths = SessionPaths(data_root, session_id)
    if paths.run_dir.exists():
        return paths.run_dir
    root = paths.data_root
    if (root / "ledger" / session_id).exists() or (root / "artifacts" / session_id).exists():
        return root
    return None


def is_session_run_dir(data_dir: str | Path, session_id: str) -> bool:
    """Whether ``data_dir`` is the new-layout directory for ``session_id``."""
    path = Path(data_dir).expanduser().resolve(strict=False)
    return path.name == session_id and path.parent.name == "runs"


def artifact_index_path(data_dir: str | Path, session_id: str) -> Path:
    """Return the artifact index path for either supported layout."""
    root = Path(data_dir)
    if is_session_run_dir(root, session_id):
        return root / "artifacts" / "artifacts.jsonl"
    return root / "artifacts" / session_id / "artifacts.jsonl"


def ledger_session_dir(data_dir: str | Path, session_id: str) -> Path:
    """Return the ledger directory for either supported layout."""
    root = Path(data_dir)
    if is_session_run_dir(root, session_id):
        return root / "ledger"
    return root / "ledger" / session_id
