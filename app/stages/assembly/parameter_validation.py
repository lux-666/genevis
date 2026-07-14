"""Validate L2 assembly parameter recommendations before command freezing."""

from __future__ import annotations

from typing import Any

from app.domains.assembly.methods_evidence import allowed_parameter_evidence_span_ids
from app.domains.assembly.profile_evidence import (
    missing_profile_evidence_ids,
    profile_evidence_ids_requiring_retry_basis,
)
from app.schemas.parameter_catalog import MethodParameterCatalog, ParameterSpec
from app.stages.assembly.parameter_profile import flatten_profile_facts


def validate_recommended_parameters(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    recommended_params: dict[str, Any],
    readonly_facts: dict[str, Any],
    rules: dict[str, Any],
    full_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable validation result for L2 recommendations."""

    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    rule_facts = dict(readonly_facts)
    if full_profile is not None:
        rule_facts = {**flatten_profile_facts(full_profile), **rule_facts}
    if not isinstance(recommended_params, dict):
        return {
            "valid": False,
            "method_id": method_id,
            "violations": [
                {
                    "param": None,
                    "value": recommended_params,
                    "reason": "recommended_params must be a JSON object",
                    "severity": "critical",
                }
            ],
            "warnings": warnings,
        }

    for key, value in recommended_params.items():
        spec = catalog.parameters.get(key)
        if spec is None:
            violations.append(
                {
                    "param": key,
                    "value": value,
                    "reason": "unknown parameter",
                    "severity": "critical",
                }
            )
            continue
        if spec.level != 2:
            violations.append(
                {
                    "param": key,
                    "value": value,
                    "reason": f"LLM output may contain only L2 parameters; {key!r} is L{spec.level}",
                    "level": spec.level,
                    "severity": "critical",
                }
            )
            continue
        violations.extend(_catalog_violations(key, value, spec))

    if not violations:
        for rule in _iter_rules(rules):
            if _condition_matches(rule.get("condition") or {}, rule_facts):
                violations.extend(_rule_violations(rule, recommended_params))

    return {
        "valid": not violations,
        "method_id": method_id,
        "violations": violations,
        "warnings": warnings,
    }


def _catalog_violations(param: str, value: Any, spec: ParameterSpec) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    type_reason = _type_reason(value, spec.value_type)
    if type_reason is not None:
        violations.append(
            {
                "param": param,
                "value": value,
                "reason": type_reason,
                "severity": "critical",
            }
        )
        return violations

    if spec.allowed_values is not None and value not in spec.allowed_values:
        violations.append(
            {
                "param": param,
                "value": value,
                "allowed": spec.allowed_values,
                "reason": "value not in allowed_values",
                "severity": "critical",
            }
        )
    if spec.min is not None and _is_number(value) and value < spec.min:
        violations.append(
            {
                "param": param,
                "value": value,
                "min": spec.min,
                "reason": "value below catalog minimum",
                "severity": "critical",
            }
        )
    if spec.max is not None and _is_number(value) and value > spec.max:
        violations.append(
            {
                "param": param,
                "value": value,
                "max": spec.max,
                "reason": "value above catalog maximum",
                "severity": "critical",
            }
        )
    return violations


def _type_reason(value: Any, value_type: str) -> str | None:
    if value_type == "int":
        return None if type(value) is int else "value must be an integer"
    if value_type == "float":
        return None if _is_number(value) else "value must be numeric"
    if value_type in {"string", "path"}:
        return None if isinstance(value, str) else "value must be a string"
    if value_type == "boolean":
        return None if isinstance(value, bool) else "value must be boolean"
    if value_type == "path_list":
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return None
        return "value must be a list of strings"
    return f"unsupported value_type: {value_type}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _iter_rules(rules: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rules = rules.get("rules", [])
    return raw_rules if isinstance(raw_rules, list) else []


def _condition_matches(condition: dict[str, Any], readonly_facts: dict[str, Any]) -> bool:
    for fact_key, expected in condition.items():
        if fact_key not in readonly_facts:
            return False
        actual = readonly_facts[fact_key]
        if isinstance(expected, dict):
            if "min" in expected and (not _is_number(actual) or actual < expected["min"]):
                return False
            if "max" in expected and (not _is_number(actual) or actual > expected["max"]):
                return False
        elif actual != expected:
            return False
    return True


def _rule_violations(rule: dict[str, Any], recommended_params: dict[str, Any]) -> list[dict[str, Any]]:
    constraints = rule.get("constraints") or {}
    if not isinstance(constraints, dict):
        return []
    violations: list[dict[str, Any]] = []
    for param, constraint in constraints.items():
        if param not in recommended_params:
            continue
        value = recommended_params[param]
        if isinstance(constraint, list):
            if value not in constraint:
                violations.append(_rule_violation(rule, param, value, allowed=constraint))
        elif isinstance(constraint, dict):
            if "allowed" in constraint and value not in constraint["allowed"]:
                violations.append(_rule_violation(rule, param, value, allowed=constraint["allowed"]))
            if "min" in constraint and (not _is_number(value) or value < constraint["min"]):
                violations.append(_rule_violation(rule, param, value, min=constraint["min"]))
            if "max" in constraint and (not _is_number(value) or value > constraint["max"]):
                violations.append(_rule_violation(rule, param, value, max=constraint["max"]))
    return violations


def _rule_violation(rule: dict[str, Any], param: str, value: Any, **extra: Any) -> dict[str, Any]:
    return {
        "param": param,
        "value": value,
        "rule_id": rule.get("rule_id"),
        "reason": rule.get("reason", "rule constraint violation"),
        "severity": "critical",
        **extra,
    }


_ALLOWED_EVIDENCE_BASES = {
    "case",
    "profile_fact",
    "skill_heuristic",
    "retry_failure",
    "catalog_default",
}


def validate_parameter_evidence(
    *,
    parameter_evidence: Any,
    recommended_params: dict[str, Any],
    prompt_payload: dict[str, Any],
    declared_selected_case_ids: Any = None,
) -> dict[str, Any]:
    """Validate per-parameter evidence bases against the visible prompt.

    Evidence remains optional for legacy recommendation contracts, but Task 4's
    hifiasm Methods-aware contract requires it for every rendered L2 value.

    ``basis=case`` is surface-driven, not method-named:
    - every cited case_id must be on the prompt shortlist / selected cases
    - if a cited case exposes Methods spans in the prompt, at least one of those
      spans must be cited and must be curated support for the parameter
    - if a cited case has no span surface, case_id binding alone is enough
    """

    violations: list[dict[str, Any]] = []
    case_selection = (
        prompt_payload.get("case_selection")
        or prompt_payload.get("case_retrieval")
        or {}
    )
    selected_ids = set(
        case_selection.get("evidence_case_ids")
        or case_selection.get("candidate_case_ids")
        or case_selection.get("selected_case_ids")
        or []
    )
    # Cases actually present in the prompt are always citable by id.
    for case_list_key in ("selected_cases", "evidence_cases"):
        for case in prompt_payload.get(case_list_key) or []:
            if isinstance(case, dict) and case.get("case_id"):
                selected_ids.add(str(case["case_id"]))

    uses_hifiasm_evidence = case_selection.get("retrieval_backend") in {
        "hifiasm_numeric",
    }
    declared_selected_ids: set[str] | None = None
    if uses_hifiasm_evidence and declared_selected_case_ids is not None:
        if not isinstance(declared_selected_case_ids, list) or not all(
            isinstance(case_id, str) for case_id in declared_selected_case_ids
        ):
            violations.append(
                {
                    "param": None,
                    "reason": "selected_case_ids must be a list of strings",
                    "severity": "critical",
                }
            )
        else:
            declared_selected_ids = set(declared_selected_case_ids)
            unsupported_selected = sorted(set(declared_selected_case_ids) - selected_ids)
            if unsupported_selected:
                violations.append(
                    {
                        "param": None,
                        "reason": f"selected_case_ids contains non-shortlisted cases: {unsupported_selected}",
                        "severity": "critical",
                    }
                )
    if parameter_evidence is None:
        if uses_hifiasm_evidence and recommended_params:
            violations.extend(
                {
                    "param": parameter,
                    "reason": "every recommended hifiasm parameter requires an evidence basis",
                    "severity": "critical",
                }
                for parameter in sorted(recommended_params)
            )
        return {
            "valid": not violations,
            "violations": violations,
            "warnings": [] if violations else [{"reason": "parameter_evidence not provided"}],
        }
    if not isinstance(parameter_evidence, dict):
        violations.append(
            {"param": None, "reason": "parameter_evidence must be an object", "severity": "critical"}
        )
        return {"valid": False, "violations": violations, "warnings": []}

    if uses_hifiasm_evidence:
        missing_evidence = sorted(set(recommended_params) - set(parameter_evidence))
        violations.extend(
            {
                "param": parameter,
                "reason": "every recommended hifiasm parameter requires an evidence basis",
                "severity": "critical",
            }
            for parameter in missing_evidence
        )

    cases_by_id, spans_by_case = _case_surfaces(prompt_payload)

    for parameter, evidence in parameter_evidence.items():
        if parameter not in recommended_params:
            violations.append(
                {
                    "param": parameter,
                    "reason": "parameter_evidence key is not present in params",
                    "severity": "critical",
                }
            )
            continue
        if not isinstance(evidence, dict):
            violations.append(
                {"param": parameter, "reason": "parameter evidence must be an object", "severity": "critical"}
            )
            continue
        raw_bases = evidence.get("bases", evidence.get("basis"))
        bases = [raw_bases] if isinstance(raw_bases, str) else raw_bases
        if not isinstance(bases, list) or not bases:
            violations.append(
                {"param": parameter, "reason": "at least one evidence basis is required", "severity": "critical"}
            )
            continue
        if not all(isinstance(basis, str) for basis in bases):
            violations.append(
                {"param": parameter, "reason": "evidence bases must be strings", "severity": "critical"}
            )
            continue
        unknown_bases = sorted(set(bases) - _ALLOWED_EVIDENCE_BASES)
        if unknown_bases:
            violations.append(
                {
                    "param": parameter,
                    "reason": f"unknown evidence bases: {unknown_bases}",
                    "severity": "critical",
                }
            )
        if "case" in bases:
            violations.extend(
                _case_basis_violations(
                    parameter=parameter,
                    evidence=evidence,
                    allowed_case_ids=(
                        declared_selected_ids
                        if declared_selected_ids is not None
                        else selected_ids
                    ),
                    cases_by_id=cases_by_id,
                    spans_by_case=spans_by_case,
                )
            )
        if "fact_paths" in evidence:
            violations.append(
                {
                    "param": parameter,
                    "reason": "fact_paths is no longer supported; use evidence_ids",
                    "severity": "critical",
                }
            )
        evidence_ids = evidence.get("evidence_ids") or []
        if "profile_fact" in bases:
            if (
                not isinstance(evidence_ids, list)
                or not evidence_ids
                or not all(isinstance(evidence_id, str) for evidence_id in evidence_ids)
            ):
                violations.append(
                    {
                        "param": parameter,
                        "reason": "basis=profile_fact requires non-empty evidence_ids",
                        "severity": "critical",
                    }
                )
            else:
                retry_evidence_ids = profile_evidence_ids_requiring_retry_basis(
                    prompt_payload, evidence_ids
                )
                if retry_evidence_ids:
                    violations.append(
                        {
                            "param": parameter,
                            "reason": (
                                "retry failure facts require basis=retry_failure: "
                                f"{retry_evidence_ids}"
                            ),
                            "severity": "critical",
                        }
                    )
                missing_ids = missing_profile_evidence_ids(prompt_payload, evidence_ids)
                if missing_ids:
                    violations.append(
                        {
                            "param": parameter,
                            "reason": (
                                "profile_fact evidence IDs are absent from observations: "
                                f"{missing_ids}"
                            ),
                            "severity": "critical",
                        }
                    )
        elif evidence_ids:
            violations.append(
                {
                    "param": parameter,
                    "reason": "evidence_ids require basis=profile_fact",
                    "severity": "critical",
                }
            )
        retry_signals = evidence.get("retry_signals") or []
        retry_evidence = prompt_payload.get("retry_evidence") or {}
        visible_retry_signals = {
            item
            for field in ("signal_ids", "signal_paths")
            for item in (retry_evidence.get(field) or [])
            if isinstance(item, str) and item.strip()
        }
        for signal in retry_evidence.get("signals") or []:
            if not isinstance(signal, dict):
                continue
            visible_retry_signals.update(
                value
                for field in ("signal_id", "path")
                if isinstance((value := signal.get(field)), str) and value.strip()
            )
        if "retry_failure" in bases:
            if prompt_payload.get("task") == "recommend_initial":
                violations.append(
                    {
                        "param": parameter,
                        "reason": "retry_failure evidence is not valid for recommend_initial",
                        "severity": "critical",
                    }
                )
            if not isinstance(retry_signals, list) or not retry_signals:
                violations.append(
                    {
                        "param": parameter,
                        "reason": "basis=retry_failure requires retry_signals",
                        "severity": "critical",
                    }
                )
            else:
                invalid_retry_signals = [
                    signal
                    for signal in retry_signals
                    if not isinstance(signal, str) or signal not in visible_retry_signals
                ]
                if invalid_retry_signals:
                    violations.append(
                        {
                            "param": parameter,
                            "reason": (
                                "retry_failure cites signals absent from structured prompt evidence: "
                                f"{invalid_retry_signals}"
                            ),
                            "severity": "critical",
                        }
                    )
        elif retry_signals:
            violations.append(
                {
                    "param": parameter,
                    "reason": "retry_signals require basis=retry_failure",
                    "severity": "critical",
                }
            )

    return {"valid": not violations, "violations": violations, "warnings": []}


def _case_surfaces(
    prompt_payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    """Index prompt cases and the Methods spans each case exposes."""

    cases_by_id: dict[str, dict[str, Any]] = {}
    spans_by_case: dict[str, set[str]] = {}
    # Prefer evidence_cases for span surfaces; still index selected_cases by id.
    for case_list_key in ("selected_cases", "evidence_cases"):
        for case in prompt_payload.get(case_list_key) or []:
            if not isinstance(case, dict) or not case.get("case_id"):
                continue
            case_id = str(case["case_id"])
            cases_by_id[case_id] = case
            spans = {
                str(span.get("span_id"))
                for span in case.get("source_excerpts") or []
                if isinstance(span, dict) and span.get("span_id")
            }
            if spans:
                spans_by_case[case_id] = spans
            elif case_id not in spans_by_case:
                spans_by_case[case_id] = set()
    return cases_by_id, spans_by_case


def _case_basis_violations(
    *,
    parameter: str,
    evidence: dict[str, Any],
    allowed_case_ids: set[str],
    cases_by_id: dict[str, dict[str, Any]],
    spans_by_case: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """Validate one parameter's case citations against visible prompt surfaces."""

    violations: list[dict[str, Any]] = []
    case_ids = evidence.get("case_ids") or []
    span_ids = evidence.get("span_ids") or []

    if (
        not isinstance(case_ids, list)
        or not case_ids
        or not all(isinstance(case_id, str) for case_id in case_ids)
    ):
        return [
            {
                "param": parameter,
                "reason": "basis=case requires non-empty case_ids",
                "severity": "critical",
            }
        ]
    if not isinstance(span_ids, list) or not all(isinstance(span_id, str) for span_id in span_ids):
        return [
            {
                "param": parameter,
                "reason": "span_ids must be a list of strings when provided",
                "severity": "critical",
            }
        ]

    invalid_cases = sorted(set(case_ids) - allowed_case_ids)
    if invalid_cases:
        violations.append(
            {
                "param": parameter,
                "reason": f"case evidence cites non-shortlisted cases: {invalid_cases}",
                "severity": "critical",
            }
        )

    # Span rules apply per cited case: only cases that expose spans require them.
    cases_with_span_surface = [
        case_id for case_id in case_ids if spans_by_case.get(str(case_id))
    ]
    if cases_with_span_surface:
        if not span_ids:
            violations.append(
                {
                    "param": parameter,
                    "reason": "basis=case requires non-empty span_ids when cited cases expose Methods spans",
                    "severity": "critical",
                }
            )
            return violations

        visible_spans = set().union(
            *(spans_by_case.get(str(case_id), set()) for case_id in case_ids)
        )
        invalid_spans = sorted(set(span_ids) - visible_spans)
        if invalid_spans:
            violations.append(
                {
                    "param": parameter,
                    "reason": f"case evidence cites spans absent from the prompt: {invalid_spans}",
                    "severity": "critical",
                }
            )
        cases_without_cited_span = sorted(
            case_id
            for case_id in cases_with_span_surface
            if not (spans_by_case.get(str(case_id), set()) & set(span_ids))
        )
        if cases_without_cited_span:
            violations.append(
                {
                    "param": parameter,
                    "reason": (
                        "case evidence cites cases without a span from that case: "
                        f"{cases_without_cited_span}"
                    ),
                    "severity": "critical",
                }
            )
        unsupported_spans_by_case = {
            str(case_id): sorted(
                (spans_by_case.get(str(case_id), set()) & set(span_ids))
                - allowed_parameter_evidence_span_ids(
                    cases_by_id.get(str(case_id), {}),
                    parameter,
                )
            )
            for case_id in cases_with_span_surface
        }
        unsupported_spans_by_case = {
            case_id: unsupported
            for case_id, unsupported in unsupported_spans_by_case.items()
            if unsupported
        }
        if unsupported_spans_by_case:
            violations.append(
                {
                    "param": parameter,
                    "reason": (
                        "case evidence cites visible spans that are not curated support "
                        "for that parameter or its effect area: "
                        f"{unsupported_spans_by_case}"
                    ),
                    "severity": "critical",
                }
            )
    elif span_ids:
        # No cited case exposes spans; any span_id is floating and invalid.
        violations.append(
            {
                "param": parameter,
                "reason": f"case evidence cites spans absent from the prompt: {sorted(set(span_ids))}",
                "severity": "critical",
            }
        )

    return violations
