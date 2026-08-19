"""Shared execution lifecycle for read-QC command method subgraphs."""

from __future__ import annotations

import csv
import math
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path

from app.schemas.execution import ArtifactInputDependency, InputDependency
from app.schemas.manifest import _detect_format
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.read_qc.state import (
    ReadQCStageCursor,
    SeqkitSummaryJson,
    read_read_qc_stage_output,
    update_read_qc_stage_output,
)
from app.storage.paths import DataLayoutError


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


def direct_read_set_provenance(
    input_dependencies: Iterable[InputDependency],
    *,
    read_set: str,
) -> dict[str, object]:
    """Return the durable direct-input proof for a whole-read-set aggregate.

    Artifact-index ``dependency_refs`` can only name run-owned artifacts.
    This provenance payload preserves the ordered asset/artifact byte evidence
    that the frozen command actually consumed, including external assets.
    """

    return {
        "kind": "direct_whole_read_set",
        "read_set": read_set,
        "input_dependencies": [
            dependency.model_dump(mode="json") for dependency in input_dependencies
        ],
    }


def direct_read_set_dependency_refs(
    input_dependencies: Iterable[InputDependency],
    *,
    asset_registry_ref: object | None = None,
) -> list[dict[str, object]]:
    """Return indexable direct-input refs in the same order as dependencies."""

    dependencies = list(input_dependencies)
    refs = [
        dependency.artifact_ref.model_dump(mode="json")
        for dependency in dependencies
        if isinstance(dependency, ArtifactInputDependency)
    ]
    if any(dependency.kind == "asset" for dependency in dependencies) and isinstance(
        asset_registry_ref, dict
    ):
        return [dict(asset_registry_ref), *refs]
    return refs


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


def seqkit_aggregate_length_commands(
    input_args: str,
    *,
    aggregate_stats_path: Path,
) -> str:
    """Return one direct whole-read-set SeqKit aggregate command.

    SeqKit emits a row per path when passed multiple inputs.  Streaming the
    exact declared paths through ``seqkit seq`` into one stdin-backed
    ``seqkit stats -a`` invocation produces one aggregate row for every
    cardinality and retains all SeqKit-derived fields, including quality.
    """

    quoted_aggregate = shlex.quote(str(aggregate_stats_path))
    return f"seqkit seq {input_args} | seqkit stats -a -T - > {quoted_aggregate}\n"


def parse_seqkit_summary(
    stats_path: Path,
    *,
    aggregate_stats_path: Path | None = None,
) -> SeqkitSummaryJson:
    """Parse SeqKit statistics, preferring the whole-read-set aggregate row.

    ``seqkit stats`` writes one row for each input path. Those per-file rows
    are retained as a diagnostic artifact.  The supplied aggregate is generated
    directly from the exact input read set and is the sole source for every
    reported aggregate metric.  Per-file rows are used only for invariants.
    """

    if aggregate_stats_path is None:
        raise DataLayoutError(
            "SeqKit direct whole-read-set aggregate stats are required"
        )
    with stats_path.open("r", encoding="utf-8") as handle:
        per_file_rows = list(csv.DictReader(handle, delimiter="\t"))
    if not per_file_rows:
        raise DataLayoutError("SeqKit per-input stats must contain at least one row")
    with aggregate_stats_path.open("r", encoding="utf-8") as handle:
        aggregate_rows = list(csv.DictReader(handle, delimiter="\t"))
    if len(aggregate_rows) != 1:
        raise DataLayoutError(
            "SeqKit whole-read-set aggregate stats must contain exactly one row"
        )
    aggregate_row = aggregate_rows[0]
    aggregate_format_hint = _common_sequence_format(per_file_rows)
    aggregate_summary: SeqkitSummaryJson = {
        "files": len(per_file_rows),
        "num_seqs": parse_int(aggregate_row.get("num_seqs")),
        "sum_len": parse_int(aggregate_row.get("sum_len")),
        "total_bases": parse_int(aggregate_row.get("sum_len")),
        "min_len": parse_int(aggregate_row.get("min_len")),
        "avg_len": parse_float(aggregate_row.get("avg_len")),
        "max_len": parse_int(aggregate_row.get("max_len")),
        "n50": parse_int(row_value(aggregate_row, "N50", "n50")),
        "q20_percent": quality_value(
            aggregate_row,
            "Q20(%)",
            "q20_percent",
            "Q20",
            sequence_format_hint=aggregate_format_hint,
        ),
        "q30_percent": quality_value(
            aggregate_row,
            "Q30(%)",
            "q30_percent",
            "Q30",
            sequence_format_hint=aggregate_format_hint,
        ),
        "avg_qual": quality_value(
            aggregate_row,
            "AvgQual",
            "avg_qual",
            sequence_format_hint=aggregate_format_hint,
        ),
    }
    _validate_aggregate_invariants(per_file_rows, aggregate_summary)
    return aggregate_summary


def _validate_aggregate_invariants(
    per_file_rows: list[dict[str, str]],
    aggregate: SeqkitSummaryJson,
) -> None:
    """Fail closed when direct aggregate totals contradict retained rows."""

    expected_num_seqs = sum_ints(row.get("num_seqs") for row in per_file_rows)
    expected_sum_len = sum_ints(row.get("sum_len") for row in per_file_rows)
    per_file_min_lens = [parse_int(row.get("min_len")) for row in per_file_rows]
    per_file_max_lens = [parse_int(row.get("max_len")) for row in per_file_rows]
    expected_min_len = min(
        (value for value in per_file_min_lens if value is not None),
        default=None,
    )
    expected_max_len = max(
        (value for value in per_file_max_lens if value is not None),
        default=None,
    )
    expected: dict[str, float | int | None] = {
        "num_seqs": expected_num_seqs,
        "sum_len": expected_sum_len,
        "min_len": expected_min_len,
        "max_len": expected_max_len,
        "avg_len": (
            expected_sum_len / expected_num_seqs
            if expected_num_seqs and expected_sum_len is not None
            else (0.0 if expected_num_seqs == 0 and expected_sum_len == 0 else None)
        ),
        "q20_percent": weighted_quality_average(
            per_file_rows, "Q20(%)", "q20_percent", "Q20"
        ),
        "q30_percent": weighted_quality_average(
            per_file_rows, "Q30(%)", "q30_percent", "Q30"
        ),
        "avg_qual": weighted_phred_average(per_file_rows, "AvgQual", "avg_qual"),
    }
    for key, expected_value in expected.items():
        actual_value = aggregate.get(key)
        if isinstance(expected_value, float):
            if (
                actual_value is None
                or not isinstance(actual_value, (float, int))
                or not math.isclose(
                    actual_value,
                    expected_value,
                    rel_tol=1e-6,
                    abs_tol=0.01,
                )
            ):
                raise DataLayoutError(
                    "SeqKit whole-read-set aggregate invariant failed: "
                    f"{key}={actual_value!r}, expected {expected_value!r}"
                )
        elif actual_value != expected_value:
            raise DataLayoutError(
                "SeqKit whole-read-set aggregate invariant failed: "
                f"{key}={actual_value!r}, expected {expected_value!r}"
            )


def row_value(row: dict[str, str], *keys: str) -> str | None:
    return next((row[key] for key in keys if key in row), None)


def parse_int(value: str | None) -> int | None:
    return int(value.replace(",", "")) if value not in {None, ""} else None


def parse_float(value: str | None) -> float | None:
    return float(value.replace(",", "")) if value not in {None, ""} else None


def quality_value(
    row: dict[str, str],
    *keys: str,
    sequence_format_hint: str | None = None,
) -> float | None:
    """Normalize one SeqKit quality metric across aggregate and per-file rows.

    SeqKit 2.13.0 reports an empty FASTQ read set with zero quality values,
    while retained TSVs can also contain blank quality cells from older runs.
    Both encode the same no-read FASTQ result. FASTA quality remains
    inapplicable, including for an empty FASTA input.
    """

    sequence_format = _sequence_format(row) or sequence_format_hint
    if sequence_format == "fasta":
        return None
    if parse_int(row.get("num_seqs")) == 0 and parse_int(row.get("sum_len")) == 0:
        return 0.0
    return parse_float(row_value(row, *keys))


def _common_sequence_format(rows: Iterable[dict[str, str]]) -> str | None:
    """Infer the aggregate format only when every retained row agrees."""

    formats = [_sequence_format(row) for row in rows]
    if not formats or formats[0] is None:
        return None
    return formats[0] if all(value == formats[0] for value in formats[1:]) else None


def _sequence_format(row: dict[str, str]) -> str | None:
    """Recover formats that SeqKit 2.13 omits for zero-sequence files."""

    explicit = str(row_value(row, "format", "Format") or "").strip().lower()
    if explicit:
        return explicit
    source = str(row_value(row, "file", "File") or "").strip().lower()
    for suffix in (".gz", ".bz2", ".xz"):
        if source.endswith(suffix):
            source = source[: -len(suffix)]
            break
    if source.endswith((".fa", ".fasta", ".fna", ".ffn", ".faa", ".frn")):
        return "fasta"
    if source.endswith((".fq", ".fastq")):
        return "fastq"
    return None


def sum_ints(values: Iterable[str | None]) -> int | None:
    parsed = [parse_int(value) for value in values]
    return (
        sum(value for value in parsed if value is not None)
        if any(value is not None for value in parsed)
        else None
    )


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


def weighted_quality_average(rows: list[dict[str, str]], *keys: str) -> float | None:
    weighted: list[tuple[float, int]] = []
    normalized_values: list[float | None] = []
    for row in rows:
        value = quality_value(row, *keys)
        normalized_values.append(value)
        count = parse_int(row.get("sum_len")) or parse_int(row.get("num_seqs"))
        if value is not None and count is not None and count > 0:
            weighted.append((value, count))
    if weighted:
        return sum(value * count for value, count in weighted) / sum(
            count for _, count in weighted
        )
    return next((value for value in normalized_values if value is not None), None)


def weighted_phred_average(rows: list[dict[str, str]], *keys: str) -> float | None:
    """Combine SeqKit AvgQual values through their per-base error rates."""

    weighted_errors: list[tuple[float, int]] = []
    normalized_values: list[float | None] = []
    for row in rows:
        quality = quality_value(row, *keys)
        normalized_values.append(quality)
        bases = parse_int(row.get("sum_len"))
        if quality is not None and bases is not None and bases > 0:
            weighted_errors.append((10 ** (-quality / 10), bases))
    if not weighted_errors:
        return next((value for value in normalized_values if value is not None), None)
    mean_error = sum(error * bases for error, bases in weighted_errors) / sum(
        bases for _, bases in weighted_errors
    )
    return -10 * math.log10(mean_error) if mean_error > 0 else None
