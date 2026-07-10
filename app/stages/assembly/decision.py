"""Shared LLM decision boundary for assembly-stage decisions."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domains.assembly.methods_evidence import (
    normalize_hifiasm_methods_response,
    uses_hifiasm_methods_contract,
)
from app.llm.factory import build_chat_model

AssemblyDecisionKind = Literal["route_selection", "parameter_recommendation"]


class AssemblyDecisionRequest(BaseModel):
    """Rendered decision request passed to the shared assembly advisor."""

    kind: AssemblyDecisionKind
    session_id: str
    prompt_payload: dict[str, Any]
    system_prompt: str
    allowed_actions: list[str] = Field(default_factory=list)
    allow_default_on_error: bool = False


class AssemblyDecisionResult(BaseModel):
    """Parsed decision result plus raw response metadata."""

    kind: AssemblyDecisionKind
    raw_response: str
    parsed: dict[str, Any] | None = None
    valid: bool = False
    error: str | None = None
    model_id: str | None = None


def make_assembly_decision(request: AssemblyDecisionRequest) -> AssemblyDecisionResult:
    """Render prompt, call the model, and parse a JSON decision."""

    messages = [
        {"role": "system", "content": request.system_prompt},
        {"role": "user", "content": json.dumps(request.prompt_payload, ensure_ascii=False, indent=2)},
    ]
    try:
        model = build_chat_model()
        response = model.invoke(messages)
    except Exception as exc:
        if request.kind == "parameter_recommendation" and request.allow_default_on_error:
            fallback = _default_parameter_recommendation(request.prompt_payload)
            return AssemblyDecisionResult(
                kind=request.kind,
                raw_response=json.dumps(fallback),
                parsed=fallback,
                valid=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response="",
            parsed=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    raw_content = str(getattr(response, "content", response))
    try:
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON must be an object")
        if request.kind == "route_selection":
            _validate_route_selection(parsed, request.allowed_actions)
        elif request.kind == "parameter_recommendation":
            parsed = _validate_parameter_recommendation(parsed, request.prompt_payload)
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
            raw_response=str(locals().get("raw_content", "")),
            parsed=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _validate_route_selection(parsed: dict[str, Any], allowed_actions: list[str]) -> None:
    recommended = parsed.get("recommended_route")
    if recommended not in set(allowed_actions):
        raise ValueError(f"recommended_route {recommended!r} not in allowed actions")


def _validate_parameter_recommendation(
    parsed: dict[str, Any],
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    if uses_hifiasm_methods_contract(prompt_payload):
        return normalize_hifiasm_methods_response(parsed, prompt_payload)
    if "params" not in parsed:
        raise ValueError("parameter recommendation requires params")
    if not isinstance(parsed.get("params"), dict):
        raise ValueError("params must be an object")
    if "parameter_evidence" in parsed and not isinstance(parsed.get("parameter_evidence"), dict):
        raise ValueError("parameter_evidence must be an object")
    return parsed


def _default_parameter_recommendation(prompt_payload: dict[str, Any]) -> dict[str, Any]:
    reason = "model unavailable; using catalog defaults for all L2 parameters"
    if uses_hifiasm_methods_contract(prompt_payload):
        return normalize_hifiasm_methods_response(
            {
                "case_assessments": [],
                "recommendation": {"adjustable_params": {}},
                "selected_case_ids": [],
                "summary": reason,
            },
            prompt_payload,
        )
    return {"params": {}, "reasoning": reason, "confidence": 0.0}
