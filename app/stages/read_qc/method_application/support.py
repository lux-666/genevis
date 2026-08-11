"""Shared execution lifecycle for read-QC command method subgraphs."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from pathlib import Path

from app.schemas.execution import InputDependency
from app.schemas.manifest import _detect_format
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.read_qc.state import (
    ReadQCStageCursor,
    SeqkitSummaryJson,
    read_read_qc_stage_output,
    update_read_qc_stage_output,
)


def read_qc_output(state: TopLevelState) -> ReadQCStageCursor:
    return read_read_qc_stage_output(state)


def update_read_qc_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    return update_read_qc_stage_output(state, payload)


def fail_update(state: TopLevelState, reason: str) -> StateUpdate:
    return update_read_qc_output(state, {"status": "failed", "reason": reason})


def read_paths_from_output(output: ReadQCStageCursor) -> list[Path]:
    values = output.get("raw_long_reads_paths")
    if values:
        return [Path(str(value)) for value in values]
    value = output.get("raw_long_reads_path")
    return [Path(str(value))] if value else []


def read_input_dependencies(
    state: TopLevelState,
    output: ReadQCStageCursor,
    paths: list[Path],
) -> list[InputDependency]:
    """Freeze reads according to their authoritative asset/artifact source."""

    from app.execution.input_integrity import (
        artifact_input_dependency,
        asset_input_dependencies,
    )

    artifact_refs = list(output.get("input_artifact_refs") or [])
    if artifact_refs:
        if len(artifact_refs) != len(paths):
            raise ValueError(
                "artifact input dependency cardinality mismatch: "
                f"{len(artifact_refs)} refs vs {len(paths)} paths"
            )
        return [
            artifact_input_dependency(state, artifact_ref, path)
            for artifact_ref, path in zip(artifact_refs, paths, strict=True)
        ]
    return list(
        asset_input_dependencies(
            state,
            list(output.get("input_asset_refs") or []),
            paths,
        )
    )


def filtered_long_reads_path(
    artifact_dir: Path, input_path: Path, *, index: int, total: int, fmt: str | None
) -> Path:
    resolved_format = fmt or _detect_format(input_path)
    ext = ".fasta" if resolved_format == "fasta" else ".fastq"
    return artifact_dir / (
        f"filtered_long_reads{ext}"
        if total == 1
        else f"filtered_long_reads_{index}{ext}"
    )


def parse_seqkit_summary(stats_path: Path) -> SeqkitSummaryJson:
    with stats_path.open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        return {}
    if len(rows) == 1:
        row = rows[0]
        sum_len = parse_int(row.get("sum_len"))
        return {
            "num_seqs": parse_int(row.get("num_seqs")),
            "sum_len": sum_len,
            "total_bases": sum_len,
            "min_len": parse_int(row.get("min_len")),
            "avg_len": parse_float(row.get("avg_len")),
            "max_len": parse_int(row.get("max_len")),
            "n50": parse_int(row_value(row, "N50", "n50")),
            "q20_percent": quality_value(row, "Q20(%)", "q20_percent", "Q20"),
            "q30_percent": quality_value(row, "Q30(%)", "q30_percent", "Q30"),
            "avg_qual": quality_value(row, "AvgQual", "avg_qual"),
        }
    num_seqs = sum_ints(row.get("num_seqs") for row in rows)
    sum_len = sum_ints(row.get("sum_len") for row in rows)
    min_lens = [parse_int(row.get("min_len")) for row in rows]
    max_lens = [parse_int(row.get("max_len")) for row in rows]
    return {
        "files": len(rows),
        "num_seqs": num_seqs,
        "sum_len": sum_len,
        "total_bases": sum_len,
        "min_len": min((item for item in min_lens if item is not None), default=None),
        "avg_len": (sum_len / num_seqs) if num_seqs and sum_len is not None else None,
        "max_len": max((item for item in max_lens if item is not None), default=None),
        "n50": max_int(row_value(row, "N50", "n50") for row in rows),
        "q20_percent": weighted_quality_average(
            rows, "Q20(%)", "q20_percent", "Q20"
        ),
        "q30_percent": weighted_quality_average(
            rows, "Q30(%)", "q30_percent", "Q30"
        ),
        "avg_qual": weighted_quality_average(rows, "AvgQual", "avg_qual"),
    }


def row_value(row: dict[str, str], *keys: str) -> str | None:
    return next((row[key] for key in keys if key in row), None)


def parse_int(value: str | None) -> int | None:
    return int(value.replace(",", "")) if value not in {None, ""} else None


def parse_float(value: str | None) -> float | None:
    return float(value.replace(",", "")) if value not in {None, ""} else None


def quality_value(row: dict[str, str], *keys: str) -> float | None:
    """Return a SeqKit quality metric, treating FASTA quality as not applicable."""

    sequence_format = str(row_value(row, "format", "Format") or "").lower()
    if sequence_format == "fasta":
        return None
    return parse_float(row_value(row, *keys))


def sum_ints(values: Iterable[str | None]) -> int | None:
    parsed = [parse_int(value) for value in values]
    return (
        sum(value for value in parsed if value is not None)
        if any(value is not None for value in parsed)
        else None
    )


def max_int(values: Iterable[str | None]) -> int | None:
    parsed = [parse_int(value) for value in values]
    return max((value for value in parsed if value is not None), default=None)


def weighted_average(rows: list[dict[str, str]], *keys: str) -> float | None:
    weighted: list[tuple[float, int]] = []
    for row in rows:
        value = parse_float(row_value(row, *keys))
        count = parse_int(row.get("sum_len")) or parse_int(row.get("num_seqs"))
        if value is not None and count is not None and count > 0:
            weighted.append((value, count))
    return (
        sum(value * count for value, count in weighted)
        / sum(count for _, count in weighted)
        if weighted
        else None
    )


def weighted_quality_average(
    rows: list[dict[str, str]], *keys: str
) -> float | None:
    weighted: list[tuple[float, int]] = []
    for row in rows:
        value = quality_value(row, *keys)
        count = parse_int(row.get("sum_len")) or parse_int(row.get("num_seqs"))
        if value is not None and count is not None and count > 0:
            weighted.append((value, count))
    return (
        sum(value * count for value, count in weighted)
        / sum(count for _, count in weighted)
        if weighted
        else None
    )
