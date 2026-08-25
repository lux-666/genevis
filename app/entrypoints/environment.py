"""Shared environment loading for CLI entrypoints and diagnostics."""

from __future__ import annotations

import os
from pathlib import Path


def candidate_dotenv_paths() -> list[Path]:
    """Return deterministic project-local dotenv candidates."""

    cwd = Path(os.getcwd()).resolve()
    repo_root = Path(__file__).resolve().parents[2]
    return list(dict.fromkeys([cwd / ".env", repo_root / ".env"]))


def load_dotenv_without_override(paths: list[Path] | None = None) -> Path | None:
    """Apply the first existing dotenv without overriding exported variables."""

    for path in paths if paths is not None else candidate_dotenv_paths():
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return path
    return None
