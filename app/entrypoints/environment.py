"""Shared environment loading for CLI entrypoints and diagnostics."""

from __future__ import annotations

import os
from pathlib import Path

from app.execution.env_resolver import active_config_path
from app.execution.project_bootstrap import project_root_for_config


def candidate_dotenv_paths() -> list[Path]:
    """Return the one dotenv owned by the active host configuration."""

    return [project_root_for_config(active_config_path()) / ".env"]


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
