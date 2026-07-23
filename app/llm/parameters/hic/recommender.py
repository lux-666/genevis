"""Prompt rendering and strict response parsing for AutoHiC parameters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping

from app.llm.factory import build_chat_model
from app.llm.json_response import parse_json_object, response_text
from app.llm.prompts import load_prompt_bundle, render_prompt_messages

PROMPT_BUNDLE = "hic.parameter_autohic"


@dataclass(frozen=True)
class AutoHiCParameterDecision:
    """The raw model response and its validated direct recommendation."""

    raw_response: str
    recommendation: dict[str, Any] | None
    valid: bool
    error: str | None = None
    model_id: str | None = None


def render_autohic_parameter_messages(
    prompt_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Render the exact messages persisted and passed to the model."""

    return render_prompt_messages(
        load_prompt_bundle(PROMPT_BUNDLE),
        {
            "prompt_payload_json": json.dumps(
                dict(prompt_payload),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        },
    )


def autohic_parameter_prompt_hash() -> str:
    """Hash the immutable AutoHiC parameter prompt templates."""

    bundle = load_prompt_bundle(PROMPT_BUNDLE)
    canonical = json.dumps(
        {"name": bundle.name, "system": bundle.system, "user": bundle.user_template},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def recommend_autohic_parameters(
    prompt_payload: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> AutoHiCParameterDecision:
    """Ask the LLM for one attempt-local sparse AutoHiC mapping."""

    invocation_messages = (
        messages
        if messages is not None
        else render_autohic_parameter_messages(prompt_payload)
    )
    try:
        model = build_chat_model()
        response = model.invoke(invocation_messages)
    except Exception as exc:
        return AutoHiCParameterDecision(
            raw_response="",
            recommendation=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    raw_response = response_text(response)
    try:
        parsed = parse_json_object(raw_response)
        recommendation = _normalize_direct_recommendation(parsed)
    except Exception as exc:
        return AutoHiCParameterDecision(
            raw_response=raw_response,
            recommendation=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
            model_id=type(model).__name__,
        )
    return AutoHiCParameterDecision(
        raw_response=raw_response,
        recommendation=recommendation,
        valid=True,
        model_id=type(model).__name__,
    )


def restore_autohic_parameter_decision(
    record: Mapping[str, Any],
) -> AutoHiCParameterDecision:
    """Rehydrate a decision from immutable model-response evidence."""

    if record.get("schema_version") != 2:
        raise ValueError("persisted AutoHiC model response is not schema v2")
    raw_response = record.get("raw_response")
    validity = record.get("validity")
    error = record.get("error")
    model_id = record.get("model_id")
    if not isinstance(raw_response, str) or validity not in {"parsed", "invalid"}:
        raise ValueError("persisted AutoHiC model response is malformed")
    if error is not None and not isinstance(error, str):
        raise ValueError("persisted AutoHiC model response has an invalid error")
    if model_id is not None and not isinstance(model_id, str):
        raise ValueError("persisted AutoHiC model response has an invalid model_id")
    if validity == "invalid":
        return AutoHiCParameterDecision(
            raw_response=raw_response,
            recommendation=None,
            valid=False,
            error=error,
            model_id=model_id,
        )
    return AutoHiCParameterDecision(
        raw_response=raw_response,
        recommendation=_normalize_direct_recommendation(
            parse_json_object(raw_response)
        ),
        valid=True,
        error=error,
        model_id=model_id,
    )


def _normalize_direct_recommendation(response: dict[str, Any]) -> dict[str, Any]:
    unexpected_fields = sorted(set(response) - {"parameters", "summary"})
    if unexpected_fields:
        raise ValueError(
            f"AutoHiC response contains unsupported fields: {unexpected_fields}"
        )
    parameters = response.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("AutoHiC response requires a parameters object")
    wrapped = sorted(name for name, value in parameters.items() if isinstance(value, dict))
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
    return {
        "parameters": dict(parameters),
        "summary": summary,
    }
