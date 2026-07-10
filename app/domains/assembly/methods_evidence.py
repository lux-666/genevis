"""Hifiasm Methods-aware evidence and recommendation contracts.

This module deliberately contains only deterministic, prompt-visible logic. Numeric
ranking remains owned by ``numeric_case_retriever`` and catalog/rule validation
remains owned by the assembly parameter validator.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

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

_ALLOWED_RELEVANCE = {"strong", "medium", "weak", "not_relevant"}
_ALLOWED_ACTIONS = {"set", "keep_default", "omit"}
_ALLOWED_CONFIDENCE = {"strong", "medium", "weak"}
_ALLOWED_EVIDENCE_BASES = {
    "case",
    "profile_fact",
    "skill_heuristic",
    "retry_failure",
    "catalog_default",
}
_EVIDENCE_CASE_FIELDS = (
    "source",
    "numeric_match",
    "observed_facts",
    "intervention",
    "rationale_claims",
    "source_excerpts",
    "outcomes",
)


def uses_hifiasm_methods_contract(prompt_payload: dict[str, Any]) -> bool:
    """Return whether the prompt uses Task 4's hifiasm evidence contract."""

    retrieval = prompt_payload.get("case_retrieval") or prompt_payload.get("case_selection") or {}
    return retrieval.get("retrieval_backend") == "hifiasm_numeric"


def build_hifiasm_evidence_payload(
    *,
    evidence_cases: list[dict[str, Any]],
    case_retrieval: dict[str, Any],
) -> dict[str, Any]:
    """Build the bounded, replayable Methods evidence payload.

    The numeric retriever is the only authority allowed to define this candidate
    set. Full source documents are intentionally not accepted by this builder;
    only the already validated prompt-safe case representation is copied.
    """

    if case_retrieval.get("retrieval_backend") != "hifiasm_numeric":
        raise ValueError("hifiasm evidence payload requires retrieval_backend=hifiasm_numeric")
    retrieval_candidate_ids = _string_list(
        case_retrieval.get("candidate_case_ids") or [],
        field="candidate_case_ids",
    )
    if len(retrieval_candidate_ids) != len(set(retrieval_candidate_ids)):
        raise ValueError("candidate_case_ids must not contain duplicates")
    declared_evidence_ids = case_retrieval.get("evidence_case_ids")
    if declared_evidence_ids is None:
        evidence_case_ids = retrieval_candidate_ids[:HIFIASM_METHODS_EVIDENCE_CASE_LIMIT]
    else:
        evidence_case_ids = _string_list(
            declared_evidence_ids,
            field="evidence_case_ids",
        )
    if len(evidence_case_ids) > HIFIASM_METHODS_EVIDENCE_CASE_LIMIT:
        raise ValueError(
            "hifiasm Methods evidence payload may contain at most "
            f"{HIFIASM_METHODS_EVIDENCE_CASE_LIMIT} cases"
        )
    if evidence_case_ids != retrieval_candidate_ids[: len(evidence_case_ids)]:
        raise ValueError("evidence_case_ids must be the leading numeric retrieval candidates")
    candidate_set = set(evidence_case_ids)

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_case in evidence_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("evidence_cases entries must be objects")
        case_id = _nonempty_string(raw_case.get("case_id"), field="case_id")
        if case_id not in candidate_set:
            raise ValueError(f"evidence case {case_id!r} is outside the numeric shortlist")
        if case_id in seen:
            raise ValueError(f"duplicate evidence case {case_id!r}")
        seen.add(case_id)
        if "source_documents" in raw_case:
            raise ValueError("full source_documents are not allowed in the evidence payload")

        case = {"case_id": case_id}
        for field in _EVIDENCE_CASE_FIELDS:
            case[field] = deepcopy(raw_case.get(field) or ([] if field in {"rationale_claims", "source_excerpts"} else {}))
        case["missing_fields"] = [
            field for field in _EVIDENCE_CASE_FIELDS if not raw_case.get(field)
        ]
        cases.append(case)

    ordered_cases = sorted(cases, key=lambda case: evidence_case_ids.index(case["case_id"]))
    if [case["case_id"] for case in ordered_cases] != evidence_case_ids:
        missing = [case_id for case_id in evidence_case_ids if case_id not in seen]
        raise ValueError(f"numeric shortlist cases missing from evidence payload: {missing}")

    return {
        "retrieval_backend": "hifiasm_numeric",
        "query_id": case_retrieval.get("query_id"),
        "candidate_case_ids": evidence_case_ids,
        "retrieval_candidate_case_ids": retrieval_candidate_ids,
        "warning": case_retrieval.get("warning"),
        "effect_areas": list(HIFIASM_EFFECT_AREAS),
        "cases": ordered_cases,
    }


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
        raise ValueError("hifiasm Methods-aware response requires hifiasm_numeric retrieval")

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
        else:
            omitted_parameters.append(parameter)

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
    elif supporting_case_ids or supporting_span_ids:
        raise ValueError(f"parameter {parameter!r} has case citations without basis=case")

    fact_paths = _string_list(raw_decision.get("fact_paths") or [], field=f"{parameter}.fact_paths")
    if "profile_fact" in bases:
        if not fact_paths:
            raise ValueError(f"parameter {parameter!r} basis=profile_fact requires fact_paths")
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
    if "skill_heuristic" in bases and not heuristic_labels:
        raise ValueError(f"parameter {parameter!r} basis=skill_heuristic requires heuristic_labels")

    retry_signals = _string_list(raw_decision.get("retry_signals") or [], field=f"{parameter}.retry_signals")
    if "retry_failure" in bases:
        if prompt_payload.get("task") == "recommend_initial":
            raise ValueError("retry_failure evidence is not valid for recommend_initial")
        if not retry_signals:
            raise ValueError(f"parameter {parameter!r} basis=retry_failure requires retry_signals")

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


def _lookup_path(values: Any, path: str) -> Any:
    current = values
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


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
