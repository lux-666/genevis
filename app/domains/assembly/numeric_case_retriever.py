"""Continuous, missing-aware retrieval for approved hifiasm EvidenceCases."""

from __future__ import annotations

import math
from typing import Any

from app.knowledge.selectors import SelectionResult

_FEATURES = ("genome_size_mb", "coverage", "read_n50_kb", "heterozygosity", "ploidy")
_DEFAULT_SCALES = {
    "genome_size_mb": math.log(2),
    "coverage": math.log(2),
    "read_n50_kb": math.log(2),
    "heterozygosity": math.log(2),
    "ploidy": 1.0,
}


class HifiasmNumericCaseRetriever:
    """Apply hard compatibility filters before continuous numeric ranking."""

    def __init__(
        self,
        *,
        max_cases: int = 8,
        weak_coverage_threshold: float = 0.6,
        distance_clip: float = 4.0,
        epsilon: float = 1e-6,
        scales: dict[str, float] | None = None,
    ) -> None:
        self.max_cases = max_cases
        self.weak_coverage_threshold = weak_coverage_threshold
        self.distance_clip = distance_clip
        self.epsilon = epsilon
        self.scales = {**_DEFAULT_SCALES, **(scales or {})}

    def select(self, cases: list[dict[str, Any]], *, query: dict[str, Any]) -> SelectionResult:
        trace: list[dict[str, Any]] = []
        ranked: list[tuple[float, float, int, str, dict[str, Any], dict[str, Any]]] = []
        for case in cases:
            case_id = str(case.get("case_id") or "")
            reasons = _hard_filter_reasons(case, query)
            if reasons:
                trace.append(
                    {
                        "case_id": case_id,
                        "decision": "excluded",
                        "hard_filter_reasons": reasons,
                        "numeric_distance": None,
                        "numeric_similarity_display": None,
                        "evidence_coverage": 0.0,
                        "weak_evidence": True,
                        "feature_distances": {},
                    }
                )
                continue

            numeric = self._numeric_match(case, query)
            clade_penalty = _clade_tie_break(case, query)
            distance_key = numeric["numeric_distance"] if numeric["numeric_distance"] is not None else math.inf
            ranked.append(
                (
                    float(distance_key),
                    -float(numeric["evidence_coverage"]),
                    clade_penalty,
                    case_id,
                    case,
                    numeric,
                )
            )

        ranked.sort(key=lambda item: item[:4])
        selected_entries = ranked[: self.max_cases]
        selected_ids = {entry[3] for entry in selected_entries}
        selected_cases: list[dict[str, Any]] = []
        for rank, entry in enumerate(ranked, start=1):
            _, _, _, case_id, case, numeric = entry
            decision = "candidate" if case_id in selected_ids else "not_selected"
            trace_entry = {
                "case_id": case_id,
                "decision": decision,
                "hard_filter_reasons": [],
                "rank_numeric": rank,
                **numeric,
            }
            trace.append(trace_entry)
            if decision == "candidate":
                selected_cases.append(_prompt_safe_case(case, numeric_match=trace_entry))

        selected_cases.sort(
            key=lambda case: next(
                item.get("rank_numeric", math.inf)
                for item in trace
                if item.get("case_id") == case.get("case_id")
            )
        )
        evidence_available = bool(selected_cases)
        weak_selected = bool(selected_cases) and all(
            case["numeric_match"].get("weak_evidence") for case in selected_cases
        )
        warning = None
        if not evidence_available:
            warning = (
                "No compatible approved hifiasm V2 evidence cases; use readonly L1 facts, "
                "SKILL guidance, retry context, and catalog defaults conservatively."
            )
        elif weak_selected:
            warning = (
                "Compatible hifiasm cases have weak evidence coverage; treat cases as optional support "
                "and prefer conservative L2 changes."
            )

        metadata = {
            "retrieval_backend": "hifiasm_numeric_v2",
            "selected_case_ids": [case["case_id"] for case in selected_cases],
            "candidate_case_ids": [case["case_id"] for case in selected_cases],
            "selected_count": len(selected_cases),
            "eligible_case_count": len(ranked),
            "total_v2_cases": len(cases),
            "evidence_available": evidence_available,
            "case_evidence_required": False,
            "warning": warning,
            "fallback_guidance": [
                "Use readonly L1 profile facts as recommendation context.",
                "Use parameter-recommender SKILL heuristics for bounded L2 choices.",
                "Preserve catalog defaults when the combined evidence is weak or conflicting.",
            ],
            "trace": trace,
        }
        return SelectionResult(selected_cases=selected_cases, metadata=metadata, trace=trace)

    def _numeric_match(self, case: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
        query_features = query.get("numeric_features") or {}
        observed_facts = case.get("observed_facts") or {}
        feature_distances: dict[str, dict[str, Any]] = {}
        weighted_distance = 0.0
        present_weight = 0.0
        total_weight = float(len(_FEATURES))

        for feature in _FEATURES:
            query_value = _fact_value(query_features.get(feature))
            case_fact = observed_facts.get(feature)
            case_value = _fact_value(case_fact)
            if query_value is None:
                feature_distances[feature] = {
                    "query": None,
                    "case": case_value,
                    "distance": None,
                    "status": "missing_query_value",
                }
                continue
            if case_value is None and not _has_interval(case_fact):
                feature_distances[feature] = {
                    "query": query_value,
                    "case": None,
                    "distance": None,
                    "status": "missing_case_value",
                }
                continue

            distance, comparable_case = self._feature_distance(feature, query_value, case_fact)
            robust_distance = min(distance, self.distance_clip)
            weighted_distance += robust_distance
            present_weight += 1.0
            feature_distances[feature] = {
                "query": query_value,
                "case": comparable_case,
                "distance": robust_distance,
                "status": "compared",
            }

        numeric_distance = weighted_distance / present_weight if present_weight else None
        coverage = present_weight / total_weight
        return {
            "numeric_distance": numeric_distance,
            "numeric_similarity_display": math.exp(-numeric_distance) if numeric_distance is not None else 0.0,
            "evidence_coverage": coverage,
            "weak_evidence": coverage < self.weak_coverage_threshold,
            "feature_distances": feature_distances,
        }

    def _feature_distance(self, feature: str, query_value: float, case_fact: Any) -> tuple[float, Any]:
        if feature == "ploidy":
            case_value = float(_fact_value(case_fact))
            return (0.0 if query_value == case_value else 1.0), case_value

        comparison_value: float
        interval = _interval(case_fact)
        if interval is not None:
            low, high = interval
            if low <= query_value <= high:
                return 0.0, {"min": low, "max": high}
            comparison_value = low if query_value < low else high
        else:
            comparison_value = float(_fact_value(case_fact))

        scale = float(self.scales[feature])
        if feature == "heterozygosity":
            distance = abs(
                math.log((query_value + self.epsilon) / (comparison_value + self.epsilon))
            ) / scale
        else:
            if query_value <= 0 or comparison_value <= 0:
                distance = self.distance_clip
            else:
                distance = abs(math.log(query_value / comparison_value)) / scale
        return distance, comparison_value


def _hard_filter_reasons(case: dict[str, Any], query: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    curation = case.get("curation") or {}
    applicability = case.get("applicability") or {}
    intervention = case.get("intervention") or {}
    expected_case_type = "initial" if query.get("task") == "recommend_initial" else "retry"
    if curation.get("status") != "approved":
        reasons.append("curation_not_approved")
    if case.get("case_type") != expected_case_type:
        reasons.append("case_type_mismatch")
    if applicability.get("read_type") != "hifi" or query.get("read_type") not in {None, "hifi", "pacbio_hifi"}:
        reasons.append("read_type_mismatch")
    if _short_method_name(applicability.get("source_tool") or intervention.get("tool")) != "hifiasm":
        reasons.append("source_tool_mismatch")
    targets = applicability.get("target_methods") or []
    if not isinstance(targets, list) or not any(
        _short_method_name(target) == _short_method_name(query.get("method_id")) for target in targets
    ):
        reasons.append("target_method_mismatch")
    query_goal = query.get("assembly_goal")
    case_goal = applicability.get("assembly_goal")
    if query_goal and case_goal and query_goal != case_goal:
        reasons.append("assembly_goal_mismatch")
    return reasons


def _prompt_safe_case(case: dict[str, Any], *, numeric_match: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case.get("case_id"),
        "case_type": case.get("case_type"),
        "source": case.get("source") or {},
        "applicability": case.get("applicability") or {},
        "numeric_match": {
            key: value
            for key, value in numeric_match.items()
            if key not in {"case_id", "decision", "hard_filter_reasons", "rank_numeric"}
        },
        "observed_facts": case.get("observed_facts") or {},
        "intervention": case.get("intervention") or {},
        "rationale_claims": case.get("rationale_claims") or [],
        "outcomes": case.get("outcomes") or {},
        "source_excerpts": case.get("source_excerpts") or [],
    }


def _fact_value(fact: Any) -> float | None:
    value = fact.get("value") if isinstance(fact, dict) else fact
    return float(value) if _is_number(value) else None


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


def _clade_tie_break(case: dict[str, Any], query: dict[str, Any]) -> int:
    query_clade = (query.get("categorical_context") or {}).get("clade")
    case_clade = _fact_raw_value((case.get("observed_facts") or {}).get("clade"))
    return 0 if query_clade and case_clade == query_clade else 1


def _fact_raw_value(fact: Any) -> Any:
    return fact.get("value") if isinstance(fact, dict) else fact


def _short_method_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.rsplit(".", 1)[-1]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
