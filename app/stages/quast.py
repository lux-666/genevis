"""Validate comparison tables against the candidate roster frozen for QUAST."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from app.storage.paths import DataLayoutError


def read_quast_table(path: Path) -> list[list[str]]:
    """Validate report structure for both comparison and final evaluators."""
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream, delimiter="\t", strict=True))
    except (csv.Error, UnicodeError) as exc:
        raise DataLayoutError(f"QUAST roster/table mismatch: {exc}") from exc
    if (
        len(rows) < 2
        or len(rows[0]) < 2
        or rows[0][0] != "Assembly"
        or any(not cell.strip() for cell in rows[0])
        or len(set(rows[0][1:])) != len(rows[0]) - 1
        or any(
            len(row) != len(rows[0]) or any(not cell.strip() for cell in row)
            for row in rows[1:]
        )
        or len({row[0] for row in rows[1:] if row}) != len(rows) - 1
    ):
        raise DataLayoutError("QUAST roster/table mismatch")
    return rows


def validate_quast_comparison(path: Path, labels: Sequence[str]) -> None:
    """Treat malformed tool reports as expected collect failures."""
    rows = read_quast_table(path)
    if not labels or rows[0] != ["Assembly", *labels]:
        raise DataLayoutError("QUAST roster/table mismatch")
