"""Validate comparison tables against the candidate roster frozen for QUAST."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from app.storage.paths import DataLayoutError


def validate_quast_comparison(path: Path, labels: Sequence[str]) -> None:
    """Treat malformed tool reports as expected collect failures."""
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream, delimiter="\t", strict=True))
    except (csv.Error, UnicodeError) as exc:
        raise DataLayoutError(f"QUAST roster/table mismatch: {exc}") from exc
    if (
        not labels
        or len(set(labels)) != len(labels)
        or len(rows) < 2
        or rows[0] != ["Assembly", *labels]
        or any(
            len(row) != len(labels) + 1 or any(not cell.strip() for cell in row)
            for row in rows[1:]
        )
        or len({row[0] for row in rows[1:] if row}) != len(rows) - 1
    ):
        raise DataLayoutError("QUAST roster/table mismatch")
