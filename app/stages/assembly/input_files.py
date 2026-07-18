"""Canonical resolution of read files consumed by assembly methods."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AssemblyInputError(ValueError):
    """Raised when an assembly read input cannot be resolved."""


def resolve_assembly_read_paths(input_ref: Any, input_type: Any) -> list[Path]:
    """Resolve an assembly input reference into existing read paths.

    Relative entries in a FOFN are interpreted relative to the directory that
    contains the FOFN, independent of the process working directory.
    """

    if not input_ref:
        raise AssemblyInputError("requires assembly input_ref")

    if input_type == "long_reads.fofn":
        fofn = Path(str(input_ref))
        if not fofn.exists():
            raise AssemblyInputError(f"FOFN not found: {fofn}")
        try:
            entries = fofn.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AssemblyInputError(f"FOFN could not be read: {fofn}: {exc}") from exc

        paths = []
        for raw_entry in entries:
            entry = raw_entry.strip()
            if not entry:
                continue
            path = Path(entry)
            if not path.is_absolute():
                path = (fofn.parent / path).resolve(strict=False)
            paths.append(path)
    elif input_type == "clean_long_reads":
        paths = [Path(str(input_ref))]
    else:
        raise AssemblyInputError(f"does not support input_type: {input_type}")

    if not paths:
        raise AssemblyInputError("requires at least one read path")

    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise AssemblyInputError(f"read paths not found: {missing}")
    return paths
