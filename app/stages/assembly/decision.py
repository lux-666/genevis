"""LLM boundary for Assembly route selection only."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.llm.factory import build_chat_model
from app.llm.json_response import parse_json_object, response_text

AssemblyDecisionKind = Literal["route_selection"]


class AssemblyDecisionRequest(BaseModel):
    """Validated request passed to the shared assembly advisor."""

    kind: AssemblyDecisionKind
    session_id: str
    prompt_payload: dict[str, Any]
    system_prompt: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_request(self) -> "AssemblyDecisionRequest":
        if not self.system_prompt:
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

    return [
        {"role": "system", "content": request.system_prompt or ""},
        {
            "role": "user",
            "content": json.dumps(
                request.prompt_payload,
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


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
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response="",
            error=f"{type(exc).__name__}: {exc}",
        )

    raw_content = response_text(response)
    try:
        parsed = parse_json_object(raw_content)
        _validate_route_selection(parsed, request.allowed_actions)
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


def _validate_route_selection(
    parsed: dict[str, Any], allowed_actions: list[str]
) -> None:
    recommended = parsed.get("recommended_route")
    if recommended not in set(allowed_actions):
        raise ValueError(f"recommended_route {recommended!r} not in allowed actions")
