"""Materialize the durable v2 layout for one workflow session."""

from __future__ import annotations

import shutil
from pathlib import Path

from app.storage.paths import SessionPaths, ensure_data_layout, ensure_run_layout


def materialize_run_layout(
    paths: SessionPaths,
    *,
    manifest_path: Path,
    run_config_path: Path | None,
) -> None:
    """Create a run layout and copy entry files into its control directory.

    The helper is safe to call when an entry file is already at its canonical
    destination, which is the normal path after ``ask`` materializes a source
    manifest.  Distinct source files are copied on every invocation so the
    destination always reflects the current request.
    """
    ensure_data_layout(paths.data_root)
    ensure_run_layout(paths)
    _copy_if_distinct(manifest_path, paths.manifest_path)
    if run_config_path is not None:
        _copy_if_distinct(run_config_path, paths.run_config_path)


def _copy_if_distinct(source: Path, destination: Path) -> None:
    """Copy ``source`` unless both paths identify the same existing file."""
    source = Path(source)
    destination = Path(destination)
    if source.exists() and source.resolve(strict=False) == destination.resolve(
        strict=False
    ):
        return
    try:
        if source.samefile(destination):
            return
    except (FileNotFoundError, OSError):
        pass
    shutil.copy2(source, destination)
