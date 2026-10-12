"""Shared text, JSON-object, and persisted model-response envelope helpers."""

from __future__ import annotations

import json
from typing import Any, Literal, Mapping, TypedDict, cast

from app.llm.provenance import ModelCallProvenance

ModelResponseValidity = Literal["parsed", "invalid"]


class ModelResponseRecord(TypedDict):
    """Canonical persisted envelope for one raw model response."""

    schema_version: int
    raw_response: str
    validity: ModelResponseValidity
    error: str | None
    model_id: str | None
    model_call: dict[str, Any]


def response_text(response: Any) -> str:
    """Extract provider-neutral text content from a chat-model response."""

    content = getattr(response, "content", response)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                raise ValueError("LLM content blocks must contain text")
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM returned empty content")
    return content.strip()


def parse_json_object(text: str) -> dict[str, Any]:
    """Accept one JSON object, with an optional Markdown code fence."""

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


def model_response_record(
    *,
    schema_version: int,
    raw_response: str,
    valid: bool,
    error: str | None,
    model_id: str | None,
    model_call: dict[str, Any] | None = None,
) -> ModelResponseRecord:
    """Build the shared raw-response envelope used by persisted LLM evidence."""

    record: ModelResponseRecord = {
        "schema_version": schema_version,
        "raw_response": raw_response,
        "validity": "parsed" if valid else "invalid",
        "error": error,
        "model_id": model_id,
        "model_call": ModelCallProvenance.model_validate(model_call or {}).model_dump(
            mode="json"
        ),
    }
    return validate_model_response_record(
        record,
        expected_schema_version=schema_version,
        label="model response",
    )


def validate_model_response_record(
    record: Mapping[str, Any],
    *,
    expected_schema_version: int,
    label: str,
) -> ModelResponseRecord:
    """Validate and normalize the fields shared by persisted model responses."""

    actual_schema_version = record.get("schema_version")
    if (
        type(expected_schema_version) is not int
        or expected_schema_version < 1
        or type(actual_schema_version) is not int
        or actual_schema_version != expected_schema_version
    ):
        raise ValueError(f"{label} is not schema v{expected_schema_version}")
    raw_response = record.get("raw_response")
    validity = record.get("validity")
    error = record.get("error")
    model_id = record.get("model_id")
    if not isinstance(raw_response, str) or validity not in {"parsed", "invalid"}:
        raise ValueError(f"{label} is malformed")
    if error is not None and not isinstance(error, str):
        raise ValueError(f"{label} has an invalid error")
    if model_id is not None and not isinstance(model_id, str):
        raise ValueError(f"{label} has an invalid model_id")
    return {
        "schema_version": expected_schema_version,
        "raw_response": raw_response,
        "validity": cast(ModelResponseValidity, validity),
        "error": error,
        "model_id": model_id,
        "model_call": ModelCallProvenance.model_validate(
            record.get("model_call", {})
        ).model_dump(mode="json"),
    }
