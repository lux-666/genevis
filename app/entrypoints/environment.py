"""Shared process bootstrap for CLI-style entrypoints."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv_without_override() -> Path | None:
    """Load the first project ``.env`` without replacing exported variables.

    Returning the selected path lets diagnostics report configuration
    provenance without ever exposing secret values.
    """

    for path in candidate_dotenv_paths():
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


def candidate_dotenv_paths() -> list[Path]:
    cwd = Path(os.getcwd()).resolve()
    repo_root = Path(__file__).resolve().parents[2]
    return list(dict.fromkeys([cwd / ".env", repo_root / ".env"]))
