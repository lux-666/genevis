"""Minimal assembly parameter-patch contract.

Biological observations, mechanism guidance, and literature cases are inputs to
the model.  The model only has to return parameter values; provenance remains
in the prompt artifact and a human reviews every proposed patch.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


PARAMETER_RECOMMENDATION_CONTRACT = "parameter_patch_v2"
_ACCEPTED_CONTRACTS = {PARAMETER_RECOMMENDATION_CONTRACT, "literature_patch_v1"}
_RETRY_DISPOSITIONS = {"retry", "stop", "expert_review"}


def normalize_parameter_recommendation(
    response: dict[str, Any],
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate a sparse model patch and derive its effective parameter set."""

    if not isinstance(response, dict):
        raise ValueError("parameter recommendation response must be an object")
    if prompt_payload.get("recommendation_contract") not in _ACCEPTED_CONTRACTS:
        raise ValueError("prompt does not use a supported parameter-patch contract")

    adjustable = prompt_payload.get("adjustable_params") or {}
    if not isinstance(adjustable, dict):
        raise ValueError("prompt adjustable_params must be an object")
    raw_patch = _extract_raw_patch(response, adjustable=set(adjustable))
    unexpected = sorted(set(raw_patch) - set(adjustable))
    if unexpected:
        raise ValueError(f"parameter_patch contains non-L2 parameters: {unexpected}")

    parameter_patch: dict[str, Any] = {}
    parameter_reasoning: dict[str, dict[str, Any]] = {}
    for parameter, raw_recommendation in raw_patch.items():
        recommendation, reasoning = _normalize_patch_entry(parameter, raw_recommendation)
        parameter_patch[parameter] = recommendation
        parameter_reasoning[parameter] = reasoning

    summary = _recommendation_summary(response, parameter_count=len(parameter_patch))
    diagnosis = _normalize_diagnosis(response.get("sample_diagnosis"), fallback_summary=summary)

    baseline = prompt_payload.get("baseline_params") or {}
    if not isinstance(baseline, dict):
        raise ValueError("baseline_params must be an object")
    effective_params = {**deepcopy(baseline), **deepcopy(parameter_patch)}
    baseline_overrides = prompt_payload.get("baseline_overrides") or {}
    if not isinstance(baseline_overrides, dict):
        raise ValueError("baseline_overrides must be an object")
    execution_params = {
        **deepcopy(baseline_overrides),
        **deepcopy(parameter_patch),
    }

    retry_disposition = _normalize_retry_disposition(
        response.get("retry_disposition"),
        task=prompt_payload.get("task"),
        has_patch=bool(parameter_patch),
    )

    return {
        "sample_diagnosis": diagnosis,
        "parameter_patch": parameter_patch,
        "parameter_reasoning": parameter_reasoning,
        "baseline_params": deepcopy(baseline),
        "effective_params": effective_params,
        "execution_params": execution_params,
        "used_literature_case_ids": [],
        "retry_disposition": retry_disposition,
        "summary": summary,
        # Downstream execution consumes only the sparse patch.  The complete
        # effective snapshot remains available for replay and retry baselines.
        "params": parameter_patch,
        "reasoning": summary,
    }


def default_parameter_recommendation(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    """Return the conservative no-patch fallback for model unavailability."""

    task = prompt_payload.get("task")
    response: dict[str, Any] = {
        "sample_diagnosis": {
            "effect_areas": [],
            "summary": "Model unavailable; retain the deterministic baseline.",
        },
        "parameter_patch": {},
        "summary": "Model unavailable; retain the deterministic baseline.",
    }
    if task != "recommend_initial":
        response["retry_disposition"] = "expert_review"
    return normalize_parameter_recommendation(response, prompt_payload)


def _normalize_diagnosis(value: Any, *, fallback_summary: str) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    raw_effect_areas = source.get("effect_areas")
    effect_areas = (
        list(dict.fromkeys(item.strip() for item in raw_effect_areas if isinstance(item, str) and item.strip()))
        if isinstance(raw_effect_areas, list)
        else []
    )
    diagnosis_summary = source.get("summary")
    if not isinstance(diagnosis_summary, str) or not diagnosis_summary.strip():
        diagnosis_summary = fallback_summary
    return {
        "effect_areas": effect_areas,
        "summary": diagnosis_summary.strip(),
    }


def _normalize_patch_entry(parameter: str, raw: Any) -> tuple[Any, dict[str, Any]]:
    """Accept the v2 scalar value and the former rich entry during migration."""

    if not isinstance(raw, dict):
        return deepcopy(raw), {}
    if "value" not in raw:
        raise ValueError(
            f"parameter_patch.{parameter} must be a value or an object containing value"
        )

    reasoning: dict[str, Any] = {}
    for field in ("expected_effect", "confidence", "reason"):
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            reasoning[field] = value.strip()
    tradeoffs = raw.get("tradeoffs")
    if isinstance(tradeoffs, str) and tradeoffs.strip():
        reasoning["tradeoffs"] = [tradeoffs.strip()]
    elif isinstance(tradeoffs, list):
        normalized = [item.strip() for item in tradeoffs if isinstance(item, str) and item.strip()]
        if normalized:
            reasoning["tradeoffs"] = list(dict.fromkeys(normalized))
    return deepcopy(raw["value"]), reasoning


def _extract_raw_patch(response: dict[str, Any], *, adjustable: set[str]) -> dict[str, Any]:
    """Accept common JSON labels plus a direct top-level parameter mapping."""

    for field in ("parameter_patch", "params", "parameters", "recommended_params"):
        candidate = response.get(field)
        if isinstance(candidate, dict):
            return candidate
    return {key: value for key, value in response.items() if key in adjustable}


def _recommendation_summary(response: dict[str, Any], *, parameter_count: int) -> str:
    summary = response.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    diagnosis = response.get("sample_diagnosis")
    if isinstance(diagnosis, dict):
        diagnosis_summary = diagnosis.get("summary")
        if isinstance(diagnosis_summary, str) and diagnosis_summary.strip():
            return diagnosis_summary.strip()
    return f"LLM proposed {parameter_count} parameter change(s) for human review."


def _normalize_retry_disposition(value: Any, *, task: Any, has_patch: bool) -> str | None:
    if task == "recommend_initial":
        return None
    if value in _RETRY_DISPOSITIONS:
        if has_patch and value in {"stop", "expert_review"}:
            return "retry"
        return value
    return "retry" if has_patch else "expert_review"
