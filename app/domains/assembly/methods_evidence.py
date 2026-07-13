"""Hifiasm Methods-aware evidence and recommendation contracts.

This module deliberately contains only deterministic, prompt-visible logic. Numeric
ranking remains owned by ``numeric_case_retriever`` and EvidenceCase schema projection
remains owned by ``evidence_case``.
"""

from __future__ import annotations

import hashlib
import json
import weakref
from copy import deepcopy
from typing import Any

from app.domains.assembly.evidence_case import _project_hifiasm_ranked_prompt_case
from app.domains.assembly.numeric_case_retriever import _validate_hifiasm_ranked_selection
from app.domains.assembly.profile_paths import lookup_mapping_path as _lookup_path
from app.knowledge.evidence_cases import MAX_SOURCE_EXCERPTS_PER_CASE

HIFIASM_EFFECT_AREAS = (
    "kmer_and_seed_selection",
    "overlap_filtering",
    "error_correction",
    "repeat_handling",
    "graph_cleaning",
    "haplotype_preservation",
    "purging",
    "performance_and_memory",
)
HIFIASM_METHODS_EVIDENCE_CASE_LIMIT = 8
HIFIASM_METHODS_EVIDENCE_SPAN_LIMIT_PER_CASE = 12
HIFIASM_METHODS_EVIDENCE_EXCERPT_CHARS_PER_CASE = 6_000
HIFIASM_METHODS_EVIDENCE_TOTAL_EXCERPT_CHARS = 24_000
HIFIASM_METHODS_EVIDENCE_SERIALIZED_BYTES = 65_536
HIFIASM_METHODS_PROMPT_SCHEMA_VERSION = 1

_PROMPT_SCHEMA_VERSION_FIELD = "hifiasm_methods_prompt_schema_version"

_ALLOWED_RELEVANCE = {"strong", "medium", "weak", "not_relevant"}
_ALLOWED_ACTIONS = {"set", "keep_previous", "keep_default", "omit"}
_ALLOWED_CONFIDENCE = {"strong", "medium", "weak"}
_ALLOWED_EVIDENCE_BASES = {
    "case",
    "profile_fact",
    "skill_heuristic",
    "retry_failure",
    "catalog_default",
}
_NUMERIC_MATCH_FIELDS = (
    "distance",
    "numeric_distance",
    "numeric_similarity_display",
    "evidence_coverage",
    "weak_evidence",
    "rank_numeric",
    "feature_distances",
)
_FEATURE_DISTANCE_FIELDS = (
    "query",
    "case",
    "distance",
    "raw_distance",
    "weight",
    "status",
)
_PROMPT_COPY_FIELDS = (
    "method_id",
    "task",
    "input_profile_ref",
    "input_profile_hash",
    "prompt_profile",
    "catalog_subset",
    "rules",
    "recommendation_policy",
    "skill_instructions",
    "readonly_params",
    "adjustable_params",
    "evidence_payload_ref",
    "evidence_payload_hash",
    "retry_evidence",
)
_PROMPT_SELF_FIELDS = ("prompt_payload_ref", "prompt_payload_hash")
_CASE_RETRIEVAL_SCALAR_FIELDS = (
    "retrieval_backend",
    "query_id",
    "selected_count",
    "eligible_case_count",
    "total_cases",
    "evidence_available",
    "case_evidence_required",
    "warning",
    "query_ref",
    "query_hash",
    "trace_ref",
    "trace_hash",
    "evidence_payload_ref",
    "evidence_payload_hash",
)
_CASE_RETRIEVAL_ID_FIELDS = (
    "selected_case_ids",
    "candidate_case_ids",
    "evidence_case_ids",
)
_GATE_FIELDS = (
    "initial_cutover_enabled",
    "decision",
    "reason",
)


def uses_hifiasm_methods_contract(prompt_payload: dict[str, Any]) -> bool:
    """Return whether the prompt uses the structured hifiasm Methods contract."""

    retrieval = prompt_payload.get("case_retrieval") or prompt_payload.get("case_selection") or {}
    return (
        prompt_payload.get("method_id") == "hifiasm"
        and retrieval.get("retrieval_backend") == "hifiasm_numeric"
    )


def build_hifiasm_evidence_payload(
    *,
    evidence_cases: list[dict[str, Any]],
    case_retrieval: dict[str, Any],
    allowed_parameter_ids: set[str],
) -> dict[str, Any]:
    """Build one bounded evidence payload from Task 2 prompt-safe projections."""

    ranked_cases, retrieval_candidate_ids = _validate_hifiasm_ranked_selection(
        evidence_cases=evidence_cases,
        case_retrieval=case_retrieval,
        case_limit=HIFIASM_METHODS_EVIDENCE_CASE_LIMIT,
    )
    requested_evidence_ids = [case["case_id"] for case in ranked_cases]
    evidence_case_ids = requested_evidence_ids[:HIFIASM_METHODS_EVIDENCE_CASE_LIMIT]
    retrieval_candidate_set = set(retrieval_candidate_ids)

    raw_cases_by_id: dict[str, dict[str, Any]] = {}
    for ranked_case in ranked_cases:
        case_id = ranked_case["case_id"]
        if case_id not in retrieval_candidate_set:
            raise ValueError(f"evidence case {case_id!r} is outside the numeric shortlist")
        if case_id in raw_cases_by_id:
            raise ValueError(f"duplicate evidence case {case_id!r}")
        raw_cases_by_id[case_id] = ranked_case

    missing = [case_id for case_id in evidence_case_ids if case_id not in raw_cases_by_id]
    if missing:
        raise ValueError(f"numeric shortlist cases missing from evidence payload: {missing}")

    omitted_cases = [
        {"case_id": case_id, "reason": "serialized_evidence_bytes"}
        for case_id in retrieval_candidate_ids[
            len(evidence_case_ids):HIFIASM_METHODS_EVIDENCE_CASE_LIMIT
        ]
    ] + [
        {"case_id": case_id, "reason": "case_limit"}
        for case_id in retrieval_candidate_ids[HIFIASM_METHODS_EVIDENCE_CASE_LIMIT:]
    ]
    cases: list[dict[str, Any]] = []
    omitted_spans: list[dict[str, str]] = []
    total_excerpt_chars = 0
    for case_id in evidence_case_ids:
        case, case_omitted_spans, case_excerpt_chars = _budget_evidence_case(
            raw_cases_by_id[case_id],
            total_excerpt_chars=total_excerpt_chars,
            allowed_parameter_ids=allowed_parameter_ids,
        )
        cases.append(case)
        omitted_spans.extend(case_omitted_spans)
        total_excerpt_chars += case_excerpt_chars

    payload = {
        "retrieval_backend": case_retrieval.get("retrieval_backend"),
        "query_id": _safe_optional_scalar(case_retrieval.get("query_id")),
        "candidate_case_ids": [case["case_id"] for case in cases],
        "retrieval_candidate_case_ids": retrieval_candidate_ids,
        "warning": _safe_optional_scalar(case_retrieval.get("warning")),
        "effect_areas": list(HIFIASM_EFFECT_AREAS),
        "cases": cases,
        "budget": {
            "limits": {
                "cases": HIFIASM_METHODS_EVIDENCE_CASE_LIMIT,
                "spans_per_case": HIFIASM_METHODS_EVIDENCE_SPAN_LIMIT_PER_CASE,
                "excerpt_chars_per_case": HIFIASM_METHODS_EVIDENCE_EXCERPT_CHARS_PER_CASE,
                "total_excerpt_chars": HIFIASM_METHODS_EVIDENCE_TOTAL_EXCERPT_CHARS,
                "serialized_bytes": HIFIASM_METHODS_EVIDENCE_SERIALIZED_BYTES,
            },
            "used": {},
            "case_usage": [],
            "omitted_cases": omitted_cases,
            "omitted_spans": omitted_spans,
        },
    }
    _refresh_budget_usage(payload)
    while payload["budget"]["used"]["serialized_bytes"] > HIFIASM_METHODS_EVIDENCE_SERIALIZED_BYTES:
        if not payload["cases"]:
            raise ValueError("hifiasm Methods evidence metadata exceeds the serialized byte budget")
        omitted_case = payload["cases"].pop()
        omitted_case_id = omitted_case["case_id"]
        payload["budget"]["omitted_cases"].append(
            {"case_id": omitted_case_id, "reason": "serialized_evidence_bytes"}
        )
        payload["budget"]["omitted_spans"] = [
            item
            for item in payload["budget"]["omitted_spans"]
            if item["case_id"] != omitted_case_id
        ]
        payload["candidate_case_ids"] = [case["case_id"] for case in payload["cases"]]
        _refresh_budget_usage(payload)
    candidate_order = {case_id: index for index, case_id in enumerate(retrieval_candidate_ids)}
    payload["budget"]["omitted_cases"].sort(
        key=lambda item: candidate_order.get(item["case_id"], len(candidate_order))
    )
    _refresh_budget_usage(payload)
    return payload


def _validate_canonical_hifiasm_methods_prompt_payload(
    prompt_payload: dict[str, Any],
) -> None:
    version = prompt_payload.get(_PROMPT_SCHEMA_VERSION_FIELD)
    if type(version) is not int or version != HIFIASM_METHODS_PROMPT_SCHEMA_VERSION:
        raise ValueError("unsupported canonical hifiasm Methods prompt schema version")

    required_fields = {
        _PROMPT_SCHEMA_VERSION_FIELD,
        "selected_cases",
        "case_selection",
        "case_retrieval",
        "evidence_cases",
        "methods_evidence",
    }
    allowed_fields = set(_PROMPT_COPY_FIELDS) | set(_PROMPT_SELF_FIELDS) | required_fields
    missing_fields = required_fields - set(prompt_payload)
    unexpected_fields = set(prompt_payload) - allowed_fields
    if missing_fields or unexpected_fields:
        raise ValueError(
            "canonical hifiasm Methods prompt has invalid top-level fields; "
            f"missing={sorted(missing_fields)}, unexpected={sorted(unexpected_fields)}"
        )
    if prompt_payload["selected_cases"] != []:
        raise ValueError("canonical hifiasm Methods prompt requires selected_cases=[]")

    prompt_ref = prompt_payload.get("prompt_payload_ref")
    if prompt_ref is not None and (not isinstance(prompt_ref, str) or not prompt_ref.strip()):
        raise ValueError("canonical prompt_payload_ref must be a non-empty string")
    prompt_hash = prompt_payload.get("prompt_payload_hash")
    if prompt_hash is not None:
        semantic_payload = {
            key: value
            for key, value in prompt_payload.items()
            if key not in _PROMPT_SELF_FIELDS
        }
        expected_hash = hashlib.sha256(
            json.dumps(semantic_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if prompt_hash != expected_hash:
            raise ValueError("canonical prompt_payload_hash does not match canonical content")

    case_retrieval = prompt_payload["case_retrieval"]
    if not isinstance(case_retrieval, dict):
        raise ValueError("canonical case_retrieval must be an object")
    evidence_case_ids = _string_list(
        case_retrieval.get("evidence_case_ids"),
        field="evidence_case_ids",
    )
    if case_retrieval != _project_case_retrieval(
        case_retrieval,
        evidence_case_ids=evidence_case_ids,
    ):
        raise ValueError("canonical case_retrieval contains non-projected fields")
    if prompt_payload["case_selection"] != case_retrieval:
        raise ValueError("canonical case_selection must equal case_retrieval")

    evidence_cases = prompt_payload["evidence_cases"]
    if not isinstance(evidence_cases, list):
        raise ValueError("canonical evidence_cases must be a list")
    visible_case_ids: list[str] = []
    for case in evidence_cases:
        if not isinstance(case, dict):
            raise ValueError("canonical evidence_cases entries must be objects")
        visible_case_ids.append(_nonempty_string(case.get("case_id"), field="case_id"))
    if len(visible_case_ids) != len(set(visible_case_ids)):
        raise ValueError("canonical evidence_cases must not contain duplicate case IDs")
    if visible_case_ids != evidence_case_ids:
        raise ValueError("canonical evidence cases do not match evidence_case_ids")

    candidate_case_ids = _string_list(
        case_retrieval.get("candidate_case_ids") or [],
        field="candidate_case_ids",
    )
    if len(candidate_case_ids) != len(set(candidate_case_ids)):
        raise ValueError("canonical candidate_case_ids must not contain duplicates")
    if visible_case_ids != candidate_case_ids[: len(visible_case_ids)]:
        raise ValueError("canonical evidence cases must preserve numeric shortlist order")

    methods_evidence = prompt_payload["methods_evidence"]
    if not isinstance(methods_evidence, dict):
        raise ValueError("canonical methods_evidence must be an object")
    budget = methods_evidence.get("evidence_budget")
    if not isinstance(budget, dict):
        raise ValueError("canonical evidence_budget must be an object")
    if methods_evidence != _project_methods_evidence(budget):
        raise ValueError("canonical methods_evidence contains non-projected fields")
    _validate_canonical_evidence_budget(
        budget,
        evidence_cases=evidence_cases,
        candidate_case_ids=candidate_case_ids,
        case_retrieval=case_retrieval,
    )

    policy = prompt_payload.get("recommendation_policy")
    if isinstance(policy, dict):
        allowed_bases = policy.get("allowed_evidence_bases")
        if isinstance(allowed_bases, list) and "skill_heuristic" in allowed_bases:
            raise ValueError("canonical recommendation policy must disable skill_heuristic")
    retry_evidence = prompt_payload.get("retry_evidence")
    if prompt_payload.get("task") == "recommend_initial":
        if retry_evidence is not None:
            raise ValueError("initial hifiasm prompt must not contain retry_evidence")
    else:
        _validate_retry_evidence(retry_evidence)


def _validate_authoritative_catalog_snapshot(
    prompt_payload: dict[str, Any],
    *,
    authoritative_catalog_snapshot: dict[str, Any],
) -> None:
    """Bind every prompt catalog projection to the caller-owned current catalog."""

    required_fields = ("catalog_subset", "readonly_params", "adjustable_params")
    if not isinstance(authoritative_catalog_snapshot, dict) or set(
        authoritative_catalog_snapshot
    ) != set(required_fields):
        raise ValueError("authoritative catalog snapshot must use the closed catalog structure")
    readonly = authoritative_catalog_snapshot.get("readonly_params")
    adjustable = authoritative_catalog_snapshot.get("adjustable_params")
    subset = authoritative_catalog_snapshot.get("catalog_subset")
    if (
        not isinstance(readonly, dict)
        or not isinstance(adjustable, dict)
        or not isinstance(subset, dict)
        or set(subset) != {"readonly_params", "adjustable_params"}
        or subset.get("readonly_params") != readonly
        or subset.get("adjustable_params") != adjustable
        or any(
            not isinstance(parameter, str)
            or not parameter
            or not isinstance(spec, dict)
            for projection in (readonly, adjustable)
            for parameter, spec in projection.items()
        )
    ):
        raise ValueError("authoritative catalog snapshot contains inconsistent projections")
    for field in required_fields:
        if prompt_payload.get(field) != authoritative_catalog_snapshot[field]:
            raise ValueError(
                f"{field} differs from the authoritative catalog snapshot"
            )


def _validate_retry_evidence(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "task", "signal_ids", "signal_paths", "signals"
    }:
        raise ValueError("retry_evidence must use the closed same-run structure")
    if value.get("task") not in {"retry_after_execution_failure", "retry_after_review_failure"}:
        raise ValueError("retry_evidence has an unsupported task")
    signal_ids = _string_list(value.get("signal_ids"), field="retry_evidence.signal_ids")
    signal_paths = _string_list(value.get("signal_paths"), field="retry_evidence.signal_paths")
    signals = value.get("signals")
    if not isinstance(signals, list):
        raise ValueError("retry_evidence.signals must be a list")
    projected_ids: list[str] = []
    projected_paths: list[str] = []
    for signal in signals:
        if not isinstance(signal, dict) or set(signal) != {"signal_id", "path", "value"}:
            raise ValueError("retry_evidence signal has invalid fields")
        projected_ids.append(_nonempty_string(signal.get("signal_id"), field="retry signal_id"))
        projected_paths.append(_nonempty_string(signal.get("path"), field="retry signal path"))
        _safe_optional_scalar(signal.get("value"))
    if projected_ids != signal_ids or projected_paths != signal_paths:
        raise ValueError("retry_evidence signal indexes are inconsistent")


def _validate_canonical_evidence_budget(
    budget: dict[str, Any],
    *,
    evidence_cases: list[dict[str, Any]],
    candidate_case_ids: list[str],
    case_retrieval: dict[str, Any],
) -> None:
    expected_limits = {
        "cases": HIFIASM_METHODS_EVIDENCE_CASE_LIMIT,
        "spans_per_case": HIFIASM_METHODS_EVIDENCE_SPAN_LIMIT_PER_CASE,
        "excerpt_chars_per_case": HIFIASM_METHODS_EVIDENCE_EXCERPT_CHARS_PER_CASE,
        "total_excerpt_chars": HIFIASM_METHODS_EVIDENCE_TOTAL_EXCERPT_CHARS,
        "serialized_bytes": HIFIASM_METHODS_EVIDENCE_SERIALIZED_BYTES,
    }
    if set(budget) != {"limits", "used", "case_usage", "omitted_cases", "omitted_spans"}:
        raise ValueError("canonical evidence_budget has invalid fields")
    if budget.get("limits") != expected_limits:
        raise ValueError("canonical evidence_budget limits do not match the contract")

    visible_case_ids = [case["case_id"] for case in evidence_cases]
    if len(evidence_cases) > HIFIASM_METHODS_EVIDENCE_CASE_LIMIT:
        raise ValueError("canonical evidence_cases exceeds the case limit")
    total_excerpt_chars = 0
    for case in evidence_cases:
        spans = case.get("source_excerpts") or []
        excerpt_chars = sum(len(span["quote"]) for span in spans)
        if len(spans) > HIFIASM_METHODS_EVIDENCE_SPAN_LIMIT_PER_CASE:
            raise ValueError("canonical evidence case exceeds the per-case span limit")
        if excerpt_chars > HIFIASM_METHODS_EVIDENCE_EXCERPT_CHARS_PER_CASE:
            raise ValueError("canonical evidence case exceeds the per-case excerpt limit")
        total_excerpt_chars += excerpt_chars
    if total_excerpt_chars > HIFIASM_METHODS_EVIDENCE_TOTAL_EXCERPT_CHARS:
        raise ValueError("canonical evidence cases exceed the total excerpt limit")

    expected_omitted_cases = [
        {"case_id": case_id, "reason": "serialized_evidence_bytes"}
        for case_id in candidate_case_ids[
            len(visible_case_ids):HIFIASM_METHODS_EVIDENCE_CASE_LIMIT
        ]
    ] + [
        {"case_id": case_id, "reason": "case_limit"}
        for case_id in candidate_case_ids[HIFIASM_METHODS_EVIDENCE_CASE_LIMIT:]
    ]
    if budget.get("omitted_cases") != expected_omitted_cases:
        raise ValueError("canonical omitted_cases do not match shortlist budgeting")

    omitted_spans = budget.get("omitted_spans")
    if not isinstance(omitted_spans, list):
        raise ValueError("canonical omitted_spans must be a list")
    if len(omitted_spans) > (
        HIFIASM_METHODS_EVIDENCE_CASE_LIMIT * MAX_SOURCE_EXCERPTS_PER_CASE
    ):
        raise ValueError("canonical omitted_spans exceeds the bounded audit limit")
    visible_spans_by_case = {
        case["case_id"]: {
            span["span_id"] for span in case.get("source_excerpts") or []
        }
        for case in evidence_cases
    }
    seen_omitted_spans: set[tuple[str, str]] = set()
    for item in omitted_spans:
        if not isinstance(item, dict) or set(item) != {"case_id", "span_id", "reason"}:
            raise ValueError("canonical omitted_spans entries have invalid fields")
        case_id = _nonempty_string(item.get("case_id"), field="omitted_spans.case_id")
        span_id = _nonempty_string(item.get("span_id"), field="omitted_spans.span_id")
        if item.get("reason") not in {
            "span_limit_per_case",
            "excerpt_chars_per_case",
            "excerpt_chars_total",
        }:
            raise ValueError("canonical omitted_spans contains an invalid reason")
        if case_id not in visible_spans_by_case or span_id in visible_spans_by_case[case_id]:
            raise ValueError("canonical omitted_spans must reference omitted spans of visible cases")
        key = (case_id, span_id)
        if key in seen_omitted_spans:
            raise ValueError("canonical omitted_spans must not contain duplicates")
        seen_omitted_spans.add(key)

    audit_payload = {
        "retrieval_backend": case_retrieval.get("retrieval_backend"),
        "query_id": _safe_optional_scalar(case_retrieval.get("query_id")),
        "candidate_case_ids": visible_case_ids,
        "retrieval_candidate_case_ids": candidate_case_ids,
        "warning": _safe_optional_scalar(case_retrieval.get("warning")),
        "effect_areas": list(HIFIASM_EFFECT_AREAS),
        "cases": deepcopy(evidence_cases),
        "budget": deepcopy(budget),
    }
    _refresh_budget_usage(audit_payload)
    if audit_payload["budget"] != budget:
        raise ValueError("canonical evidence_budget usage metadata is inconsistent")
    if budget["used"]["serialized_bytes"] > HIFIASM_METHODS_EVIDENCE_SERIALIZED_BYTES:
        raise ValueError("canonical evidence payload exceeds the serialized byte budget")


def _project_case_retrieval(
    raw_retrieval: dict[str, Any],
    *,
    evidence_case_ids: list[str],
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for field in _CASE_RETRIEVAL_SCALAR_FIELDS:
        if field in raw_retrieval:
            projected[field] = _safe_optional_scalar(raw_retrieval[field])
    for field in _CASE_RETRIEVAL_ID_FIELDS:
        if field in raw_retrieval:
            projected[field] = _string_list(raw_retrieval[field], field=field)
    backend = raw_retrieval.get("retrieval_backend")
    if backend != "hifiasm_numeric":
        raise ValueError("unsupported hifiasm evidence retrieval backend")
    projected["retrieval_backend"] = backend
    projected["evidence_case_ids"] = list(evidence_case_ids)
    gate = raw_retrieval.get("hifiasm_numeric_gate")
    if isinstance(gate, dict):
        projected["hifiasm_numeric_gate"] = {
            field: _safe_optional_scalar(gate[field])
            for field in _GATE_FIELDS
            if field in gate
        }
    return projected


def _project_methods_evidence(budget: dict[str, Any]) -> dict[str, Any]:
    return {
        "effect_areas": list(HIFIASM_EFFECT_AREAS),
        "max_evidence_cases": HIFIASM_METHODS_EVIDENCE_CASE_LIMIT,
        "response_contract": "case_assessments_and_recommendation",
        "assessment_rule": "assess only numeric-shortlist cases and cite only visible spans",
        "recommendation_rule": (
            "return L2 decisions with set, keep_previous, keep_default, or omit actions and a visible evidence basis"
        ),
        "evidence_budget": deepcopy(budget),
    }


def _budget_evidence_case(
    raw_case: dict[str, Any],
    *,
    total_excerpt_chars: int,
    allowed_parameter_ids: set[str],
) -> tuple[dict[str, Any], list[dict[str, str]], int]:
    projected = dict(_project_hifiasm_ranked_prompt_case(raw_case))
    case_id = _nonempty_string(projected.get("case_id"), field="case_id")
    _filter_model_visible_parameters(projected, allowed_parameter_ids=allowed_parameter_ids)
    source_excerpts = projected.get("source_excerpts") or []
    span_priorities = _supporting_span_priorities(projected)
    ordered_spans = sorted(
        enumerate(source_excerpts),
        key=lambda item: (span_priorities.get(str(item[1].get("span_id")), 2), item[0]),
    )
    safe_spans: list[dict[str, Any]] = []
    omitted_spans: list[dict[str, str]] = []
    case_excerpt_chars = 0
    for _, span in ordered_spans:
        span_id = str(span["span_id"])
        quote = str(span["quote"])
        reason = None
        if len(safe_spans) >= HIFIASM_METHODS_EVIDENCE_SPAN_LIMIT_PER_CASE:
            reason = "span_limit_per_case"
        elif case_excerpt_chars + len(quote) > HIFIASM_METHODS_EVIDENCE_EXCERPT_CHARS_PER_CASE:
            reason = "excerpt_chars_per_case"
        elif (
            total_excerpt_chars + case_excerpt_chars + len(quote)
            > HIFIASM_METHODS_EVIDENCE_TOTAL_EXCERPT_CHARS
        ):
            reason = "excerpt_chars_total"
        if reason:
            omitted_spans.append({"case_id": case_id, "span_id": span_id, "reason": reason})
            continue
        safe_spans.append(span)
        case_excerpt_chars += len(quote)

    source_order = {str(span["span_id"]): index for index, span in enumerate(source_excerpts)}
    safe_spans.sort(key=lambda span: source_order[str(span["span_id"])])
    omitted_spans.sort(key=lambda item: source_order[item["span_id"]])
    projected["source_excerpts"] = safe_spans
    projected["numeric_match"] = _project_numeric_match(projected.get("numeric_match"))
    _filter_span_references(projected, visible_span_ids={span["span_id"] for span in safe_spans})
    _drop_unsupported_prompt_facts(projected)
    return projected, omitted_spans, case_excerpt_chars


def _filter_model_visible_parameters(
    case: dict[str, Any],
    *,
    allowed_parameter_ids: set[str],
) -> None:
    intervention = case.get("intervention")
    if not isinstance(intervention, dict):
        return
    parameters = intervention.get("parameters")
    if isinstance(parameters, dict):
        intervention["parameters"] = {
            parameter: value
            for parameter, value in parameters.items()
            if parameter in allowed_parameter_ids
        }


def _supporting_span_priorities(case: dict[str, Any]) -> dict[str, int]:
    priorities: dict[str, int] = {}
    parameters = ((case.get("intervention") or {}).get("parameters") or {})
    for parameter in parameters.values():
        if isinstance(parameter, dict) and parameter.get("reported") is True:
            for span_id in parameter.get("evidence_span_ids") or []:
                priorities[str(span_id)] = 0
    for claim in case.get("rationale_claims") or []:
        if isinstance(claim, dict) and claim.get("explicitness") == "explicit":
            for span_id in claim.get("evidence_span_ids") or []:
                priorities.setdefault(str(span_id), 1)
    return priorities


def _drop_unsupported_prompt_facts(case: dict[str, Any]) -> None:
    intervention = case.get("intervention")
    if isinstance(intervention, dict) and isinstance(intervention.get("parameters"), dict):
        intervention["parameters"] = {
            parameter: value
            for parameter, value in intervention["parameters"].items()
            if not (
                isinstance(value, dict)
                and value.get("reported") is True
                and not value.get("evidence_span_ids")
            )
        }
    rationale_claims = case.get("rationale_claims")
    if isinstance(rationale_claims, list):
        case["rationale_claims"] = [
            claim
            for claim in rationale_claims
            if not (
                isinstance(claim, dict)
                and claim.get("explicitness") == "explicit"
                and not claim.get("evidence_span_ids")
            )
        ]


def _project_numeric_match(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    projected = {
        field: _safe_optional_scalar(value[field])
        for field in _NUMERIC_MATCH_FIELDS
        if field in value and field != "feature_distances"
    }
    feature_distances = value.get("feature_distances")
    if isinstance(feature_distances, dict):
        projected["feature_distances"] = {
            str(feature): {
                field: _safe_optional_scalar(distance[field])
                for field in _FEATURE_DISTANCE_FIELDS
                if isinstance(distance, dict) and field in distance
            }
            for feature, distance in feature_distances.items()
            if isinstance(feature, str) and isinstance(distance, dict)
        }
    return projected


def _filter_span_references(value: Any, *, visible_span_ids: set[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _filter_span_references(item, visible_span_ids=visible_span_ids)
        return
    if not isinstance(value, dict):
        return
    for field, item in value.items():
        if field == "evidence_span_ids" and isinstance(item, list):
            value[field] = [span_id for span_id in item if span_id in visible_span_ids]
        else:
            _filter_span_references(item, visible_span_ids=visible_span_ids)


def _safe_optional_scalar(value: Any) -> Any:
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _refresh_budget_usage(payload: dict[str, Any]) -> None:
    cases = payload["cases"]
    case_usage = [
        {
            "case_id": case["case_id"],
            "spans": len(case["source_excerpts"]),
            "excerpt_chars": sum(len(span["quote"]) for span in case["source_excerpts"]),
        }
        for case in cases
    ]
    payload["budget"]["case_usage"] = case_usage
    payload["budget"]["used"] = {
        "cases": len(cases),
        "spans": sum(item["spans"] for item in case_usage),
        "excerpt_chars": sum(item["excerpt_chars"] for item in case_usage),
        "serialized_bytes": 0,
    }
    serialized_bytes = -1
    while serialized_bytes != payload["budget"]["used"]["serialized_bytes"]:
        payload["budget"]["used"]["serialized_bytes"] = serialized_bytes
        serialized_bytes = len(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
        )
    payload["budget"]["used"]["serialized_bytes"] = serialized_bytes


def _build_prompt_sanitizer():
    """Create the public sanitizer while hiding its attestation minting boundary."""

    registry: dict[int, tuple[weakref.ReferenceType[Any], str, bool]] = {}
    replay_registry: dict[int, tuple[weakref.ReferenceType[Any], str]] = {}

    class CanonicalPrompt(dict[str, Any]):
        __slots__ = ("__weakref__",)

        def __new__(cls, *args: Any, **kwargs: Any) -> "CanonicalPrompt":
            raise TypeError("canonical hifiasm prompts can only be created by Task 4 validation")

        def __copy__(self) -> "CanonicalPrompt":
            return _copy_registered(self)

        def __deepcopy__(self, memo: dict[int, Any]) -> "CanonicalPrompt":
            return _copy_registered(self, memo=memo)

    class PersistedInitialPrompt(dict[str, Any]):
        __slots__ = ("__weakref__",)

        def __new__(cls, *args: Any, **kwargs: Any) -> "PersistedInitialPrompt":
            raise TypeError("persisted prompts can only be attested by the artifact loader")

    def _semantic_proof(prompt: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(prompt, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _registered_attestation(prompt: dict[str, Any]) -> tuple[str, bool] | None:
        entry = registry.get(id(prompt))
        if entry is None or entry[0]() is not prompt:
            return None
        return entry[1], entry[2]

    def _register(
        prompt: CanonicalPrompt,
        proof: str,
        *,
        finalized: bool,
    ) -> CanonicalPrompt:
        prompt_id = id(prompt)

        def _discard(reference: weakref.ReferenceType[Any]) -> None:
            entry = registry.get(prompt_id)
            if entry is not None and entry[0] is reference:
                registry.pop(prompt_id, None)

        registry[prompt_id] = (weakref.ref(prompt, _discard), proof, finalized)
        return prompt

    def _new_registered(
        values: dict[str, Any],
        proof: str | None = None,
        *,
        finalized: bool = False,
    ) -> CanonicalPrompt:
        prompt = dict.__new__(CanonicalPrompt)
        dict.__init__(prompt, values)
        return _register(
            prompt,
            proof or _semantic_proof(prompt),
            finalized=finalized,
        )

    def _copy_registered(
        prompt: CanonicalPrompt,
        *,
        memo: dict[int, Any] | None = None,
    ) -> CanonicalPrompt:
        attestation = _registered_attestation(prompt)
        if attestation is None:
            raise ValueError("canonical hifiasm prompt lacks validation-owned attestation")
        proof, finalized = attestation
        copied = dict.__new__(CanonicalPrompt)
        if memo is None:
            values = dict(prompt)
        else:
            memo[id(prompt)] = copied
            values = deepcopy(dict(prompt), memo)
        dict.__init__(copied, values)
        return _register(copied, proof, finalized=finalized)

    def _require_registered(
        prompt: dict[str, Any],
        *,
        require_final: bool = False,
    ) -> None:
        attestation = _registered_attestation(prompt)
        if attestation is None:
            raise ValueError("canonical hifiasm prompt lacks validation-owned attestation")
        proof, finalized = attestation
        if _semantic_proof(prompt) != proof:
            raise ValueError("canonical hifiasm prompt differs from its validation-owned attestation")
        if require_final and not finalized:
            raise ValueError("canonical hifiasm prompt lacks final-envelope attestation")

    def _attest_loaded_initial(prompt_payload: dict[str, Any]) -> PersistedInitialPrompt:
        """Mint a process-local replay envelope after the loader verifies the artifact."""

        _validate_canonical_hifiasm_methods_prompt_payload(prompt_payload)
        prompt = dict.__new__(PersistedInitialPrompt)
        dict.__init__(prompt, prompt_payload)
        prompt_id = id(prompt)

        def _discard(reference: weakref.ReferenceType[Any]) -> None:
            entry = replay_registry.get(prompt_id)
            if entry is not None and entry[0] is reference:
                replay_registry.pop(prompt_id, None)

        replay_registry[prompt_id] = (
            weakref.ref(prompt, _discard),
            _semantic_proof(prompt),
        )
        return prompt

    def _require_loaded_initial(prompt_payload: dict[str, Any]) -> None:
        entry = replay_registry.get(id(prompt_payload))
        if entry is None or entry[0]() is not prompt_payload:
            raise ValueError("retry replay requires a loader-attested attempt 0 prompt")
        if _semantic_proof(prompt_payload) != entry[1]:
            raise ValueError("loader-attested attempt 0 prompt was mutated")

    def _require_attested(prompt_payload: dict[str, Any]) -> dict[str, Any]:
        """Require Task 4's validation-owned proof without copying the prompt."""

        _require_registered(prompt_payload)
        _validate_canonical_hifiasm_methods_prompt_payload(prompt_payload)
        return prompt_payload

    def _seal_final(
        prompt_payload: dict[str, Any],
        *,
        prompt_payload_ref: str,
        prompt_payload_hash: str,
    ) -> dict[str, Any]:
        """Bind the final replay ref/hash to a fresh validation-owned envelope."""

        _require_registered(prompt_payload)
        if any(field in prompt_payload for field in _PROMPT_SELF_FIELDS):
            raise ValueError("canonical hifiasm prompt is already finalized")
        final_values = dict(prompt_payload)
        final_values.update(
            {
                "prompt_payload_ref": prompt_payload_ref,
                "prompt_payload_hash": prompt_payload_hash,
            }
        )
        finalized = _new_registered(final_values, finalized=True)
        _validate_canonical_hifiasm_methods_prompt_payload(finalized)
        return finalized

    def _require_final_attested(prompt_payload: dict[str, Any]) -> dict[str, Any]:
        """Require the exact final object sealed after replay ref/hash materialization."""

        _require_registered(prompt_payload, require_final=True)
        _validate_canonical_hifiasm_methods_prompt_payload(prompt_payload)
        return prompt_payload

    def _rehydrate_retry(
        persisted_initial_prompt: dict[str, Any],
        retry_overlay: dict[str, Any],
        *,
        authoritative_catalog_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-attest a verified JSON attempt-0 prompt for one same-run retry."""

        _require_loaded_initial(persisted_initial_prompt)
        if persisted_initial_prompt.get("task") != "recommend_initial":
            raise ValueError("retry replay requires the canonical attempt 0 prompt")
        _validate_canonical_hifiasm_methods_prompt_payload(persisted_initial_prompt)
        _validate_authoritative_catalog_snapshot(
            persisted_initial_prompt,
            authoritative_catalog_snapshot=authoritative_catalog_snapshot,
        )
        if retry_overlay.get("task") not in {
            "retry_after_execution_failure",
            "retry_after_review_failure",
        }:
            raise ValueError("retry replay has an unsupported task")
        _validate_authoritative_catalog_snapshot(
            retry_overlay,
            authoritative_catalog_snapshot=authoritative_catalog_snapshot,
        )
        immutable_catalog_fields = (
            "catalog_subset",
            "readonly_params",
            "adjustable_params",
        )
        for field in immutable_catalog_fields:
            if retry_overlay.get(field) != persisted_initial_prompt.get(field):
                raise ValueError(
                    f"retry replay {field} differs from the canonical attempt 0 catalog"
                )
        inherited_fields = {
            *immutable_catalog_fields,
            "rules",
            "recommendation_policy",
            "skill_instructions",
            "evidence_payload_ref",
            "evidence_payload_hash",
            "case_selection",
            "case_retrieval",
            "evidence_cases",
            "methods_evidence",
        }
        selection_identity_fields = {
            "evidence_payload_ref",
            "evidence_payload_hash",
            "case_selection",
            "case_retrieval",
            "evidence_cases",
            "methods_evidence",
        }
        for field in selection_identity_fields:
            if retry_overlay.get(field) != persisted_initial_prompt.get(field):
                raise ValueError(
                    f"retry replay {field} differs from the canonical attempt 0 projection"
                )
        canonical = {
            field: deepcopy(value)
            for field, value in persisted_initial_prompt.items()
            if field not in _PROMPT_SELF_FIELDS
        }
        for field in _PROMPT_COPY_FIELDS:
            if field in inherited_fields:
                continue
            if field in retry_overlay:
                canonical[field] = deepcopy(retry_overlay[field])
        initial_profile = persisted_initial_prompt.get("prompt_profile")
        retry_profile = retry_overlay.get("prompt_profile")
        if not isinstance(initial_profile, dict) or not isinstance(retry_profile, dict):
            raise ValueError("retry replay requires initial and retry prompt profiles")
        canonical["prompt_profile"] = deepcopy(initial_profile)
        for field in ("previous_attempt", "assembly_review"):
            if field in retry_profile:
                canonical["prompt_profile"][field] = deepcopy(retry_profile[field])
        canonical["selected_cases"] = []
        canonical["retry_evidence"] = deepcopy(retry_overlay.get("retry_evidence"))
        attested = _new_registered(canonical)
        _validate_canonical_hifiasm_methods_prompt_payload(attested)
        _validate_authoritative_catalog_snapshot(
            attested,
            authoritative_catalog_snapshot=authoritative_catalog_snapshot,
        )
        return attested

    def _sanitize(
        prompt_payload: dict[str, Any],
        *,
        authoritative_catalog_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_authoritative_catalog_snapshot(
            prompt_payload,
            authoritative_catalog_snapshot=authoritative_catalog_snapshot,
        )
        if _PROMPT_SCHEMA_VERSION_FIELD in prompt_payload:
            _require_attested(prompt_payload)
            return deepcopy(prompt_payload)
        if not uses_hifiasm_methods_contract(prompt_payload):
            return deepcopy(prompt_payload)
        raw_retrieval = prompt_payload.get("case_retrieval") or {}
        evidence_payload = build_hifiasm_evidence_payload(
            evidence_cases=prompt_payload.get("evidence_cases") or [],
            case_retrieval=raw_retrieval,
            allowed_parameter_ids=set(authoritative_catalog_snapshot["adjustable_params"]),
        )
        case_retrieval = _project_case_retrieval(
            raw_retrieval,
            evidence_case_ids=evidence_payload["candidate_case_ids"],
        )
        canonical = {
            field: deepcopy(prompt_payload[field])
            for field in _PROMPT_COPY_FIELDS
            if field in prompt_payload
            and (field != "retry_evidence" or prompt_payload.get("task") != "recommend_initial")
        }
        canonical.update(
            {
                _PROMPT_SCHEMA_VERSION_FIELD: HIFIASM_METHODS_PROMPT_SCHEMA_VERSION,
                "selected_cases": [],
                "case_selection": deepcopy(case_retrieval),
                "case_retrieval": case_retrieval,
                "evidence_cases": evidence_payload["cases"],
                "methods_evidence": _project_methods_evidence(evidence_payload["budget"]),
            }
        )
        policy = canonical.get("recommendation_policy")
        if isinstance(policy, dict):
            allowed = policy.get("allowed_evidence_bases")
            if isinstance(allowed, list):
                policy["allowed_evidence_bases"] = [
                    basis for basis in allowed if basis != "skill_heuristic"
                ]
            policy["no_case_behavior"] = (
                "continue with readonly L1 facts and catalog defaults; "
                "prefer conservative L2 changes"
            )
        attested = _new_registered(canonical)
        _validate_canonical_hifiasm_methods_prompt_payload(attested)
        _validate_authoritative_catalog_snapshot(
            attested,
            authoritative_catalog_snapshot=authoritative_catalog_snapshot,
        )
        return attested

    _sanitize.__doc__ = (
        "Build the single closed canonical hifiasm prompt used by the full call chain."
    )
    _sanitize.__name__ = "sanitize_hifiasm_methods_prompt_payload"
    _sanitize.__qualname__ = "sanitize_hifiasm_methods_prompt_payload"
    return (
        _sanitize,
        _attest_loaded_initial,
        _rehydrate_retry,
        _require_attested,
        _seal_final,
        _require_final_attested,
    )


(
    sanitize_hifiasm_methods_prompt_payload,
    _attest_loaded_hifiasm_initial_prompt_snapshot,
    rehydrate_hifiasm_methods_retry_prompt_payload,
    _require_attested_hifiasm_methods_prompt_payload,
    _seal_attested_hifiasm_methods_prompt_payload,
    _require_final_attested_hifiasm_methods_prompt_payload,
) = _build_prompt_sanitizer()
del _build_prompt_sanitizer


def normalize_hifiasm_methods_response(
    response: dict[str, Any],
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate Task 4's response and add the legacy downstream projection.

    The returned object preserves the complete Methods assessment and parameter
    decisions while projecting only ``action=set`` values into ``params`` for the
    existing catalog/rule validator and command builder.
    """

    if not isinstance(response, dict):
        raise ValueError("hifiasm Methods-aware response must be an object")
    if not uses_hifiasm_methods_contract(prompt_payload):
        raise ValueError("hifiasm Methods-aware response requires attested hifiasm evidence retrieval")
    evidence_cases = prompt_payload.get("evidence_cases") or []
    cases_by_id = {
        str(case.get("case_id")): case
        for case in evidence_cases
        if isinstance(case, dict) and case.get("case_id")
    }
    case_retrieval = prompt_payload.get("case_retrieval") or {}
    shortlist_ids = set(
        case_retrieval.get("evidence_case_ids")
        or case_retrieval.get("candidate_case_ids")
        or ((prompt_payload.get("case_selection") or {}).get("selected_case_ids"))
        or []
    )
    if set(cases_by_id) != shortlist_ids:
        raise ValueError("prompt evidence cases do not match the numeric shortlist")

    raw_assessments = response.get("case_assessments")
    if not isinstance(raw_assessments, list):
        raise ValueError("case_assessments must be a list")
    assessments: list[dict[str, Any]] = []
    accepted_ids: set[str] = set()
    assessed_ids: set[str] = set()
    for raw_assessment in raw_assessments:
        assessment = _validate_case_assessment(
            raw_assessment,
            cases_by_id=cases_by_id,
            shortlist_ids=shortlist_ids,
        )
        case_id = assessment["case_id"]
        if case_id in assessed_ids:
            raise ValueError(f"duplicate case assessment for {case_id!r}")
        assessed_ids.add(case_id)
        if assessment["accepted_as_support"]:
            accepted_ids.add(case_id)
        assessments.append(assessment)

    selected_case_ids = _string_list(
        response.get("selected_case_ids"),
        field="selected_case_ids",
    )
    if len(selected_case_ids) != len(set(selected_case_ids)):
        raise ValueError("selected_case_ids must not contain duplicates")
    unsupported_selected = set(selected_case_ids) - accepted_ids
    if unsupported_selected:
        raise ValueError(
            "selected_case_ids must reference an accepted assessment; "
            f"unsupported={sorted(unsupported_selected)}"
        )

    recommendation = response.get("recommendation")
    if not isinstance(recommendation, dict):
        raise ValueError("recommendation must be an object")
    retry_disposition = None
    if prompt_payload.get("task") != "recommend_initial":
        retry_disposition = recommendation.get("retry_disposition")
    raw_decisions = recommendation.get("adjustable_params")
    if not isinstance(raw_decisions, dict):
        raise ValueError("recommendation.adjustable_params must be an object")

    allowed_parameters = set((prompt_payload.get("adjustable_params") or {}).keys())
    decision_parameters = set(raw_decisions)
    if decision_parameters != allowed_parameters:
        missing = sorted(allowed_parameters - decision_parameters)
        unexpected = sorted(decision_parameters - allowed_parameters)
        raise ValueError(
            "recommendation.adjustable_params must contain exactly one decision for every "
            f"catalog adjustable L2 parameter; missing={missing}, unexpected={unexpected}"
        )
    params: dict[str, Any] = {}
    omitted_parameters: list[str] = []
    parameter_evidence: dict[str, dict[str, Any]] = {}
    parameter_decisions: dict[str, dict[str, Any]] = {}
    changed_parameters: set[str] = set()
    for parameter, raw_decision in raw_decisions.items():
        if parameter not in allowed_parameters:
            raise ValueError(f"parameter {parameter!r} is not an adjustable L2 parameter")
        decision, evidence = _validate_parameter_decision(
            parameter,
            raw_decision,
            parameter_spec=(prompt_payload.get("adjustable_params") or {}).get(parameter) or {},
            prompt_payload=prompt_payload,
            selected_case_ids=set(selected_case_ids),
            accepted_case_ids=accepted_ids,
            cases_by_id=cases_by_id,
        )
        parameter_decisions[parameter] = decision
        if decision["action"] == "set":
            params[parameter] = decision["value"]
            parameter_evidence[parameter] = evidence
            changed_parameters.add(parameter)
        elif decision["action"] == "keep_previous":
            params[parameter] = decision["value"]
            parameter_evidence[parameter] = evidence
        else:
            omitted_parameters.append(parameter)

    task = prompt_payload.get("task")
    if task != "recommend_initial" and retry_disposition not in {
        "retry", "stop", "expert_review"
    }:
        raise ValueError("retry recommendation requires retry_disposition=retry, stop, or expert_review")
    if task == "retry_after_execution_failure" and retry_disposition != "retry":
        raise ValueError("execution-failure retry recommendation requires retry_disposition=retry")
    if retry_disposition in {"stop", "expert_review"} and changed_parameters:
        raise ValueError("stop/expert_review retry disposition must not invent parameter changes")
    if task == "retry_after_review_failure" and retry_disposition == "retry" and not changed_parameters:
        raise ValueError("retry disposition requires at least one evidence-backed parameter change")

    summary = _nonempty_string(response.get("summary"), field="summary")
    normalized = deepcopy(response)
    normalized.update(
        {
            "case_assessments": assessments,
            "parameter_decisions": parameter_decisions,
            "params": params,
            "parameter_evidence": parameter_evidence,
            "omitted_parameters": omitted_parameters,
            "selected_case_ids": selected_case_ids,
            "summary": summary,
            "reasoning": summary,
        }
    )
    return normalized


def _validate_case_assessment(
    raw_assessment: Any,
    *,
    cases_by_id: dict[str, dict[str, Any]],
    shortlist_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(raw_assessment, dict):
        raise ValueError("case_assessments entries must be objects")
    case_id = _nonempty_string(raw_assessment.get("case_id"), field="case_assessment.case_id")
    if case_id not in shortlist_ids or case_id not in cases_by_id:
        raise ValueError(f"assessed case {case_id!r} is outside the numeric shortlist")
    relevance = raw_assessment.get("relevance")
    if relevance not in _ALLOWED_RELEVANCE:
        raise ValueError(f"case assessment relevance must be one of {sorted(_ALLOWED_RELEVANCE)}")
    accepted = raw_assessment.get("accepted_as_support")
    if not isinstance(accepted, bool):
        raise ValueError("accepted_as_support must be a boolean")
    if accepted and relevance == "not_relevant":
        raise ValueError("not_relevant case assessment cannot be accepted as support")

    visible_spans = _visible_span_ids(cases_by_id[case_id])
    raw_findings = raw_assessment.get("transferable_findings") or []
    if not isinstance(raw_findings, list):
        raise ValueError("transferable_findings must be a list")
    findings: list[dict[str, Any]] = []
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict):
            raise ValueError("transferable_findings entries must be objects")
        effect_area = raw_finding.get("effect_area")
        if effect_area not in HIFIASM_EFFECT_AREAS:
            raise ValueError(f"unsupported effect_area {effect_area!r}")
        span_ids = _string_list(
            raw_finding.get("evidence_span_ids"),
            field="transferable_findings.evidence_span_ids",
            require_nonempty=True,
        )
        invalid_spans = set(span_ids) - visible_spans
        if invalid_spans:
            raise ValueError(
                f"transferable finding cites a non-visible span for {case_id!r}: {sorted(invalid_spans)}"
            )
        findings.append(
            {
                **deepcopy(raw_finding),
                "effect_area": effect_area,
                "direction": _nonempty_string(raw_finding.get("direction"), field="transferable_findings.direction"),
                "evidence_span_ids": span_ids,
                "explanation": _nonempty_string(raw_finding.get("explanation"), field="transferable_findings.explanation"),
            }
        )

    conflicts = raw_assessment.get("conflicts_or_limits") or []
    if not isinstance(conflicts, list) or not all(isinstance(item, str) and item.strip() for item in conflicts):
        raise ValueError("conflicts_or_limits must be a list of non-empty strings")
    return {
        **deepcopy(raw_assessment),
        "case_id": case_id,
        "relevance": relevance,
        "transferable_findings": findings,
        "conflicts_or_limits": conflicts,
        "accepted_as_support": accepted,
    }


def _validate_parameter_decision(
    parameter: str,
    raw_decision: Any,
    *,
    parameter_spec: dict[str, Any],
    prompt_payload: dict[str, Any],
    selected_case_ids: set[str],
    accepted_case_ids: set[str],
    cases_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw_decision, dict):
        raise ValueError(f"parameter decision for {parameter!r} must be an object")
    action = raw_decision.get("action")
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"parameter decision action must be one of {sorted(_ALLOWED_ACTIONS)}")
    if action == "set" and "value" not in raw_decision:
        raise ValueError(f"parameter {parameter!r} action=set requires value")
    previous_value = _lookup_path(
        prompt_payload.get("prompt_profile") or {},
        f"previous_attempt.params.{parameter}",
    )
    if action == "keep_previous":
        if prompt_payload.get("task") == "recommend_initial" or previous_value is None:
            raise ValueError(f"parameter {parameter!r} has no previous retry value to keep")
        if "value" in raw_decision and raw_decision.get("value") != previous_value:
            raise ValueError(f"parameter {parameter!r} keep_previous value differs from previous")
        raw_decision = {**raw_decision, "value": previous_value}
    if action == "keep_default" and prompt_payload.get("task") != "recommend_initial" and previous_value is not None:
        raise ValueError(f"retry parameter {parameter!r} must keep_previous, not reset to catalog default")
    if action == "keep_default" and parameter_spec.get("default") is None:
        raise ValueError(f"parameter {parameter!r} has no explicit catalog default to keep")
    if action == "omit" and parameter_spec.get("default") is not None:
        raise ValueError(f"parameter {parameter!r} has an explicit catalog default; use keep_default")
    confidence = raw_decision.get("confidence")
    if confidence not in _ALLOWED_CONFIDENCE:
        raise ValueError(f"parameter confidence must be one of {sorted(_ALLOWED_CONFIDENCE)}")
    reason = _nonempty_string(raw_decision.get("reason"), field=f"{parameter}.reason")

    raw_bases = raw_decision.get("bases", raw_decision.get("basis"))
    bases = [raw_bases] if isinstance(raw_bases, str) else raw_bases
    if not isinstance(bases, list) or not bases or not all(isinstance(item, str) for item in bases):
        raise ValueError(f"parameter {parameter!r} requires at least one evidence basis")
    unknown_bases = set(bases) - _ALLOWED_EVIDENCE_BASES
    if unknown_bases:
        raise ValueError(f"parameter {parameter!r} has unknown evidence basis: {sorted(unknown_bases)}")

    supporting_case_ids = _string_list(
        raw_decision.get("supporting_case_ids") or [],
        field=f"{parameter}.supporting_case_ids",
    )
    supporting_span_ids = _string_list(
        raw_decision.get("supporting_span_ids") or [],
        field=f"{parameter}.supporting_span_ids",
    )
    if "case" in bases:
        if not supporting_case_ids or not supporting_span_ids:
            raise ValueError(f"parameter {parameter!r} basis=case requires case and span citations")
        invalid_cases = set(supporting_case_ids) - selected_case_ids
        if invalid_cases or not set(supporting_case_ids).issubset(accepted_case_ids):
            raise ValueError(
                f"parameter {parameter!r} case evidence must cite a selected accepted case: "
                f"{sorted(invalid_cases or (set(supporting_case_ids) - accepted_case_ids))}"
            )
        visible_spans = set().union(
            *(_visible_span_ids(cases_by_id[case_id]) for case_id in supporting_case_ids)
        )
        invalid_spans = set(supporting_span_ids) - visible_spans
        if invalid_spans:
            raise ValueError(
                f"parameter {parameter!r} cites a non-visible span: {sorted(invalid_spans)}"
            )
        cases_without_cited_span = [
            case_id
            for case_id in supporting_case_ids
            if not (_visible_span_ids(cases_by_id[case_id]) & set(supporting_span_ids))
        ]
        if cases_without_cited_span:
            raise ValueError(
                f"parameter {parameter!r} cites cases without a span from that case: "
                f"{cases_without_cited_span}"
            )
        unsupported_spans_by_case = {
            case_id: sorted(
                (_visible_span_ids(cases_by_id[case_id]) & set(supporting_span_ids))
                - allowed_parameter_evidence_span_ids(cases_by_id[case_id], parameter)
            )
            for case_id in supporting_case_ids
        }
        unsupported_spans_by_case = {
            case_id: span_ids
            for case_id, span_ids in unsupported_spans_by_case.items()
            if span_ids
        }
        if unsupported_spans_by_case:
            raise ValueError(
                f"parameter {parameter!r} cites visible spans that are not curated support "
                f"for that parameter or its effect area: {unsupported_spans_by_case}"
            )
    elif supporting_case_ids or supporting_span_ids:
        raise ValueError(f"parameter {parameter!r} has case citations without basis=case")

    fact_paths = _string_list(raw_decision.get("fact_paths") or [], field=f"{parameter}.fact_paths")
    if "profile_fact" in bases:
        if not fact_paths:
            raise ValueError(f"parameter {parameter!r} basis=profile_fact requires fact_paths")
        retry_fact_paths = [
            path
            for path in fact_paths
            if path == "assembly_review"
            or path.startswith("assembly_review.")
            or path == "previous_attempt.failure_class"
            or path.startswith("previous_attempt.failure_class.")
            or path == "previous_attempt.execution_status"
            or path.startswith("previous_attempt.execution_status.")
            or path == "previous_attempt.execution_status_detail"
            or path.startswith("previous_attempt.execution_status_detail.")
            or path == "previous_attempt.exit_code"
            or path.startswith("previous_attempt.exit_code.")
            or path == "previous_attempt.stderr_ref"
            or path.startswith("previous_attempt.stderr_ref.")
            or path == "previous_attempt.stdout_ref"
            or path.startswith("previous_attempt.stdout_ref.")
            or path == "previous_attempt.resource_facts"
            or path.startswith("previous_attempt.resource_facts.")
        ]
        if retry_fact_paths:
            raise ValueError(
                f"parameter {parameter!r} retry failure facts require basis=retry_failure: "
                f"{retry_fact_paths}"
            )
        missing_paths = [
            path
            for path in fact_paths
            if _lookup_path(prompt_payload.get("prompt_profile") or {}, path) is None
        ]
        if missing_paths:
            raise ValueError(f"parameter {parameter!r} cites profile facts absent from the prompt: {missing_paths}")

    heuristic_labels = _string_list(
        raw_decision.get("heuristic_labels") or [],
        field=f"{parameter}.heuristic_labels",
    )
    if "skill_heuristic" in bases or heuristic_labels:
        raise ValueError(
            f"parameter {parameter!r} cannot use skill_heuristic until structured "
            "prompt-visible heuristic IDs are available"
        )

    retry_signals = _string_list(raw_decision.get("retry_signals") or [], field=f"{parameter}.retry_signals")
    visible_retry_signals = _visible_retry_signal_refs(prompt_payload)
    invalid_retry_signals = set(retry_signals) - visible_retry_signals
    if invalid_retry_signals:
        raise ValueError(
            f"parameter {parameter!r} cites retry signals absent from structured prompt evidence: "
            f"{sorted(invalid_retry_signals)}"
        )
    if "retry_failure" in bases:
        if prompt_payload.get("task") == "recommend_initial":
            raise ValueError("retry_failure evidence is not valid for recommend_initial")
        if not retry_signals:
            raise ValueError(f"parameter {parameter!r} basis=retry_failure requires retry_signals")
        if not visible_retry_signals:
            raise ValueError(
                f"parameter {parameter!r} basis=retry_failure requires structured retry evidence visible in the prompt"
            )
    elif retry_signals:
        raise ValueError(f"parameter {parameter!r} has retry_signals without basis=retry_failure")

    if prompt_payload.get("task") != "recommend_initial" and action == "set":
        if "retry_failure" not in bases:
            raise ValueError(
                f"retry parameter change {parameter!r} requires retry_failure evidence"
            )

    decision = {
        **deepcopy(raw_decision),
        "action": action,
        "bases": list(dict.fromkeys(bases)),
        "supporting_case_ids": supporting_case_ids,
        "supporting_span_ids": supporting_span_ids,
        "fact_paths": fact_paths,
        "heuristic_labels": heuristic_labels,
        "retry_signals": retry_signals,
        "confidence": confidence,
        "reason": reason,
    }
    evidence = {
        "bases": decision["bases"],
        "case_ids": supporting_case_ids,
        "span_ids": supporting_span_ids,
        "fact_paths": fact_paths,
        "heuristic_labels": heuristic_labels,
        "retry_signals": retry_signals,
    }
    return decision, evidence


def _visible_span_ids(case: dict[str, Any]) -> set[str]:
    return {
        str(span.get("span_id"))
        for span in case.get("source_excerpts") or []
        if isinstance(span, dict) and span.get("span_id")
    }


def allowed_parameter_evidence_span_ids(case: dict[str, Any], parameter: str) -> set[str]:
    """Return case-local spans curated to support one parameter or its effect area.

    Direct parameter spans establish the parameter anchor.  A rationale claim may
    add only its own spans, and only when that same claim intersects the direct
    anchor.  Merely sharing an effect area with another claim is not evidence of a
    parameter-to-claim relationship.
    """

    parameters = ((case.get("intervention") or {}).get("parameters") or {})
    parameter_fact = parameters.get(parameter) if isinstance(parameters, dict) else None
    if not isinstance(parameter_fact, dict):
        return set()
    direct_spans = {
        str(span_id)
        for span_id in parameter_fact.get("evidence_span_ids") or []
        if isinstance(span_id, str)
    }
    if not direct_spans:
        return set()
    claims = [claim for claim in case.get("rationale_claims") or [] if isinstance(claim, dict)]
    rationale_spans = {
        str(span_id)
        for claim in claims
        if direct_spans
        & {
            str(claim_span_id)
            for claim_span_id in claim.get("evidence_span_ids") or []
            if isinstance(claim_span_id, str)
        }
        for span_id in claim.get("evidence_span_ids") or []
        if isinstance(span_id, str)
    }
    return direct_spans | rationale_spans


def _visible_retry_signal_refs(prompt_payload: dict[str, Any]) -> set[str]:
    """Return Task 5-style structured retry IDs/paths, or an empty fail-closed set."""

    retry_evidence = prompt_payload.get("retry_evidence")
    if not isinstance(retry_evidence, dict):
        return set()
    visible: set[str] = set()
    for field in ("signal_ids", "signal_paths"):
        values = retry_evidence.get(field)
        if isinstance(values, list):
            visible.update(item for item in values if isinstance(item, str) and item.strip())
    signals = retry_evidence.get("signals")
    if isinstance(signals, list):
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            for field in ("signal_id", "path"):
                value = signal.get(field)
                if isinstance(value, str) and value.strip():
                    visible.add(value)
    return visible


def _string_list(value: Any, *, field: str, require_nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    if require_nonempty and not value:
        raise ValueError(f"{field} must not be empty")
    return list(value)


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value
