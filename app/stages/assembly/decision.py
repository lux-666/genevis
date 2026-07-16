"""Shared LLM boundary for assembly route and parameter decisions."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.domains.assembly.parameter_recommendation import (
    default_parameter_recommendation,
    normalize_parameter_recommendation,
)
from app.llm.assembly_parameter_prompts import render_assembly_parameter_messages
from app.llm.factory import build_chat_model

AssemblyDecisionKind = Literal["route_selection", "parameter_recommendation"]


class AssemblyDecisionRequest(BaseModel):
    """Validated request passed to the shared assembly advisor."""

    kind: AssemblyDecisionKind
    session_id: str
    prompt_payload: dict[str, Any]
    system_prompt: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    allow_default_on_error: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "AssemblyDecisionRequest":
        if self.kind == "route_selection" and not self.system_prompt:
            raise ValueError("route_selection requires system_prompt")
        return self


class AssemblyDecisionResult(BaseModel):
    """Parsed decision result plus raw response metadata."""

    kind: AssemblyDecisionKind
    raw_response: str
    parsed: dict[str, Any] | None = None
    valid: bool = False
    error: str | None = None
    model_id: str | None = None


def render_assembly_decision_messages(
    request: AssemblyDecisionRequest,
) -> list[dict[str, str]]:
    """Render the ordered messages that will be passed to the model."""

    if request.system_prompt:
        return [
            {"role": "system", "content": request.system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    request.prompt_payload,
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]
    return render_assembly_parameter_messages(request.prompt_payload)


def make_assembly_decision(
    request: AssemblyDecisionRequest,
    *,
    messages: list[dict[str, str]] | None = None,
) -> AssemblyDecisionResult:
    """Invoke the model with one already-rendered request and validate its response."""

    invocation_messages = (
        messages if messages is not None else render_assembly_decision_messages(request)
    )

    try:
        model = build_chat_model()
        response = model.invoke(invocation_messages)
    except Exception as exc:
        if request.kind == "parameter_recommendation" and request.allow_default_on_error:
            try:
                fallback = default_parameter_recommendation(request.prompt_payload)
                return AssemblyDecisionResult(
                    kind=request.kind,
                    raw_response=json.dumps(fallback),
                    parsed=fallback,
                    valid=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as fallback_exc:
                return AssemblyDecisionResult(
                    kind=request.kind,
                    raw_response="",
                    error=(
                        f"{type(exc).__name__}: {exc}; conservative fallback failed: "
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    ),
                )
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response="",
            error=f"{type(exc).__name__}: {exc}",
        )

    raw_content = _response_text(response)
    try:
        parsed = _parse_json_object(raw_content)
        if request.kind == "route_selection":
            _validate_route_selection(parsed, request.allowed_actions)
        else:
            parsed = normalize_parameter_recommendation(parsed, request.prompt_payload)
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response=raw_content,
            parsed=parsed,
            valid=True,
            model_id=type(model).__name__,
        )
    except Exception as exc:
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response=raw_content,
            error=f"{type(exc).__name__}: {exc}",
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


def _validate_route_selection(parsed: dict[str, Any], allowed_actions: list[str]) -> None:
    recommended = parsed.get("recommended_route")
    if recommended not in set(allowed_actions):
        raise ValueError(f"recommended_route {recommended!r} not in allowed actions")
