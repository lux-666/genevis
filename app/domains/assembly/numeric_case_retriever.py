"""Continuous, missing-aware retrieval for approved hifiasm EvidenceCases."""

from __future__ import annotations

import json
import math
import weakref
from copy import deepcopy
from typing import Any

from app.domains.assembly.evidence_case import (
    _project_hifiasm_ranked_prompt_case,
    _rank_hifiasm_prompt_case,
    project_hifiasm_retrieval_case,
)
from app.domains.assembly.selection import parameter_case_read_type
from app.knowledge.evidence_cases import EvidenceCaseValidationError
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
_CATEGORICAL_MATCH_WEIGHTS = {"method": 1.0, "read_type": 1.0}
_CONFIG_KEYS = {
    "weak_coverage_threshold",
    "distance_clip",
    "epsilon",
    "scales",
    "weights",
}
_RANKED_SELECTION_FIELD = "_hifiasm_ranked_selection_attestation"
_ATTESTED_AUDIT_FIELDS = (
    "retrieval_backend",
    "candidate_case_ids",
    "selected_case_ids",
    "selected_count",
    "eligible_case_count",
    "total_cases",
    "evidence_available",
    "case_evidence_required",
    "warning",
    "query",
    "trace",
)


def _build_ranked_selection_boundary():
    """Bind Task 3's ordered shortlist outside caller-writable metadata."""

    registry: dict[int, tuple[weakref.ReferenceType[Any], str]] = {}

    class RankedSelection(dict[str, Any]):
        __slots__ = ("__weakref__",)

        def __new__(cls, *args: Any, **kwargs: Any) -> "RankedSelection":
            raise TypeError("ranked selections can only be created by hifiasm numeric retrieval")

        def __copy__(self) -> "RankedSelection":
            return _copy_registered(self)

        def __deepcopy__(self, memo: dict[int, Any]) -> "RankedSelection":
            return _copy_registered(self, memo=memo)

    def _semantic_proof(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def _registered_proof(value: dict[str, Any]) -> str | None:
        entry = registry.get(id(value))
        if entry is None or entry[0]() is not value:
            return None
        return entry[1]

    def _register(value: RankedSelection, proof: str) -> RankedSelection:
        value_id = id(value)

        def _discard(reference: weakref.ReferenceType[Any]) -> None:
            entry = registry.get(value_id)
            if entry is not None and entry[0] is reference:
                registry.pop(value_id, None)

        registry[value_id] = (weakref.ref(value, _discard), proof)
        return value

    def _new_registered(values: dict[str, Any]) -> RankedSelection:
        result = dict.__new__(RankedSelection)
        dict.__init__(result, values)
        return _register(result, _semantic_proof(result))

    def _copy_registered(
        value: RankedSelection,
        *,
        memo: dict[int, Any] | None = None,
    ) -> RankedSelection:
        proof = _require_registered(value)
        copied = dict.__new__(RankedSelection)
        if memo is None:
            values = dict(value)
        else:
            memo[id(value)] = copied
            values = deepcopy(dict(value), memo)
        dict.__init__(copied, values)
        return _register(copied, proof)

    def _require_registered(value: dict[str, Any]) -> str:
        proof = _registered_proof(value)
        if proof is None:
            raise EvidenceCaseValidationError(
                "hifiasm numeric shortlist requires Task 3 selection attestation"
            )
        if _semantic_proof(value) != proof:
            raise EvidenceCaseValidationError(
                "hifiasm numeric shortlist differs from its Task 3 selection attestation"
            )
        return proof

    def _attest(
        *,
        selected_cases: list[dict[str, Any]],
        metadata: dict[str, Any],
        trace: list[dict[str, Any]],
    ) -> RankedSelection:
        ranked_cases = [_project_hifiasm_ranked_prompt_case(case) for case in selected_cases]
        candidate_ids = [case["case_id"] for case in ranked_cases]
        if metadata.get("candidate_case_ids") != candidate_ids:
            raise EvidenceCaseValidationError("Task 3 candidate order is internally inconsistent")
        if metadata.get("selected_case_ids") != candidate_ids:
            raise EvidenceCaseValidationError("Task 3 selected IDs are internally inconsistent")
        candidate_trace = sorted(
            (item for item in trace if item.get("decision") == "candidate"),
            key=lambda item: item.get("rank_numeric", math.inf),
        )
        if [item.get("case_id") for item in candidate_trace] != candidate_ids:
            raise EvidenceCaseValidationError("Task 3 trace order is internally inconsistent")
        candidate_ranks = [item.get("rank_numeric") for item in candidate_trace]
        if any(rank is not None for rank in candidate_ranks):
            if any(type(rank) is not int for rank in candidate_ranks) or candidate_ranks != list(
                range(1, len(candidate_trace) + 1)
            ):
                raise EvidenceCaseValidationError(
                    "Task 3 candidate ranks are internally inconsistent"
                )
        for case, trace_entry in zip(ranked_cases, candidate_trace):
            expected_numeric = {
                key: value
                for key, value in trace_entry.items()
                if key not in {"case_id", "decision", "hard_filter_reasons"}
            }
            if case.get("numeric_match") != expected_numeric:
                raise EvidenceCaseValidationError(
                    f"{case['case_id']}: ranked numeric metadata differs from Task 3 trace"
                )
        backend = metadata.get("retrieval_backend", "hifiasm_numeric")
        if backend != "hifiasm_numeric":
            raise EvidenceCaseValidationError("unsupported hifiasm ranked retrieval backend")
        return _new_registered(
            {
                "retrieval_backend": backend,
                "candidate_case_ids": candidate_ids,
                "selected_case_ids": list(candidate_ids),
                "evidence_case_ids": list(candidate_ids),
                "selected_count": len(candidate_ids),
                "eligible_case_count": metadata.get("eligible_case_count", 0),
                "total_cases": metadata.get("total_cases", 0),
                "evidence_available": bool(candidate_ids),
                "case_evidence_required": metadata.get("case_evidence_required", False),
                "warning": metadata.get("warning"),
                "query": deepcopy(metadata.get("query") or {}),
                "selected_cases": ranked_cases,
                "trace": deepcopy(trace),
            }
        )

    def _project_attested_audit_view(
        selection: dict[str, Any],
    ) -> dict[str, Any]:
        """Return audit fields only after matching Task 3's registered envelope."""

        envelope = selection.get(_RANKED_SELECTION_FIELD)
        if not isinstance(envelope, dict):
            raise EvidenceCaseValidationError(
                "hifiasm numeric shortlist requires Task 3 selection attestation"
            )
        _require_registered(envelope)
        for field in _ATTESTED_AUDIT_FIELDS:
            if selection.get(field) != envelope.get(field):
                raise EvidenceCaseValidationError(
                    f"{field} differs from Task 3 selection attestation"
                )
        return {
            field: deepcopy(envelope.get(field))
            for field in _ATTESTED_AUDIT_FIELDS
        }

    def _validate(
        *,
        evidence_cases: list[dict[str, Any]],
        case_retrieval: dict[str, Any],
        case_limit: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        envelope = case_retrieval.get(_RANKED_SELECTION_FIELD)
        if not isinstance(envelope, dict):
            raise EvidenceCaseValidationError(
                "hifiasm numeric shortlist requires Task 3 selection attestation"
            )
        _require_registered(envelope)

        candidate_ids = list(envelope["candidate_case_ids"])
        if case_retrieval.get("retrieval_backend") != envelope["retrieval_backend"]:
            raise EvidenceCaseValidationError("retrieval_backend differs from Task 3 attestation")
        if case_retrieval.get("candidate_case_ids") != candidate_ids:
            raise EvidenceCaseValidationError("candidate_case_ids differ from Task 3 attestation")
        if case_retrieval.get("selected_case_ids") != envelope["selected_case_ids"]:
            raise EvidenceCaseValidationError("selected_case_ids differ from Task 3 attestation")
        if case_retrieval.get("selected_count") != envelope["selected_count"]:
            raise EvidenceCaseValidationError("selected_count differs from Task 3 attestation")
        if case_retrieval.get("evidence_available") != envelope["evidence_available"]:
            raise EvidenceCaseValidationError("evidence_available differs from Task 3 attestation")
        for field in (
            "eligible_case_count",
            "total_cases",
            "case_evidence_required",
            "warning",
        ):
            if case_retrieval.get(field) != envelope[field]:
                raise EvidenceCaseValidationError(
                    f"{field} differs from Task 3 attestation"
                )

        ranked_cases = [_project_hifiasm_ranked_prompt_case(case) for case in evidence_cases]
        evidence_ids = [case["case_id"] for case in ranked_cases]
        expected_prefix_count = min(case_limit, len(candidate_ids))
        if len(ranked_cases) != expected_prefix_count:
            raise EvidenceCaseValidationError(
                "Task 4 requires the complete attested shortlist prefix before budgeting"
            )
        declared_evidence_ids = case_retrieval.get("evidence_case_ids")
        if declared_evidence_ids != evidence_ids:
            raise EvidenceCaseValidationError(
                "evidence_case_ids differ from the supplied Task 3 ranked cases"
            )
        if evidence_ids != candidate_ids[: len(evidence_ids)]:
            raise EvidenceCaseValidationError(
                "evidence cases do not preserve the Task 3 ranked shortlist order"
            )
        if ranked_cases != envelope["selected_cases"][: len(ranked_cases)]:
            raise EvidenceCaseValidationError(
                "evidence cases differ from the Task 3 ranked selection attestation"
            )
        return ranked_cases, candidate_ids

    return _attest, _validate, _project_attested_audit_view


(
    _attest_hifiasm_ranked_selection,
    _validate_hifiasm_ranked_selection,
    _project_hifiasm_attested_selection_audit,
) = _build_ranked_selection_boundary()
del _build_ranked_selection_boundary


class HifiasmNumericCaseRetriever:
    """Apply hard compatibility filters before continuous numeric ranking."""

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
            raise ValueError(f"hifiasm_numeric strategy contains unknown keys: {sorted(unknown)}")
        return cls(
            max_cases=12 if max_cases is None else max_cases,
            weak_coverage_threshold=raw.get("weak_coverage_threshold", 0.6),
            distance_clip=raw.get("distance_clip", 4.0),
            epsilon=raw.get("epsilon", 1e-6),
            scales=raw.get("scales"),
            weights=raw.get("weights"),
        )

    def _validate_configuration(self) -> None:
        if not isinstance(self.max_cases, int) or isinstance(self.max_cases, bool) or self.max_cases < 1:
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

    def select(self, cases: list[dict[str, Any]], *, query: dict[str, Any]) -> SelectionResult:
        if query.get("task") != "recommend_initial":
            raise ValueError("hifiasm numeric retrieval only supports task='recommend_initial'")

        trace: list[dict[str, Any]] = []
        ranked: list[
            tuple[float, float, float, int, str, dict[str, Any], dict[str, Any]]
        ] = []
        for raw_case in cases:
            case = project_hifiasm_retrieval_case(raw_case)
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
                        "feature_distances": _not_compared_features(case, query, weights=self.weights),
                    }
                )
                continue

            numeric = self._numeric_match(case, query)
            categorical = _categorical_match(case, query)
            numeric.update(categorical)
            numeric["ranking_score"] = (
                float(categorical["categorical_match_score"])
                + float(numeric["evidence_coverage"])
                * float(numeric["numeric_similarity_display"])
            )
            distance_key = numeric["numeric_distance"] if numeric["numeric_distance"] is not None else math.inf
            ranked.append(
                (
                    -float(numeric["ranking_score"]),
                    -float(numeric["evidence_coverage"]),
                    float(distance_key),
                    _clade_tie_break(case, query),
                    case_id,
                    case,
                    numeric,
                )
            )

        ranked.sort(key=lambda item: item[:5])
        selected_entries = ranked[: self.max_cases]
        selected_ids = {entry[4] for entry in selected_entries}
        selected_cases: list[dict[str, Any]] = []
        for rank, entry in enumerate(ranked, start=1):
            _, _, _, _, case_id, case, numeric = entry
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
        weak_selected = bool(selected_cases) and any(
            case["numeric_match"].get("weak_evidence") for case in selected_cases
        )
        warnings: list[str] = []
        if not evidence_available:
            warnings.append(
                "No eligible approved hifiasm evidence cases; use readonly L1 facts, "
                "SKILL guidance, retry context, and catalog defaults conservatively."
            )
        else:
            if len(selected_cases) < self.max_cases:
                warnings.append(
                    f"Only {len(selected_cases)} of {self.max_cases} requested hifiasm evidence "
                    "candidates were available; keep the recommendation conservative."
                )
            if weak_selected:
                warnings.append(
                    "Some selected hifiasm cases have weak evidence coverage; "
                    "treat cases as optional support and prefer conservative L2 changes."
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
            "case_evidence_required": False,
            "warning": warning,
            "query": deepcopy(query),
            "fallback_guidance": [
                "Use readonly L1 profile facts as recommendation context.",
                "Use parameter-recommender SKILL heuristics for bounded L2 choices.",
                "Preserve catalog defaults when the combined evidence is weak or conflicting.",
            ],
            "trace": trace,
        }
        metadata[_RANKED_SELECTION_FIELD] = _attest_hifiasm_ranked_selection(
            selected_cases=selected_cases,
            metadata=metadata,
            trace=trace,
        )
        return SelectionResult(selected_cases=selected_cases, metadata=metadata, trace=trace)

    def _numeric_match(self, case: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
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

            distance, comparable_case = self._feature_distance(feature, query_value, case_fact)
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
            if query_value < 0 or comparison_value < 0:
                distance = self.distance_clip
            else:
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
    expected_case_type = "initial"
    if curation.get("status") != "approved":
        reasons.append("curation_not_approved")
    elif case.get("retrieval_eligible") is not True:
        reasons.append("source_not_validated")
    if case.get("case_type") != expected_case_type:
        reasons.append("case_type_mismatch")
    return reasons


def _categorical_match(case: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    applicability = case.get("applicability") or {}
    targets = applicability.get("target_methods") or []
    query_method = _short_method_name(query.get("method_id"))
    method_match = bool(
        query_method
        and isinstance(targets, list)
        and any(_short_method_name(target) == query_method for target in targets)
    )
    query_read_type = parameter_case_read_type(query.get("read_type"))
    case_read_type = parameter_case_read_type(applicability.get("read_type"))
    read_type_match = bool(
        query_read_type and case_read_type and query_read_type == case_read_type
    )
    score = (
        _CATEGORICAL_MATCH_WEIGHTS["method"] * int(method_match)
        + _CATEGORICAL_MATCH_WEIGHTS["read_type"] * int(read_type_match)
    )
    return {
        "categorical_match_score": score,
        "method_match": method_match,
        "read_type_match": read_type_match,
    }


def _prompt_safe_case(case: dict[str, Any], *, numeric_match: dict[str, Any]) -> dict[str, Any]:
    return _rank_hifiasm_prompt_case(
        case,
        numeric_match={
            key: value
            for key, value in numeric_match.items()
            if key not in {"case_id", "decision", "hard_filter_reasons"}
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
            "status": "not_compared_hard_filter",
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


def _clade_tie_break(case: dict[str, Any], query: dict[str, Any]) -> int:
    query_clade = (query.get("categorical_context") or {}).get("clade")
    case_clade = _fact_raw_value((case.get("observed_facts") or {}).get("clade"))
    return 0 if query_clade and case_clade == query_clade else 1


def _fact_raw_value(fact: Any) -> Any:
    return fact.get("value") if isinstance(fact, dict) else fact


def _short_method_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.rsplit(".", 1)[-1].lower().replace("-", "_")


def _is_number(value: Any) -> bool:
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
