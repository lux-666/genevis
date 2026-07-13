"""Canonical query adapter for hifiasm evidence retrieval."""

from __future__ import annotations

import math
from typing import Any

from app.domains.assembly.profile_paths import lookup_mapping_path as _lookup
from app.domains.assembly.selection import parameter_case_read_type


def build_hifiasm_retrieval_query(
    prompt_profile: dict[str, Any],
    *,
    method_id: str,
    task: str,
    query_id: str | None = None,
) -> dict[str, Any]:
    """Normalize initial-profile paths and units before deterministic retrieval."""

    if task != "recommend_initial":
        raise ValueError("hifiasm numeric retrieval only supports task='recommend_initial'")

    return _build_query(prompt_profile, method_id=method_id, task=task, query_id=query_id)


def _build_query(
    prompt_profile: dict[str, Any], *, method_id: str, task: str, query_id: str | None
) -> dict[str, Any]:

    genome_size_bp = _lookup(prompt_profile, "genome_survey.estimated_genome_size")
    read_n50_bp = _lookup(prompt_profile, "read_qc.read_n50")
    coverage, coverage_ref = _first_number_with_ref(
        (
            _lookup(prompt_profile, "read_qc.effective_coverage_1kb"),
            "prompt_profile.read_qc.effective_coverage_1kb",
        ),
        (
            _lookup(prompt_profile, "read_qc.effective_coverage_5kb"),
            "prompt_profile.read_qc.effective_coverage_5kb",
        ),
        (
            _lookup(prompt_profile, "genome_survey.kmer_peak_coverage"),
            "prompt_profile.genome_survey.kmer_peak_coverage",
        ),
    )
    query = {
        "query_id": query_id,
        "method_id": method_id,
        "task": task,
        "read_type": parameter_case_read_type(
            _lookup(prompt_profile, "assets.read_type")
        ),
        "numeric_features": {
            "genome_size_mb": _number_record(
                _scale(genome_size_bp, 1_000_000),
                "prompt_profile.genome_survey.estimated_genome_size",
            ),
            "coverage": _number_record(coverage, coverage_ref),
            "read_n50_kb": _number_record(
                _scale(read_n50_bp, 1_000),
                "prompt_profile.read_qc.read_n50",
            ),
            "heterozygosity": _number_record(
                _lookup(prompt_profile, "genome_survey.heterozygosity"),
                "prompt_profile.genome_survey.heterozygosity",
            ),
            "ploidy": _number_record(
                _lookup(prompt_profile, "sample.ploidy"),
                "prompt_profile.sample.ploidy",
            ),
        },
        "categorical_context": {"clade": _lookup(prompt_profile, "sample.clade")},
    }
    return query


def _number_record(value: Any, source_ref: str | None) -> dict[str, float | str | None]:
    return {
        "value": float(value) if _is_number(value) else None,
        "source_ref": source_ref,
    }


def _scale(value: Any, denominator: float) -> float | None:
    return float(value) / denominator if _is_number(value) else None


def _first_number_with_ref(*values: tuple[Any, str]) -> tuple[float | None, str | None]:
    for value, source_ref in values:
        if _is_number(value):
            return float(value), source_ref
    return None, None


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
