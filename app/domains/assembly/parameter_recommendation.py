"""Unified literature-guided parameter-patch contract.

The model receives each literature card once and returns only references to
visible observations, guidance records, cases, and quotes.  Initial attempts
patch catalog defaults; retries patch the previous attempt's effective values.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


PARAMETER_RECOMMENDATION_CONTRACT = "literature_patch_v1"
_CONFIDENCE_VALUES = {"high", "medium", "low"}
_RETRY_DISPOSITIONS = {"retry", "stop", "expert_review"}


def normalize_parameter_recommendation(
    response: dict[str, Any],
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate a sparse model patch and derive its effective parameter set."""

    if not isinstance(response, dict):
        raise ValueError("parameter recommendation response must be an object")
    if prompt_payload.get("recommendation_contract") != PARAMETER_RECOMMENDATION_CONTRACT:
        raise ValueError("prompt does not use the literature parameter-patch contract")

    diagnosis = _validate_diagnosis(response.get("sample_diagnosis"))
    summary = _nonempty_string(response.get("summary"), field="summary")
    raw_patch = response.get("parameter_patch")
    if not isinstance(raw_patch, dict):
        raise ValueError("parameter_patch must be an object")

    adjustable = prompt_payload.get("adjustable_params") or {}
    if not isinstance(adjustable, dict):
        raise ValueError("prompt adjustable_params must be an object")
    unexpected = sorted(set(raw_patch) - set(adjustable))
    if unexpected:
        raise ValueError(f"parameter_patch contains non-L2 parameters: {unexpected}")

    visible_observations = {
        str(item.get("id"))
        for item in prompt_payload.get("observations") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    guidance_by_id = {
        str(item.get("guidance_id")): item
        for item in prompt_payload.get("method_guidance") or []
        if isinstance(item, Mapping) and item.get("guidance_id")
    }
    case_quotes = _literature_case_quotes(prompt_payload.get("literature_cases") or [])

    parameter_patch: dict[str, Any] = {}
    parameter_reasoning: dict[str, dict[str, Any]] = {}
    used_case_ids: list[str] = []
    for parameter, raw_recommendation in raw_patch.items():
        recommendation, case_ids = _validate_patch_entry(
            parameter,
            raw_recommendation,
            visible_observations=visible_observations,
            guidance_by_id=guidance_by_id,
            case_quotes=case_quotes,
        )
        parameter_patch[parameter] = deepcopy(recommendation["value"])
        parameter_reasoning[parameter] = recommendation
        for case_id in case_ids:
            if case_id not in used_case_ids:
                used_case_ids.append(case_id)

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

    task = prompt_payload.get("task")
    retry_disposition = response.get("retry_disposition")
    if task == "recommend_initial":
        if retry_disposition is not None:
            raise ValueError("initial recommendation cannot set retry_disposition")
    elif retry_disposition not in _RETRY_DISPOSITIONS:
        raise ValueError(
            "retry recommendation requires retry_disposition=retry, stop, or expert_review"
        )
    if retry_disposition in {"stop", "expert_review"} and parameter_patch:
        raise ValueError("stop/expert_review cannot include a parameter patch")

    return {
        "sample_diagnosis": diagnosis,
        "parameter_patch": parameter_patch,
        "parameter_reasoning": parameter_reasoning,
        "baseline_params": deepcopy(baseline),
        "effective_params": effective_params,
        "execution_params": execution_params,
        "used_literature_case_ids": used_case_ids,
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


def _validate_diagnosis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("sample_diagnosis must be an object")
    effect_areas = value.get("effect_areas") or []
    if not isinstance(effect_areas, list) or not all(
        isinstance(item, str) and item.strip() for item in effect_areas
    ):
        raise ValueError("sample_diagnosis.effect_areas must be a list of strings")
    return {
        "effect_areas": list(dict.fromkeys(effect_areas)),
        "summary": _nonempty_string(
            value.get("summary"),
            field="sample_diagnosis.summary",
        ),
    }


def _validate_patch_entry(
    parameter: str,
    value: Any,
    *,
    visible_observations: set[str],
    guidance_by_id: dict[str, Mapping[str, Any]],
    case_quotes: dict[str, set[str]],
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(value, dict):
        raise ValueError(f"parameter_patch.{parameter} must be an object")
    if "value" not in value:
        raise ValueError(f"parameter_patch.{parameter}.value is required")

    observation_ids = _string_list(value.get("observation_ids") or [], f"{parameter}.observation_ids")
    missing_observations = sorted(set(observation_ids) - visible_observations)
    if missing_observations:
        raise ValueError(
            f"parameter {parameter!r} cites observations absent from the prompt: "
            f"{missing_observations}"
        )

    guidance_ids = _string_list(value.get("guidance_ids") or [], f"{parameter}.guidance_ids")
    missing_guidance = sorted(set(guidance_ids) - set(guidance_by_id))
    if missing_guidance:
        raise ValueError(
            f"parameter {parameter!r} cites guidance absent from the prompt: {missing_guidance}"
        )
    incompatible_guidance = [
        guidance_id
        for guidance_id in guidance_ids
        if parameter not in set(guidance_by_id[guidance_id].get("parameters") or [])
    ]
    if incompatible_guidance:
        raise ValueError(
            f"parameter {parameter!r} cites guidance for other parameters: "
            f"{incompatible_guidance}"
        )

    anchors = value.get("literature_anchors") or []
    if not isinstance(anchors, list):
        raise ValueError(f"{parameter}.literature_anchors must be a list")
    normalized_anchors: list[dict[str, Any]] = []
    used_case_ids: list[str] = []
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError(f"{parameter}.literature_anchors entries must be objects")
        case_id = _nonempty_string(anchor.get("case_id"), field=f"{parameter}.case_id")
        if case_id not in case_quotes:
            raise ValueError(f"parameter {parameter!r} cites a literature case absent from the prompt")
        quote_ids = _string_list(anchor.get("quote_ids") or [], f"{parameter}.quote_ids")
        invalid_quotes = sorted(set(quote_ids) - case_quotes[case_id])
        if invalid_quotes:
            raise ValueError(
                f"parameter {parameter!r} cites quotes absent from {case_id!r}: {invalid_quotes}"
            )
        normalized_anchors.append({"case_id": case_id, "quote_ids": quote_ids})
        if case_id not in used_case_ids:
            used_case_ids.append(case_id)

    tradeoffs = _string_list(value.get("tradeoffs") or [], f"{parameter}.tradeoffs")
    confidence = value.get("confidence")
    if confidence not in _CONFIDENCE_VALUES:
        raise ValueError(f"{parameter}.confidence must be high, medium, or low")
    return (
        {
            "value": deepcopy(value["value"]),
            "observation_ids": observation_ids,
            "guidance_ids": guidance_ids,
            "literature_anchors": normalized_anchors,
            "expected_effect": _nonempty_string(
                value.get("expected_effect"),
                field=f"{parameter}.expected_effect",
            ),
            "tradeoffs": tradeoffs,
            "confidence": confidence,
            "reason": _nonempty_string(value.get("reason"), field=f"{parameter}.reason"),
        },
        used_case_ids,
    )


def _literature_case_quotes(cases: Any) -> dict[str, set[str]]:
    if not isinstance(cases, list):
        raise ValueError("literature_cases must be a list")
    indexed: dict[str, set[str]] = {}
    for case in cases:
        if not isinstance(case, Mapping) or not case.get("case_id"):
            raise ValueError("literature_cases entries require case_id")
        case_id = str(case["case_id"])
        quote_ids: set[str] = set()
        for field in ("source_excerpts", "evidence_spans"):
            for quote in case.get(field) or []:
                if not isinstance(quote, Mapping):
                    continue
                quote_id = quote.get("span_id") or quote.get("quote_id")
                if quote_id:
                    quote_ids.add(str(quote_id))
        indexed[case_id] = quote_ids
    return indexed


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return list(dict.fromkeys(value))


def _nonempty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()
