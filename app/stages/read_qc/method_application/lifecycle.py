"""Shared execution lifecycle for read-QC command method subgraphs."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.execution.evidence import (
    load_frozen_attempt,
    persist_execution_result,
)
from app.execution.method_runner import run_frozen_attempt
from app.schemas.manifest import _detect_format
from app.schemas.state import TopLevelState


@dataclass(frozen=True)
class ReadQCMethodLifecycle:
    """Status and state-key convention for one executable read-QC method."""

    name: str
    frozen_status: str
    ran_status: str
    frozen_attempt_key: str
    execution_result_key: str


def build_method_graph(
    lifecycle: ReadQCMethodLifecycle,
    freeze: Callable[[TopLevelState], dict],
    collect: Callable[[TopLevelState], dict],
):
    """Build the common freeze -> run -> collect method graph."""
    graph = StateGraph(TopLevelState)
    graph.add_node("freeze", freeze)
    graph.add_node("run", lambda state: run_method_attempt(state, lifecycle))
    graph.add_node("collect", collect)
    graph.add_node("done", lambda _state: {})
    graph.add_edge(START, "freeze")
    graph.add_conditional_edges(
        "freeze",
        lambda state: "run" if read_qc_output(state).get("status") == lifecycle.frozen_status else "done",
        {"run": "run", "done": "done"},
    )
    graph.add_conditional_edges(
        "run",
        lambda state: "collect" if read_qc_output(state).get("status") == lifecycle.ran_status else "done",
        {"collect": "collect", "done": "done"},
    )
    graph.add_edge("collect", "done")
    graph.add_edge("done", END)
    return graph.compile()


def read_qc_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("read_qc", {}))


def update_read_qc_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["read_qc"] = {**read_qc_output(state), **payload}
    return {"stage_outputs": outputs}


def fail_update(state: TopLevelState, reason: str) -> dict:
    return update_read_qc_output(state, {"status": "failed", "reason": reason})


def run_method_attempt(state: TopLevelState, lifecycle: ReadQCMethodLifecycle) -> dict:
    output = read_qc_output(state)
    if output.get("status") != lifecycle.frozen_status:
        return {}
    attempt = load_frozen_attempt(
        state,
        output[lifecycle.frozen_attempt_key],
        expected_stage="read_qc",
    )
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=lifecycle.name,
    )
    result_ref = persist_execution_result(
        state,
        stage="read_qc",
        method_name=attempt.method_name,
        result=outcome.result,
        error=outcome.error,
    ).model_dump(mode="json")
    if not outcome.success:
        return update_read_qc_output(state, {"status": "failed", "reason": outcome.error, lifecycle.execution_result_key: result_ref})
    return update_read_qc_output(state, {"status": lifecycle.ran_status, lifecycle.execution_result_key: result_ref})


def read_paths_from_output(output: dict[str, Any], plural_key: str, single_key: str) -> list[Path]:
    values = output.get(plural_key)
    if values:
        return [Path(str(value)) for value in values]
    value = output.get(single_key)
    return [Path(str(value))] if value else []


def filtered_long_reads_path(artifact_dir: Path, input_path: Path, *, index: int, total: int, fmt: str | None) -> Path:
    resolved_format = fmt or _detect_format(input_path)
    ext = ".fasta" if resolved_format == "fasta" else ".fastq"
    return artifact_dir / (f"filtered_long_reads{ext}" if total == 1 else f"filtered_long_reads_{index}{ext}")


def parse_seqkit_summary(stats_path: Path) -> dict[str, Any]:
    with stats_path.open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        return {}
    if len(rows) == 1:
        row = rows[0]
        sum_len = parse_int(row.get("sum_len"))
        return {"num_seqs": parse_int(row.get("num_seqs")), "sum_len": sum_len, "total_bases": sum_len,
                "min_len": parse_int(row.get("min_len")), "avg_len": parse_float(row.get("avg_len")),
                "max_len": parse_int(row.get("max_len")), "n50": parse_int(row_value(row, "N50", "n50")),
                "q20_percent": parse_float(row_value(row, "Q20(%)", "q20_percent", "Q20")),
                "q30_percent": parse_float(row_value(row, "Q30(%)", "q30_percent", "Q30")),
                "avg_qual": parse_float(row_value(row, "AvgQual", "avg_qual"))}
    num_seqs = sum_ints(row.get("num_seqs") for row in rows)
    sum_len = sum_ints(row.get("sum_len") for row in rows)
    min_lens = [parse_int(row.get("min_len")) for row in rows]
    max_lens = [parse_int(row.get("max_len")) for row in rows]
    return {"files": len(rows), "num_seqs": num_seqs, "sum_len": sum_len, "total_bases": sum_len,
            "min_len": min((item for item in min_lens if item is not None), default=None),
            "avg_len": (sum_len / num_seqs) if num_seqs and sum_len is not None else None,
            "max_len": max((item for item in max_lens if item is not None), default=None),
            "n50": max_int(row_value(row, "N50", "n50") for row in rows),
            "q20_percent": weighted_average(rows, "Q20(%)", "q20_percent", "Q20"),
            "q30_percent": weighted_average(rows, "Q30(%)", "q30_percent", "Q30"),
            "avg_qual": weighted_average(rows, "AvgQual", "avg_qual")}


def row_value(row: dict[str, str], *keys: str) -> str | None:
    return next((row[key] for key in keys if key in row), None)


def parse_int(value: str | None) -> int | None:
    return int(value.replace(",", "")) if value not in {None, ""} else None


def parse_float(value: str | None) -> float | None:
    return float(value.replace(",", "")) if value not in {None, ""} else None


def sum_ints(values) -> int | None:
    parsed = [parse_int(value) for value in values]
    return sum(value for value in parsed if value is not None) if any(value is not None for value in parsed) else None


def max_int(values) -> int | None:
    parsed = [parse_int(value) for value in values]
    return max((value for value in parsed if value is not None), default=None)


def weighted_average(rows: list[dict[str, str]], *keys: str) -> float | None:
    weighted = [
        (parse_float(row_value(row, *keys)), parse_int(row.get("sum_len")) or parse_int(row.get("num_seqs")))
        for row in rows
    ]
    weighted = [(value, count) for value, count in weighted if value is not None and count]
    return sum(value * count for value, count in weighted) / sum(count for _, count in weighted) if weighted else None
