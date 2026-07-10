"""Validate L2 assembly parameter recommendations before command freezing."""

from __future__ import annotations

from typing import Any

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
    """Validate optional per-parameter evidence bases against the visible prompt."""

    violations: list[dict[str, Any]] = []
    selected_ids = set((prompt_payload.get("case_selection") or {}).get("selected_case_ids") or [])
    uses_hifiasm_evidence = (
        (prompt_payload.get("case_selection") or {}).get("retrieval_backend")
        == "hifiasm_numeric"
    )
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
        return {
            "valid": not violations,
            "violations": violations,
            "warnings": [{"reason": "parameter_evidence not provided"}],
        }
    if not isinstance(parameter_evidence, dict):
        violations.append(
            {"param": None, "reason": "parameter_evidence must be an object", "severity": "critical"}
        )
        return {"valid": False, "violations": violations, "warnings": []}

    spans_by_case = {
        str(case.get("case_id")): {
            str(span.get("span_id"))
            for span in case.get("source_excerpts") or []
            if isinstance(span, dict) and span.get("span_id")
        }
        for case in prompt_payload.get("evidence_cases") or []
        if isinstance(case, dict)
    }

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
            case_ids = evidence.get("case_ids") or []
            span_ids = evidence.get("span_ids") or []
            if (
                not isinstance(case_ids, list)
                or not case_ids
                or not all(isinstance(case_id, str) for case_id in case_ids)
                or not isinstance(span_ids, list)
                or not span_ids
                or not all(isinstance(span_id, str) for span_id in span_ids)
            ):
                violations.append(
                    {
                        "param": parameter,
                        "reason": "basis=case requires non-empty case_ids and span_ids",
                        "severity": "critical",
                    }
                )
            else:
                invalid_cases = sorted(set(case_ids) - selected_ids)
                if invalid_cases:
                    violations.append(
                        {
                            "param": parameter,
                            "reason": f"case evidence cites non-shortlisted cases: {invalid_cases}",
                            "severity": "critical",
                        }
                    )
                visible_spans = set().union(*(spans_by_case.get(str(case_id), set()) for case_id in case_ids))
                invalid_spans = sorted(set(span_ids) - visible_spans)
                if invalid_spans:
                    violations.append(
                        {
                            "param": parameter,
                            "reason": f"case evidence cites spans absent from the prompt: {invalid_spans}",
                            "severity": "critical",
                        }
                    )
        if "profile_fact" in bases:
            fact_paths = evidence.get("fact_paths") or []
            if not isinstance(fact_paths, list) or not fact_paths:
                violations.append(
                    {
                        "param": parameter,
                        "reason": "basis=profile_fact requires fact_paths",
                        "severity": "critical",
                    }
                )
            else:
                missing_paths = [
                    path
                    for path in fact_paths
                    if not isinstance(path, str) or _lookup_path(prompt_payload.get("prompt_profile") or {}, path) is None
                ]
                if missing_paths:
                    violations.append(
                        {
                            "param": parameter,
                            "reason": f"profile_fact paths are absent from the prompt: {missing_paths}",
                            "severity": "critical",
                        }
                    )
        if "retry_failure" in bases and prompt_payload.get("task") == "recommend_initial":
            violations.append(
                {
                    "param": parameter,
                    "reason": "retry_failure evidence is not valid for recommend_initial",
                    "severity": "critical",
                }
            )

    return {"valid": not violations, "violations": violations, "warnings": []}


def _lookup_path(values: Any, path: str) -> Any:
    current = values
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current
