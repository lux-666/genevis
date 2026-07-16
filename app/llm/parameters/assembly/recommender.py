"""Invoke the Assembly parameter model and parse its strict response."""

from __future__ import annotations

import json
from typing import Any, Mapping

from pydantic import BaseModel

from app.llm.factory import build_chat_model
from app.llm.parameters.assembly.prompt import render_assembly_parameter_messages
from app.llm.parameters.assembly.response import normalize_parameter_recommendation


class AssemblyParameterDecision(BaseModel):
    """Raw provider output and its syntactically parsed recommendation."""

    raw_response: str
    parsed: dict[str, Any] | None = None
    valid: bool = False
    error: str | None = None
    model_id: str | None = None


def recommend_assembly_parameters(
    request_dto: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> AssemblyParameterDecision:
    """Invoke once; parameter semantics remain for the human reviewer."""

    invocation_messages = (
        messages
        if messages is not None
        else render_assembly_parameter_messages(request_dto)
    )
    try:
        model = build_chat_model()
        response = model.invoke(invocation_messages)
    except Exception as exc:
        return AssemblyParameterDecision(
            raw_response="",
            error=f"{type(exc).__name__}: {exc}",
        )

    raw_content = _response_text(response)
    try:
        parsed = normalize_parameter_recommendation(
            _parse_json_object(raw_content),
            dict(request_dto),
        )
    except Exception as exc:
        return AssemblyParameterDecision(
            raw_response=raw_content,
            error=f"{type(exc).__name__}: {exc}",
            model_id=type(model).__name__,
        )
    return AssemblyParameterDecision(
        raw_response=raw_content,
        parsed=parsed,
        valid=True,
        model_id=type(model).__name__,
    )


def restore_assembly_parameter_decision(
    record: Mapping[str, Any],
    request_dto: Mapping[str, Any],
) -> AssemblyParameterDecision:
    """Rehydrate a decision from immutable model-response evidence."""

    if record.get("schema_version") != 2:
        raise ValueError("persisted Assembly model response is not schema v2")
    raw_response = record.get("raw_response")
    validity = record.get("validity")
    error = record.get("error")
    model_id = record.get("model_id")
    if not isinstance(raw_response, str) or validity not in {"parsed", "invalid"}:
        raise ValueError("persisted Assembly model response is malformed")
    if error is not None and not isinstance(error, str):
        raise ValueError("persisted Assembly model response has an invalid error")
    if model_id is not None and not isinstance(model_id, str):
        raise ValueError("persisted Assembly model response has an invalid model_id")
    if validity == "invalid":
        return AssemblyParameterDecision(
            raw_response=raw_response,
            valid=False,
            error=error,
            model_id=model_id,
        )
    parsed = normalize_parameter_recommendation(
        _parse_json_object(raw_response),
        dict(request_dto),
    )
    return AssemblyParameterDecision(
        raw_response=raw_response,
        parsed=parsed,
        valid=True,
        error=error,
        model_id=model_id,
    )


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
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON must be an object")
    return parsed
