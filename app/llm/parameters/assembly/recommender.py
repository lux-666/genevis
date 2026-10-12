"""Invoke the Assembly parameter model and parse its strict response."""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, Field

from app.llm.factory import build_chat_model
from app.llm.json_response import (
    parse_json_object,
    validate_model_response_record,
)
from app.llm.parameters.assembly.prompt import render_assembly_parameter_messages
from app.llm.parameters.assembly.response import normalize_parameter_recommendation
from app.llm.provenance import ModelCallProvenance
from app.llm.recommender import StructuredRecommender


class AssemblyParameterDecision(BaseModel):
    """Raw provider output and its syntactically parsed recommendation."""

    raw_response: str
    parsed: dict[str, Any] | None = None
    valid: bool = False
    error: str | None = None
    model_id: str | None = None
    model_call: ModelCallProvenance = Field(default_factory=ModelCallProvenance)


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
    result = StructuredRecommender(
        lambda payload: normalize_parameter_recommendation(payload, dict(request_dto))
    ).invoke(invocation_messages, model_factory=build_chat_model)
    return AssemblyParameterDecision(
        raw_response=result.raw_response,
        parsed=result.parsed,
        valid=result.valid,
        error=result.error,
        model_id=result.model_id,
        model_call=result.model_call,
    )


def restore_assembly_parameter_decision(
    record: Mapping[str, Any],
    request_dto: Mapping[str, Any],
) -> AssemblyParameterDecision:
    """Rehydrate a decision from immutable model-response evidence."""

    envelope = validate_model_response_record(
        record,
        expected_schema_version=2,
        label="persisted Assembly model response",
    )
    if envelope["validity"] == "invalid":
        return AssemblyParameterDecision(
            raw_response=envelope["raw_response"],
            valid=False,
            error=envelope["error"],
            model_id=envelope["model_id"],
            model_call=ModelCallProvenance.model_validate(record.get("model_call", {})),
        )
    parsed = normalize_parameter_recommendation(
        parse_json_object(envelope["raw_response"]),
        dict(request_dto),
    )
    return AssemblyParameterDecision(
        raw_response=envelope["raw_response"],
        parsed=parsed,
        valid=True,
        error=envelope["error"],
        model_id=envelope["model_id"],
        model_call=ModelCallProvenance.model_validate(record.get("model_call", {})),
    )
