"""Method-specific prompt rendering for assembly parameter recommendations."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from app.llm.prompts import load_prompt_bundle, render_prompt_messages


_BUNDLE_BY_METHOD = {
    "flye": "assembly.parameter_flye",
    "nextdenovo": "assembly.parameter_nextdenovo",
    "hifiasm": "assembly.parameter_hifiasm",
    "hifiasm_ont": "assembly.parameter_hifiasm_ont",
}


def render_assembly_parameter_messages(
    prompt_payload: Mapping[str, Any],
    *,
    prompt_contract: str,
) -> list[dict[str, str]]:
    """Render a method-specific positive example followed by the live payload."""

    method_id = _normalized_method_id(prompt_payload.get("method_id"))
    try:
        bundle_name = _BUNDLE_BY_METHOD[method_id]
        example_builder = _EXAMPLE_BY_METHOD[method_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported assembly parameter prompt method: {method_id!r}") from exc

    bundle = load_prompt_bundle(bundle_name)
    prompt_payload_json = json.dumps(prompt_payload, ensure_ascii=False, indent=2)
    rendered = render_prompt_messages(
        bundle,
        {
            "method_id": method_id,
            "prompt_payload_json": prompt_payload_json,
        },
    )
    example = example_builder(prompt_payload, prompt_contract)
    return [
        rendered[0],
        {
            "role": "user",
            "content": f"Representative {method_id} response for the supplied contract:",
        },
        {
            "role": "assistant",
            "content": json.dumps(example, ensure_ascii=False, indent=2),
        },
        rendered[1],
    ]


def parameter_prompt_bundle_name(method_id: Any) -> str:
    """Return the app/llm bundle selected for an assembly method."""

    normalized = _normalized_method_id(method_id)
    try:
        return _BUNDLE_BY_METHOD[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported assembly parameter prompt method: {normalized!r}") from exc


def _normalized_method_id(method_id: Any) -> str:
    value = str(method_id or "").strip()
    return value.rsplit(".", 1)[-1]


def _legacy_response(
    prompt_payload: Mapping[str, Any],
    *,
    preferred_parameters: tuple[str, ...],
) -> dict[str, Any]:
    adjustable = prompt_payload.get("adjustable_params") or {}
    if not isinstance(adjustable, Mapping):
        adjustable = {}

    selected: list[tuple[str, Mapping[str, Any]]] = []
    for parameter in preferred_parameters:
        spec = adjustable.get(parameter)
        if isinstance(spec, Mapping):
            selected.append((parameter, spec))
    if not selected:
        selected = [
            (str(parameter), spec)
            for parameter, spec in adjustable.items()
            if isinstance(spec, Mapping) and spec.get("default") is not None
        ][:2]

    params: dict[str, Any] = {}
    parameter_evidence: dict[str, dict[str, Any]] = {}
    profile_fact_path = _first_profile_fact_path(prompt_payload.get("prompt_profile") or {})
    retry_evidence = prompt_payload.get("retry_evidence") or {}
    retry_signals = [
        str(item)
        for item in (retry_evidence.get("signal_ids") or retry_evidence.get("signal_paths") or [])
        if isinstance(item, str) and item.strip()
    ]
    for parameter, spec in selected:
        value = _representative_value(parameter, spec)
        if value is None:
            continue
        params[parameter] = value
        if spec.get("default") is not None and value == spec.get("default"):
            parameter_evidence[parameter] = {
                "bases": ["catalog_default"],
                "case_ids": [],
                "span_ids": [],
                "fact_paths": [],
                "heuristic_labels": [],
                "retry_signals": [],
            }
        elif prompt_payload.get("task") != "recommend_initial" and retry_signals:
            parameter_evidence[parameter] = {
                "bases": ["retry_failure"],
                "case_ids": [],
                "span_ids": [],
                "fact_paths": [],
                "heuristic_labels": [],
                "retry_signals": retry_signals,
            }
        elif profile_fact_path:
            parameter_evidence[parameter] = {
                "bases": ["profile_fact"],
                "case_ids": [],
                "span_ids": [],
                "fact_paths": [profile_fact_path],
                "heuristic_labels": [],
                "retry_signals": [],
            }

    return {
        "params": params,
        "reasoning": "Use a small, evidence-linked adjustment set for this method.",
        "selected_case_ids": [],
        "omitted_parameters": sorted(set(adjustable) - set(params)),
        "uncertainty_notes": [],
        "parameter_evidence": parameter_evidence,
    }


def _flye_example(prompt_payload: Mapping[str, Any], _contract: str) -> dict[str, Any]:
    return _legacy_response(prompt_payload, preferred_parameters=("min_overlap", "asm_coverage"))


def _nextdenovo_example(prompt_payload: Mapping[str, Any], _contract: str) -> dict[str, Any]:
    return _legacy_response(prompt_payload, preferred_parameters=("read_cutoff", "nextgraph_a"))


def _hifiasm_ont_example(prompt_payload: Mapping[str, Any], _contract: str) -> dict[str, Any]:
    return _legacy_response(prompt_payload, preferred_parameters=("purge_level", "kmer_length"))


def _hifiasm_example(prompt_payload: Mapping[str, Any], contract: str) -> dict[str, Any]:
    if contract != "hifiasm_methods_v1":
        return _legacy_response(prompt_payload, preferred_parameters=("purge_level", "kmer_length"))

    adjustable = prompt_payload.get("adjustable_params") or {}
    if not isinstance(adjustable, Mapping):
        adjustable = {}
    task = str(prompt_payload.get("task") or "recommend_initial")
    is_retry = task != "recommend_initial"
    previous_params = (
        ((prompt_payload.get("prompt_profile") or {}).get("previous_attempt") or {}).get("params")
        or {}
    )
    retry_evidence = prompt_payload.get("retry_evidence") or {}
    retry_signals = [
        str(item)
        for item in (retry_evidence.get("signal_ids") or retry_evidence.get("signal_paths") or [])
        if isinstance(item, str) and item.strip()
    ]

    decisions: dict[str, dict[str, Any]] = {}
    for parameter, raw_spec in adjustable.items():
        spec = raw_spec if isinstance(raw_spec, Mapping) else {}
        if is_retry and parameter in previous_params:
            basis = "retry_failure" if retry_signals else "catalog_default"
            decision = {
                "action": "keep_previous",
                "value": previous_params[parameter],
                "basis": basis,
                "supporting_case_ids": [],
                "supporting_span_ids": [],
                "fact_paths": [],
                "heuristic_labels": [],
                "retry_signals": retry_signals if basis == "retry_failure" else [],
                "reason": "Preserve the approved value while evaluating the current retry signal.",
            }
        elif spec.get("default") is not None:
            decision = {
                "action": "keep_default",
                "value": spec.get("default"),
                "basis": "catalog_default",
                "supporting_case_ids": [],
                "supporting_span_ids": [],
                "fact_paths": [],
                "heuristic_labels": [],
                "retry_signals": [],
                "reason": "Use the catalog default when the current evidence supports a conservative choice.",
            }
        else:
            decision = {
                "action": "omit",
                "basis": "catalog_default",
                "supporting_case_ids": [],
                "supporting_span_ids": [],
                "fact_paths": [],
                "heuristic_labels": [],
                "retry_signals": [],
                "reason": "Leave the optional setting unset when the current evidence does not justify a value.",
            }
        decisions[str(parameter)] = decision

    recommendation: dict[str, Any] = {"adjustable_params": decisions}
    if task == "retry_after_execution_failure":
        recommendation["retry_disposition"] = "retry"
    elif task == "retry_after_review_failure":
        recommendation["retry_disposition"] = "expert_review"

    return {
        "case_assessments": [],
        "recommendation": recommendation,
        "selected_case_ids": [],
        "summary": "Use catalog defaults, preserve approved retry values, and omit unsupported optional settings.",
    }


def _representative_value(parameter: str, spec: Mapping[str, Any]) -> Any:
    if spec.get("default") is not None:
        return spec.get("default")
    if parameter == "min_overlap":
        return 5000
    if parameter == "asm_coverage":
        return 40
    allowed = spec.get("allowed_values")
    if isinstance(allowed, list) and allowed:
        return allowed[0]
    if spec.get("min") is not None:
        return spec.get("min")
    return None


def _first_profile_fact_path(profile: Any, prefix: str = "") -> str | None:
    if not isinstance(profile, Mapping):
        return prefix or None
    for key in sorted(profile):
        path = f"{prefix}.{key}" if prefix else str(key)
        value = profile[key]
        if isinstance(value, Mapping):
            nested = _first_profile_fact_path(value, path)
            if nested:
                return nested
        elif value is not None and not _is_retry_failure_path(path):
            return path
    return None


def _is_retry_failure_path(path: str) -> bool:
    retry_roots = (
        "assembly_review",
        "previous_attempt.failure_class",
        "previous_attempt.execution_status",
        "previous_attempt.execution_status_detail",
        "previous_attempt.exit_code",
        "previous_attempt.stderr_ref",
        "previous_attempt.stdout_ref",
        "previous_attempt.resource_facts",
    )
    return any(path == root or path.startswith(f"{root}.") for root in retry_roots)


_EXAMPLE_BY_METHOD: dict[
    str,
    Callable[[Mapping[str, Any], str], dict[str, Any]],
] = {
    "flye": _flye_example,
    "nextdenovo": _nextdenovo_example,
    "hifiasm": _hifiasm_example,
    "hifiasm_ont": _hifiasm_ont_example,
}
