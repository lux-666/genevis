"""Canonical read-input contracts shared by workflow stages."""

from __future__ import annotations

from pathlib import Path
from typing import Literal


AssemblyReadInputType = Literal["long_reads.fofn", "clean_long_reads"]


class AssemblyInputError(ValueError):
    """Raised when an assembly read input violates the runtime contract."""


def resolve_assembly_read_paths(
    input_ref: str | Path | None,
    input_type: AssemblyReadInputType | None,
) -> list[Path]:
    """Resolve one canonical runtime input into existing read files.

    Runtime input refs are absolute. FOFN entries may be relative, in which
    case they are anchored to the FOFN directory rather than the process CWD.
    """

    if input_ref is None or not str(input_ref).strip():
        raise AssemblyInputError("requires assembly input_ref")
    source = Path(input_ref).expanduser()
    if not source.is_absolute():
        raise AssemblyInputError(f"requires an absolute assembly input_ref: {source}")

    if input_type == "long_reads.fofn":
        paths = parse_fofn_paths(source)
    elif input_type == "clean_long_reads":
        paths = [source]
    else:
        raise AssemblyInputError(f"does not support input_type: {input_type}")

    if not paths:
        raise AssemblyInputError("requires at least one read path")

    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise AssemblyInputError(f"read files not found: {missing}")
    return paths


def parse_fofn_paths(fofn_ref: str | Path) -> list[Path]:
    """Parse one canonical FOFN, anchoring relative entries to its directory."""

    source = Path(fofn_ref).expanduser()
    if not source.is_absolute():
        raise AssemblyInputError(f"requires an absolute FOFN ref: {source}")
    if not source.is_file():
        raise AssemblyInputError(f"FOFN not found: {source}")
    try:
        entries = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AssemblyInputError(f"FOFN could not be read: {source}: {exc}") from exc

    paths: list[Path] = []
    for raw_entry in entries:
        entry = raw_entry.strip()
        if not entry or entry.startswith("#"):
            continue
        path = Path(entry).expanduser()
        if not path.is_absolute():
            path = (source.parent / path).resolve(strict=False)
        paths.append(path)
    return paths
