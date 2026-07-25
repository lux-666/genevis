"""Prompt rendering and shape-only response parsing for T2T parameters."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping

from app.llm.factory import build_chat_model
from app.llm.json_response import parse_json_object, response_text
from app.llm.prompts import load_prompt_bundle, render_prompt_messages

PROMPT_BUNDLE = "t2t.parameter_thresholds"


@dataclass(frozen=True)
class T2TParameterDecision:
    raw_response: str
    recommendation: dict[str, Any] | None
    valid: bool
    error: str | None = None
    model_id: str | None = None


def render_t2t_parameter_messages(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    return render_prompt_messages(
        load_prompt_bundle(PROMPT_BUNDLE),
        {
            "prompt_payload_json": json.dumps(
                dict(payload), ensure_ascii=False, separators=(",", ":")
            )
        },
    )


def t2t_parameter_prompt_hash() -> str:
    bundle = load_prompt_bundle(PROMPT_BUNDLE)
    canonical = json.dumps(
        {"name": bundle.name, "system": bundle.system, "user": bundle.user_template},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def recommend_t2t_parameters(
    payload: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> T2TParameterDecision:
    invocation_messages = messages or render_t2t_parameter_messages(payload)
    try:
        model = build_chat_model()
        response = model.invoke(invocation_messages)
    except Exception as exc:
        return T2TParameterDecision("", None, False, f"{type(exc).__name__}: {exc}")
    raw = response_text(response)
    try:
        parsed = normalize_t2t_recommendation(parse_json_object(raw))
    except Exception as exc:
        return T2TParameterDecision(
            raw, None, False, f"{type(exc).__name__}: {exc}", type(model).__name__
        )
    return T2TParameterDecision(raw, parsed, True, model_id=type(model).__name__)


def restore_t2t_parameter_decision(record: Mapping[str, Any]) -> T2TParameterDecision:
    if record.get("schema_version") != 2:
        raise ValueError("persisted T2T model response is not schema v2")
    raw = record.get("raw_response")
    validity = record.get("validity")
    if not isinstance(raw, str) or validity not in {"parsed", "invalid"}:
        raise ValueError("persisted T2T model response is malformed")
    if validity == "invalid":
        return T2TParameterDecision(
            raw, None, False, record.get("error"), record.get("model_id")
        )
    return T2TParameterDecision(
        raw,
        normalize_t2t_recommendation(parse_json_object(raw)),
        True,
        record.get("error"),
        record.get("model_id"),
    )


def normalize_t2t_recommendation(response: dict[str, Any]) -> dict[str, Any]:
    unexpected = sorted(set(response) - {"parameters", "summary"})
    if unexpected:
        raise ValueError(f"T2T response contains unsupported fields: {unexpected}")
    parameters = response.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("T2T response requires a parameters object")
    wrapped = sorted(
        name for name, value in parameters.items() if isinstance(value, dict)
    )
    if wrapped:
        raise ValueError(
            "parameter values must be direct JSON values, not wrapper objects: "
            + ", ".join(wrapped)
        )
    summary = response.get("summary")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string when provided")
        summary = summary.strip()
    return {"parameters": deepcopy(parameters), "summary": summary}
