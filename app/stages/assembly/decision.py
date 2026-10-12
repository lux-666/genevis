"""LLM boundary for Assembly route selection only."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.llm.factory import build_chat_model
from app.llm.json_response import (
    parse_json_object,
    validate_model_response_record,
)
from app.llm.provenance import ModelCallProvenance
from app.llm.recommender import StructuredRecommender

AssemblyDecisionKind = Literal["route_selection"]


class AssemblyDecisionRequest(BaseModel):
    """Validated request passed to the shared assembly advisor."""

    kind: AssemblyDecisionKind
    session_id: str
    prompt_payload: dict[str, Any]
    system_prompt: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    allowed_methods: list[str] = Field(default_factory=list)

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
    model_call: ModelCallProvenance = Field(default_factory=ModelCallProvenance)


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

    def parse(parsed: dict[str, Any]) -> dict[str, Any]:
        _validate_route_selection(
            parsed, request.allowed_actions, request.allowed_methods
        )
        return parsed

    result = StructuredRecommender(parse).invoke(
        invocation_messages, model_factory=build_chat_model
    )
    return AssemblyDecisionResult(
        kind=request.kind,
        raw_response=result.raw_response,
        parsed=result.parsed,
        valid=result.valid,
        error=result.error,
        model_id=result.model_id,
        model_call=result.model_call,
    )


def _validate_route_selection(
    parsed: dict[str, Any], allowed_actions: list[str], allowed_methods: list[str]
) -> None:
    recommended = parsed.get("recommended_route")
    if recommended not in set(allowed_actions):
        raise ValueError(f"recommended_route {recommended!r} not in allowed actions")
    # An actionable route binds one of the offered executable methods.
    method = parsed.get("recommended_method")
    if recommended != "insufficient_data" and allowed_methods and method is None:
        raise ValueError("recommended_method is required for a selected route")
    if method is not None and allowed_methods and method not in set(allowed_methods):
        raise ValueError(f"recommended_method {method!r} not in allowed methods")


def restore_assembly_decision(
    record: dict[str, Any], request: AssemblyDecisionRequest
) -> AssemblyDecisionResult:
    """Rehydrate one immutable Assembly decision without invoking the model."""

    envelope = validate_model_response_record(
        record,
        expected_schema_version=1,
        label="persisted Assembly model response",
    )
    if envelope["validity"] == "invalid":
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response=envelope["raw_response"],
            valid=False,
            error=envelope["error"],
            model_id=envelope["model_id"],
            model_call=ModelCallProvenance.model_validate(record.get("model_call", {})),
        )
    parsed = parse_json_object(envelope["raw_response"])
    _validate_route_selection(parsed, request.allowed_actions, request.allowed_methods)
    return AssemblyDecisionResult(
        kind=request.kind,
        raw_response=envelope["raw_response"],
        parsed=parsed,
        valid=True,
        error=envelope["error"],
        model_id=envelope["model_id"],
        model_call=ModelCallProvenance.model_validate(record.get("model_call", {})),
    )
