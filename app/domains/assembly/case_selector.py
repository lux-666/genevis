"""Domain-aware evidence-case selection for assembly parameter prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domains.assembly.evidence_case import validate_hifiasm_evidence_cases
from app.domains.assembly.numeric_case_retriever import HifiasmNumericCaseRetriever
from app.domains.assembly.retrieval_query import build_hifiasm_retrieval_query
from app.domains.assembly.selection import parameter_case_read_type
from app.knowledge.selectors import SelectionResult

DEFAULT_STRATEGY = {
    "hard_filters": {
        "read_type_mismatch": True,
        "exclude_failure_cases_for_initial": True,
    },
    "scoring_weights": {
        "method_exact_match": 8,
        "method_family_match": 3,
        "read_type_match": 8,
        "failure_mode_match": 5,
        "clade_match": 2,
        "coverage_bucket_match": 2,
        "genome_size_bucket_match": 2,
        "heterozygosity_bucket_match": 2,
        "read_n50_bucket_match": 1,
    },
    "buckets": {
        "coverage": [30, 60],
        "genome_size_mb": [500, 3000, 10000],
        "read_n50_kb": [15, 30, 50],
    },
}


class EvidenceBasedCaseSelector:
    """Select assembly evidence cases with explicit, auditable scoring."""

    def __init__(
        self,
        *,
        min_cases: int = 3,
        max_cases: int = 8,
        strategy: dict[str, Any] | None = None,
    ) -> None:
        self.min_cases = min_cases
        self.max_cases = max_cases
        self.strategy = _normalize_strategy(strategy)

    def select(
        self,
        cases: list[dict[str, Any]],
        *,
        method_id: str,
        task: str,
        prompt_profile: dict[str, Any],
    ) -> SelectionResult:
        profile_read_type = _lookup(prompt_profile, "assets.read_type")
        hard_filters = self.strategy["hard_filters"]

        scored: list[tuple[int, str, dict[str, Any]]] = []
        trace: list[dict[str, Any]] = []
        for case in cases:
            case_id = str(case.get("case_id") or "")
            features = case.get("features") or {}
            excluded_reasons: list[str] = []
            if hard_filters.get("read_type_mismatch", True) and not evidence_read_type_matches(
                profile_read_type,
                features.get("read_type"),
            ):
                excluded_reasons.append("read_type_mismatch")
            if (
                hard_filters.get("exclude_failure_cases_for_initial", True)
                and task == "recommend_initial"
                and features.get("failure_mode")
            ):
                excluded_reasons.append("failure_case_excluded_for_initial")

            if excluded_reasons:
                trace.append(
                    {
                        "case_id": case_id,
                        "decision": "excluded",
                        "score": 0,
                        "matched_features": [],
                        "excluded_reasons": excluded_reasons,
                    }
                )
                continue

            score, matched_features = _case_score(
                case,
                method_id=method_id,
                task=task,
                prompt_profile=prompt_profile,
                strategy=self.strategy,
            )
            decision = "selected_candidate" if score > 0 else "not_selected"
            trace.append(
                {
                    "case_id": case_id,
                    "decision": decision,
                    "score": score,
                    "matched_features": matched_features,
                    "excluded_reasons": [],
                }
            )
            if score > 0:
                scored.append((score, case_id, case))

        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = [case for _, _, case in scored[: self.max_cases]]
        selected_ids = {str(case.get("case_id") or "") for case in selected}
        for item in trace:
            if item["case_id"] in selected_ids and item["decision"] == "selected_candidate":
                item["decision"] = "selected"

        warning = None
        if len(selected) < self.min_cases:
            warning = f"Only {len(selected)} compatible evidence cases selected; recommendations should stay conservative."

        metadata = {
            "selected_case_ids": [case.get("case_id") for case in selected],
            "selected_count": len(selected),
            "total_cases": len(cases),
            "warning": warning,
            "trace": trace,
        }
        return SelectionResult(selected_cases=selected, metadata=metadata, trace=trace)


def select_evidence_cases(
    cases: list[dict[str, Any]],
    *,
    method_id: str,
    task: str,
    prompt_profile: dict[str, Any],
    min_cases: int = 3,
    max_cases: int | None = None,
    strategy: dict[str, Any] | None = None,
    source_root: Path | str | None = None,
    query_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Dispatch hifiasm to numeric evidence retrieval and preserve other methods."""

    if _tool_name(method_id) == "hifiasm":
        pack_root = Path(source_root) if source_root is not None else None
        validated_cases = validate_hifiasm_evidence_cases(cases, pack_root=pack_root)
        query = build_hifiasm_retrieval_query(
            prompt_profile,
            method_id=method_id,
            task=task,
            query_id=query_id,
        )
        if strategy is not None and not isinstance(strategy, dict):
            raise ValueError("strategy must be a mapping")
        numeric_config = (strategy or {}).get("hifiasm_numeric")
        result = HifiasmNumericCaseRetriever.from_config(
            numeric_config,
            max_cases=max_cases,
        ).select(validated_cases, query=query)
        return result.selected_cases, {**result.metadata, "query": query}

    selector = EvidenceBasedCaseSelector(
        min_cases=min_cases,
        max_cases=8 if max_cases is None else max_cases,
        strategy=strategy,
    )
    result = selector.select(cases, method_id=method_id, task=task, prompt_profile=prompt_profile)
    return result.selected_cases, result.metadata


def _normalize_strategy(strategy: dict[str, Any] | None) -> dict[str, Any]:
    source = strategy or {}
    strict_strategy = False
    if "strategies" in source:
        strategy_name = source.get("default_strategy")
        strategies = source.get("strategies") or {}
        if not isinstance(strategies, dict):
            raise ValueError("strategies must be a mapping")
        if not isinstance(strategy_name, str) or not strategy_name:
            raise ValueError("default_strategy must select a named strategy")
        if strategy_name and strategy_name not in strategies:
            raise ValueError(
                f"default_strategy '{strategy_name}' not found in strategies. "
                f"Available strategies: {list(strategies.keys())}"
            )
        source = strategies.get(strategy_name) or {}
        strict_strategy = True
    if not isinstance(source, dict):
        raise ValueError("strategy must be a mapping")
    if strict_strategy:
        _validate_strategy(source)
    elif strategy is not None:
        _validate_strategy_fragment(source)
    return {
        "hard_filters": {**DEFAULT_STRATEGY["hard_filters"], **dict(source.get("hard_filters") or {})},
        "scoring_weights": {**DEFAULT_STRATEGY["scoring_weights"], **dict(source.get("scoring_weights") or {})},
        "buckets": {**DEFAULT_STRATEGY["buckets"], **dict(source.get("buckets") or {})},
    }


def _validate_strategy_fragment(strategy: dict[str, Any]) -> None:
    unknown_keys = set(strategy) - {"hard_filters", "scoring_weights", "buckets"}
    if unknown_keys:
        raise ValueError(f"strategy contains unknown keys: {sorted(unknown_keys)}")

    hard_filters = strategy.get("hard_filters")
    if hard_filters is not None:
        if not isinstance(hard_filters, dict):
            raise ValueError("strategy.hard_filters must be a mapping")
        _validate_section_keys("hard_filters", hard_filters)
        for key, value in hard_filters.items():
            if not isinstance(value, bool):
                raise ValueError(f"strategy.hard_filters.{key} must be a bool")

    scoring_weights = strategy.get("scoring_weights")
    if scoring_weights is not None:
        if not isinstance(scoring_weights, dict):
            raise ValueError("strategy.scoring_weights must be a mapping")
        _validate_section_keys("scoring_weights", scoring_weights)
        for key, value in scoring_weights.items():
            if not _is_number(value):
                raise ValueError(f"strategy.scoring_weights.{key} must be a number")

    buckets = strategy.get("buckets")
    if buckets is not None:
        if not isinstance(buckets, dict):
            raise ValueError("strategy.buckets must be a mapping")
        _validate_section_keys("buckets", buckets)
        for key, value in buckets.items():
            if not isinstance(value, list) or not all(_is_number(item) for item in value):
                raise ValueError(f"strategy.buckets.{key} must be a list of numbers")


def _validate_strategy(strategy: dict[str, Any]) -> None:
    for required_key in ("hard_filters", "scoring_weights"):
        if required_key not in strategy:
            raise ValueError(f"strategy missing required field '{required_key}'")
        if not isinstance(strategy[required_key], dict):
            raise ValueError(f"strategy.{required_key} must be a mapping")

    buckets = strategy.get("buckets")
    if buckets is not None and not isinstance(buckets, dict):
        raise ValueError("strategy.buckets must be a mapping")

    _validate_strategy_fragment(strategy)


def _validate_section_keys(section: str, values: dict[str, Any]) -> None:
    unknown_keys = set(values) - set(DEFAULT_STRATEGY[section])
    if unknown_keys:
        raise ValueError(f"strategy.{section} contains unknown keys: {sorted(unknown_keys)}")


def _case_score(
    case: dict[str, Any],
    *,
    method_id: str,
    task: str,
    prompt_profile: dict[str, Any],
    strategy: dict[str, Any],
) -> tuple[int, list[str]]:
    features = case.get("features") or {}
    params = case.get("params") or {}
    case_method = params.get("tool")
    read_type = _lookup(prompt_profile, "assets.read_type")
    case_read_type = features.get("read_type")
    failure_mode = _lookup(prompt_profile, "previous_attempt.failure_class") or _lookup(
        prompt_profile, "assembly_review.failure_mode"
    )
    weights = strategy["scoring_weights"]
    buckets = strategy["buckets"]

    score = 0
    matched: list[str] = []
    if _tool_name(case_method) == _tool_name(method_id):
        score += weights["method_exact_match"]
        matched.append("method_exact_match")
    elif _tool_family(case_method) == _tool_family(method_id):
        score += weights["method_family_match"]
        matched.append("method_family_match")
    if read_type and evidence_read_type_matches(read_type, case_read_type):
        score += weights["read_type_match"]
        matched.append("read_type_match")
    if failure_mode and features.get("failure_mode") == failure_mode:
        score += weights["failure_mode_match"]
        matched.append("failure_mode_match")

    sample_clade = _lookup(prompt_profile, "sample.clade")
    if sample_clade and features.get("clade") == sample_clade:
        score += weights["clade_match"]
        matched.append("clade_match")

    coverage = _lookup(prompt_profile, "read_qc.effective_coverage_1kb") or _lookup(
        prompt_profile, "read_qc.effective_coverage_5kb"
    )
    if _same_bucket(coverage, features.get("coverage"), tuple(buckets["coverage"])):
        score += weights["coverage_bucket_match"]
        matched.append("coverage_bucket_match")

    genome_size = _lookup(prompt_profile, "genome_survey.estimated_genome_size")
    if _same_bucket(_bp_to_mb(genome_size), features.get("genome_size_mb"), tuple(buckets["genome_size_mb"])):
        score += weights["genome_size_bucket_match"]
        matched.append("genome_size_bucket_match")

    heterozygosity = _heterozygosity_bucket(_lookup(prompt_profile, "genome_survey.heterozygosity"))
    if heterozygosity is not None and heterozygosity == features.get("heterozygosity"):
        score += weights["heterozygosity_bucket_match"]
        matched.append("heterozygosity_bucket_match")

    read_n50_kb = _bp_to_kb(_lookup(prompt_profile, "read_qc.read_n50"))
    if _same_bucket(read_n50_kb, features.get("read_n50_kb"), tuple(buckets["read_n50_kb"])):
        score += weights["read_n50_bucket_match"]
        matched.append("read_n50_bucket_match")

    return score, matched


def _lookup(values: Any, path: str) -> Any:
    current = values
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def evidence_read_type_matches(left: Any, right: Any) -> bool:
    left_key = _evidence_read_type_key(left)
    right_key = _evidence_read_type_key(right)
    return bool(left_key and right_key and left_key == right_key)


def _evidence_read_type_key(read_type: Any) -> str | None:
    return parameter_case_read_type(read_type)


def _tool_family(method_id: Any) -> str | None:
    method_name = _tool_name(method_id)
    if method_name is None:
        return None
    return method_name.split("_", 1)[0]


def _tool_name(method_id: Any) -> str | None:
    if not isinstance(method_id, str):
        return None
    return method_id.rsplit(".", 1)[-1]


def _same_bucket(left: Any, right: Any, thresholds: tuple[int, ...]) -> bool:
    if not _is_number(left) or not _is_number(right):
        return False
    return _bucket(float(left), thresholds) == _bucket(float(right), thresholds)


def _bucket(value: float, thresholds: tuple[int, ...]) -> int:
    for index, threshold in enumerate(thresholds):
        if value <= threshold:
            return index
    return len(thresholds)


def _heterozygosity_bucket(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.lower()
        return normalized if normalized in {"low", "medium", "high", "unknown"} else None
    if not _is_number(value):
        return None
    numeric = float(value)
    if numeric < 0.003:
        return "low"
    if numeric < 0.01:
        return "medium"
    return "high"


def _bp_to_mb(value: Any) -> float | None:
    return float(value) / 1_000_000 if _is_number(value) else None


def _bp_to_kb(value: Any) -> float | None:
    return float(value) / 1_000 if _is_number(value) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
