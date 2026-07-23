"""Canonical query adapter for hifiasm evidence retrieval."""

from __future__ import annotations

import math
from typing import Any

from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
)


def build_hifiasm_retrieval_query(
    facts: CanonicalRecommendationFacts,
    context: AssemblyRecommendationContext,
    method_id: str,
    task: ParameterAttemptTask,
) -> dict[str, Any]:
    """Build a typed retrieval query from canonical facts and stage context."""

    if task != "recommend_initial":
        raise ValueError("hifiasm numeric retrieval only supports task='recommend_initial'")

    genome_fact = facts.genome.effective_genome_size
    genome_size_bp = genome_fact.value if genome_fact is not None else None

    coverage_fact = facts.sequencing.raw_coverage
    coverage = coverage_fact.value if coverage_fact is not None else None

    n50_fact = facts.reads.read_n50
    read_n50_bp = n50_fact.value if n50_fact is not None else None

    het_fact = facts.genome.heterozygosity
    heterozygosity = het_fact.value if het_fact is not None else None

    ploidy_fact = facts.species.ploidy
    ploidy = ploidy_fact.value if ploidy_fact is not None else None

    return {
        "method_id": method_id,
        "task": task,
        "read_type": context.read_type,
        "numeric_features": {
            "genome_size_mb": _number_record(
                _scale(genome_size_bp, 1_000_000),
                "facts.genome.effective_genome_size",
            ),
            "coverage": _number_record(
                coverage,
                "facts.sequencing.raw_coverage",
            ),
            "read_n50_kb": _number_record(
                _scale(read_n50_bp, 1_000),
                "facts.reads.read_n50",
            ),
            "heterozygosity": _number_record(
                heterozygosity,
                "facts.genome.heterozygosity",
            ),
            "ploidy": _number_record(
                ploidy,
                "facts.species.ploidy",
            ),
        },
        "categorical_context": {"clade": facts.species.clade},
    }


def _number_record(value: Any, source_ref: str | None) -> dict[str, float | str | None]:
    return {
        "value": float(value) if _is_number(value) else None,
        "source_ref": source_ref,
    }


def _scale(value: Any, denominator: float) -> float | None:
    return float(value) / denominator if _is_number(value) else None


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
