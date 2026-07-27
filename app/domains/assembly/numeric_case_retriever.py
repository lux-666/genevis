"""Continuous, missing-aware retrieval for approved hifiasm EvidenceCases."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, TypeGuard

from app.domains.assembly.evidence_case import (
    _rank_hifiasm_prompt_case,
    project_hifiasm_retrieval_case,
)
from app.knowledge.selectors import SelectionResult

_FEATURES = ("genome_size_mb", "coverage", "read_n50_kb", "heterozygosity", "ploidy")
_DEFAULT_SCALES = {
    "genome_size_mb": math.log(2),
    "coverage": math.log(2),
    "read_n50_kb": math.log(2),
    "heterozygosity": math.log(2),
    "ploidy": 1.0,
}
_DEFAULT_WEIGHTS = {feature: 1.0 for feature in _FEATURES}
_CONFIG_KEYS = {
    "weak_coverage_threshold",
    "distance_clip",
    "epsilon",
    "scales",
    "weights",
}


class HifiasmNumericCaseRetriever:
    """Rank validated literature cases by shared biological measurements.

    Method, read type, route, assembly goal, and taxonomy are documentary
    context for the LLM.  They deliberately do not filter or score a paper.
    """

    def __init__(
        self,
        *,
        max_cases: int = 12,
        weak_coverage_threshold: float = 0.6,
        distance_clip: float = 4.0,
        epsilon: float = 1e-6,
        scales: dict[str, float] | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        if scales is not None and not isinstance(scales, dict):
            raise ValueError("scales must be a mapping")
        if weights is not None and not isinstance(weights, dict):
            raise ValueError("weights must be a mapping")
        self.max_cases = max_cases
        self.weak_coverage_threshold = weak_coverage_threshold
        self.distance_clip = distance_clip
        self.epsilon = epsilon
        self.scales = {**_DEFAULT_SCALES, **(scales or {})}
        self.weights = {**_DEFAULT_WEIGHTS, **(weights or {})}
        self._validate_configuration()

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None,
        *,
        max_cases: int | None = None,
    ) -> "HifiasmNumericCaseRetriever":
        """Build a retriever from the knowledge-pack numeric strategy."""

        raw = {} if config is None else config
        if not isinstance(raw, dict):
            raise ValueError("hifiasm_numeric strategy must be a mapping")
        unknown = set(raw) - _CONFIG_KEYS
        if unknown:
            raise ValueError(
                f"hifiasm_numeric strategy contains unknown keys: {sorted(unknown)}"
            )
        return cls(
            max_cases=12 if max_cases is None else max_cases,
            weak_coverage_threshold=raw.get("weak_coverage_threshold", 0.6),
            distance_clip=raw.get("distance_clip", 4.0),
            epsilon=raw.get("epsilon", 1e-6),
            scales=raw.get("scales"),
            weights=raw.get("weights"),
        )

    def _validate_configuration(self) -> None:
        if (
            not isinstance(self.max_cases, int)
            or isinstance(self.max_cases, bool)
            or self.max_cases < 1
        ):
            raise ValueError("max_cases must be positive")
        for name, value in (
            ("weak_coverage_threshold", self.weak_coverage_threshold),
            ("distance_clip", self.distance_clip),
            ("epsilon", self.epsilon),
        ):
            if not _is_number(value):
                raise ValueError(f"{name} must be a number")
        if not 0 <= self.weak_coverage_threshold <= 1:
            raise ValueError("weak_coverage_threshold must be between 0 and 1")
        if self.distance_clip <= 0:
            raise ValueError("distance_clip must be positive")
        if self.epsilon <= 0:
            raise ValueError("epsilon must be positive")
        _validate_feature_numbers("scales", self.scales, positive=True)
        _validate_feature_numbers("weights", self.weights, positive=False)
        if not any(weight > 0 for weight in self.weights.values()):
            raise ValueError("at least one numeric feature weight must be positive")

    def select(
        self, cases: list[dict[str, Any]], *, query: dict[str, Any]
    ) -> SelectionResult:
        if query.get("task") != "recommend_initial":
            raise ValueError(
                "hifiasm numeric retrieval only supports task='recommend_initial'"
            )

        trace: list[dict[str, Any]] = []
        ranked: list[tuple[float, float, str, dict[str, Any], dict[str, Any]]] = []
        for raw_case in cases:
            case = project_hifiasm_retrieval_case(raw_case)
            case_id = str(case.get("case_id") or "")
            reasons = _eligibility_reasons(case)
            if reasons:
                trace.append(
                    {
                        "case_id": case_id,
                        "decision": "excluded",
                        "eligibility_reasons": reasons,
                        "numeric_distance": None,
                        "numeric_similarity_display": None,
                        "evidence_coverage": 0.0,
                        "weak_evidence": True,
                        "actionability_score": _actionability_score(case),
                        "feature_distances": _not_compared_features(
                            case,
                            query,
                            weights=self.weights,
                        ),
                    }
                )
                continue

            numeric = self._numeric_match(case, query)
            numeric["actionability_score"] = _actionability_score(case)
            numeric["ranking_score"] = float(numeric["numeric_similarity_display"])
            ranked.append(
                (
                    -float(numeric["ranking_score"]),
                    -float(numeric["actionability_score"]),
                    case_id,
                    case,
                    numeric,
                )
            )

        ranked.sort(key=lambda item: item[:3])
        selected_entries = ranked[: self.max_cases]
        selected_ids = {entry[2] for entry in selected_entries}
        selected_cases: list[dict[str, Any]] = []
        for rank, entry in enumerate(ranked, start=1):
            _, _, case_id, case, numeric = entry
            decision = "candidate" if case_id in selected_ids else "not_selected"
            trace_entry = {
                "case_id": case_id,
                "decision": decision,
                "eligibility_reasons": [],
                "rank_numeric": rank,
                **numeric,
            }
            trace.append(trace_entry)
            if decision == "candidate":
                selected_cases.append(
                    _prompt_safe_case(case, numeric_match=trace_entry)
                )

        selected_cases.sort(
            key=lambda case: next(
                item.get("rank_numeric", math.inf)
                for item in trace
                if item.get("case_id") == case.get("case_id")
            )
        )
        evidence_available = bool(selected_cases)
        weak_selected = bool(selected_cases) and any(
            case["numeric_match"].get("weak_evidence") for case in selected_cases
        )
        warnings: list[str] = []
        if not evidence_available:
            warnings.append(
                "No eligible approved literature cases; generate the initial "
                "recommendation "
                "from the current sample profile and method guidance."
            )
        else:
            if len(selected_cases) < self.max_cases:
                warnings.append(
                    f"Only {len(selected_cases)} of {self.max_cases} requested "
                    "literature "
                    "cases were available."
                )
            if weak_selected:
                warnings.append(
                    "Some selected literature cases share few measured biological "
                    "fields "
                    "with the current sample; let the LLM assess their applicability."
                )
        warning = " ".join(warnings) or None

        metadata = {
            "retrieval_backend": "hifiasm_numeric",
            "selected_case_ids": [case["case_id"] for case in selected_cases],
            "candidate_case_ids": [case["case_id"] for case in selected_cases],
            "selected_count": len(selected_cases),
            "eligible_case_count": len(ranked),
            "total_cases": len(cases),
            "evidence_available": evidence_available,
            "warning": warning,
            "query": deepcopy(query),
            "trace": trace,
        }
        return SelectionResult(
            selected_cases=selected_cases, metadata=metadata, trace=trace
        )

    def _numeric_match(
        self, case: dict[str, Any], query: dict[str, Any]
    ) -> dict[str, Any]:
        query_features = query.get("numeric_features") or {}
        observed_facts = case.get("observed_facts") or {}
        feature_distances: dict[str, dict[str, Any]] = {}
        weighted_distance = 0.0
        present_weight = 0.0
        total_weight = sum(self.weights.values())

        for feature in _FEATURES:
            query_value = _fact_value(query_features.get(feature))
            case_fact = observed_facts.get(feature)
            case_value = _fact_value(case_fact)
            case_display = _case_display_value(case_fact)
            weight = self.weights[feature]
            if query_value is None:
                feature_distances[feature] = {
                    "query": None,
                    "case": case_display,
                    "distance": None,
                    "weight": weight,
                    "status": "missing_query_value",
                }
                continue
            interval_usable = feature != "ploidy" and _has_interval(case_fact)
            if case_value is None and not interval_usable:
                feature_distances[feature] = {
                    "query": query_value,
                    "case": case_display,
                    "distance": None,
                    "weight": weight,
                    "status": "missing_case_value",
                }
                continue

            distance, comparable_case = self._feature_distance(
                feature, query_value, case_fact
            )
            robust_distance = min(distance, self.distance_clip)
            weighted_distance += weight * robust_distance
            present_weight += weight
            feature_distances[feature] = {
                "query": query_value,
                "case": comparable_case,
                "distance": robust_distance,
                "raw_distance": distance,
                "weight": weight,
                "status": "compared",
            }

        numeric_distance = (
            weighted_distance / present_weight if present_weight else None
        )
        coverage = present_weight / total_weight
        return {
            "numeric_distance": numeric_distance,
            "numeric_similarity_display": math.exp(-numeric_distance)
            if numeric_distance is not None
            else 0.0,
            "evidence_coverage": coverage,
            "weak_evidence": coverage < self.weak_coverage_threshold,
            "feature_distances": feature_distances,
        }

    def _feature_distance(
        self, feature: str, query_value: float, case_fact: Any
    ) -> tuple[float, Any]:
        if feature == "ploidy":
            case_value = _fact_value(case_fact)
            if case_value is None:
                raise ValueError("ploidy comparison requires a numeric case value")
            return (0.0 if query_value == case_value else 1.0), case_value

        comparison_value: float
        interval = _interval(case_fact)
        if interval is not None:
            low, high = interval
            if low <= query_value <= high:
                return 0.0, {"min": low, "max": high}
            comparison_value = low if query_value < low else high
        else:
            case_value = _fact_value(case_fact)
            if case_value is None:
                raise ValueError(
                    f"{feature} comparison requires a numeric case value or interval"
                )
            comparison_value = case_value

        scale = float(self.scales[feature])
        if feature == "heterozygosity":
            if query_value < 0 or comparison_value < 0:
                distance = self.distance_clip
            else:
                distance = (
                    abs(
                        math.log(
                            (query_value + self.epsilon)
                            / (comparison_value + self.epsilon)
                        )
                    )
                    / scale
                )
        else:
            if query_value <= 0 or comparison_value <= 0:
                distance = self.distance_clip
            else:
                distance = abs(math.log(query_value / comparison_value)) / scale
        return distance, comparison_value


def _eligibility_reasons(case: dict[str, Any]) -> list[str]:
    """Return corpus-integrity failures, never sample compatibility judgments."""

    reasons: list[str] = []
    curation = case.get("curation") or {}
    if curation.get("status") != "approved":
        reasons.append("curation_not_approved")
    elif case.get("retrieval_eligible") is not True:
        reasons.append("source_not_validated")
    return reasons


def _actionability_score(case: dict[str, Any]) -> float:
    """Tie-break papers by whether parameter and rationale claims link to quotes."""

    intervention = case.get("intervention") or {}
    parameters = intervention.get("parameters") or {}
    rationale_claims = case.get("rationale_claims") or []
    has_parameter_settings = bool(parameters)
    has_parameter_quotes = any(
        isinstance(parameter, dict) and bool(parameter.get("evidence_span_ids"))
        for parameter in parameters.values()
    )
    has_rationale_quotes = any(
        isinstance(claim, dict) and bool(claim.get("evidence_span_ids"))
        for claim in rationale_claims
    )
    return (
        sum((has_parameter_settings, has_parameter_quotes, has_rationale_quotes)) / 3.0
    )


def _prompt_safe_case(
    case: dict[str, Any], *, numeric_match: dict[str, Any]
) -> dict[str, Any]:
    return _rank_hifiasm_prompt_case(
        case,
        numeric_match={
            key: value
            for key, value in numeric_match.items()
            if key not in {"case_id", "decision", "eligibility_reasons"}
        },
    )


def _fact_value(fact: Any) -> float | None:
    value = fact.get("value") if isinstance(fact, dict) else fact
    return float(value) if _is_number(value) else None


def _case_display_value(fact: Any) -> Any:
    value = _fact_value(fact)
    if value is not None:
        return value
    interval = _interval(fact)
    if interval is not None:
        return {"min": interval[0], "max": interval[1]}
    return None


def _not_compared_features(
    case: dict[str, Any],
    query: dict[str, Any],
    *,
    weights: dict[str, float],
) -> dict[str, dict[str, Any]]:
    query_features = query.get("numeric_features") or {}
    observed_facts = case.get("observed_facts") or {}
    return {
        feature: {
            "query": _fact_value(query_features.get(feature)),
            "case": _case_display_value(observed_facts.get(feature)),
            "distance": None,
            "weight": weights[feature],
            "status": "not_compared_ineligible",
        }
        for feature in _FEATURES
    }


def _has_interval(fact: Any) -> bool:
    return _interval(fact) is not None


def _interval(fact: Any) -> tuple[float, float] | None:
    if not isinstance(fact, dict):
        return None
    uncertainty = fact.get("uncertainty")
    if not isinstance(uncertainty, dict) or uncertainty.get("kind") != "interval":
        return None
    low = uncertainty.get("min")
    high = uncertainty.get("max")
    if not _is_number(low) or not _is_number(high) or float(low) > float(high):
        return None
    return float(low), float(high)


def _is_number(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_feature_numbers(
    section_name: str,
    values: dict[str, float],
    *,
    positive: bool,
) -> None:
    unknown = set(values) - set(_FEATURES)
    if unknown:
        raise ValueError(f"{section_name} contains unknown features: {sorted(unknown)}")
    missing = set(_FEATURES) - set(values)
    if missing:
        raise ValueError(f"{section_name} is missing features: {sorted(missing)}")
    for feature, value in values.items():
        if not _is_number(value):
            raise ValueError(f"{section_name}.{feature} must be a number")
        if positive and value <= 0:
            raise ValueError(f"{section_name}.{feature} must be positive")
        if not positive and value < 0:
            raise ValueError(f"{section_name}.{feature} must be non-negative")
