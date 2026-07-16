"""Prompt rendering and strict response parsing for AutoHiC parameters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping

from app.llm.factory import build_chat_model
from app.llm.prompts import load_prompt_bundle, render_prompt_messages


PROMPT_BUNDLE = "hic_scaffolding.parameter_autohic"


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
                indent=2,
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


def recommend_autohic_parameter_patch(
    prompt_payload: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> AutoHiCParameterDecision:
    """Ask the LLM directly for the restriction enzyme; never auto-default."""

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

    raw_response = _response_text(response)
    try:
        parsed = _parse_json_object(raw_response)
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


def _normalize_direct_recommendation(response: dict[str, Any]) -> dict[str, Any]:
    unexpected_fields = sorted(set(response) - {"parameter_patch", "summary"})
    if unexpected_fields:
        raise ValueError(
            f"AutoHiC response contains unsupported fields: {unexpected_fields}"
        )
    patch = response.get("parameter_patch")
    if not isinstance(patch, dict):
        raise ValueError("AutoHiC response requires a parameter_patch object")
    if set(patch) != {"restriction_enzyme"}:
        raise ValueError(
            "AutoHiC parameter_patch must contain only restriction_enzyme"
        )
    enzyme = patch["restriction_enzyme"]
    if not isinstance(enzyme, str) or not enzyme.strip():
        raise ValueError("restriction_enzyme must be a non-empty string")
    enzyme = enzyme.strip()
    if any(character in enzyme for character in ("\n", "\r", "=")):
        raise ValueError("restriction_enzyme contains invalid config characters")
    summary = response.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("summary must be a string when provided")
    return {
        "parameter_patch": {"restriction_enzyme": enzyme},
        "params": {"restriction_enzyme": enzyme},
        "summary": (summary or "LLM recommended a Hi-C restriction enzyme for review").strip(),
    }


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", part)) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("LLM response JSON must be an object")
    return value
