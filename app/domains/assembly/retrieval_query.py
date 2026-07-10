"""Canonical query adapter for hifiasm EvidenceCase V2 retrieval."""

from __future__ import annotations

from typing import Any


def build_hifiasm_retrieval_query(
    prompt_profile: dict[str, Any],
    *,
    method_id: str,
    task: str,
) -> dict[str, Any]:
    """Normalize profile paths and units before deterministic retrieval."""

    genome_size_bp = _lookup(prompt_profile, "genome_survey.estimated_genome_size")
    read_n50_bp = _lookup(prompt_profile, "read_qc.read_n50")
    coverage = _first_number(
        _lookup(prompt_profile, "read_qc.effective_coverage_1kb"),
        _lookup(prompt_profile, "read_qc.effective_coverage_5kb"),
        _lookup(prompt_profile, "genome_survey.kmer_peak_coverage"),
    )
    return {
        "method_id": method_id,
        "task": task,
        "read_type": _lookup(prompt_profile, "assets.read_type"),
        "assembly_goal": _lookup(prompt_profile, "sample.assembly_goal") or "primary",
        "numeric_features": {
            "genome_size_mb": _number_record(_scale(genome_size_bp, 1_000_000)),
            "coverage": _number_record(coverage),
            "read_n50_kb": _number_record(_scale(read_n50_bp, 1_000)),
            "heterozygosity": _number_record(_lookup(prompt_profile, "genome_survey.heterozygosity")),
            "ploidy": _number_record(_lookup(prompt_profile, "sample.ploidy")),
        },
        "categorical_context": {"clade": _lookup(prompt_profile, "sample.clade")},
    }


def _lookup(values: Any, path: str) -> Any:
    current = values
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _number_record(value: Any) -> dict[str, float | None]:
    return {"value": float(value) if _is_number(value) else None}


def _scale(value: Any, denominator: float) -> float | None:
    return float(value) / denominator if _is_number(value) else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        if _is_number(value):
            return float(value)
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
