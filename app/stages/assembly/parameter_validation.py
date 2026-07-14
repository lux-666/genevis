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
