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


def make_assembly_decision(request: AssemblyDecisionRequest) -> AssemblyDecisionResult:
    """Render a prompt, invoke the model, and validate its JSON response."""

    if request.system_prompt:
        messages = [
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
    else:
        messages = render_assembly_parameter_messages(request.prompt_payload)

    try:
        model = build_chat_model()
        response = model.invoke(messages)
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

    raw_content = str(getattr(response, "content", response))
    try:
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON must be an object")
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


def _validate_route_selection(parsed: dict[str, Any], allowed_actions: list[str]) -> None:
    recommended = parsed.get("recommended_route")
    if recommended not in set(allowed_actions):
        raise ValueError(f"recommended_route {recommended!r} not in allowed actions")
