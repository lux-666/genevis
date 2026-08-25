"""Shared text, JSON-object, and persisted model-response envelope helpers."""

from __future__ import annotations

import json
from typing import Any, Literal, Mapping, TypedDict, cast

ModelResponseValidity = Literal["parsed", "invalid"]


class ModelResponseRecord(TypedDict):
    """Canonical persisted envelope for one raw model response."""

    schema_version: int
    raw_response: str
    validity: ModelResponseValidity
    error: str | None
    model_id: str | None


def response_text(response: Any) -> str:
    """Extract provider-neutral text content from a chat-model response."""

    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", part)) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content).strip()


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
) -> ModelResponseRecord:
    """Build the shared raw-response envelope used by persisted LLM evidence."""

    record: ModelResponseRecord = {
        "schema_version": schema_version,
        "raw_response": raw_response,
        "validity": "parsed" if valid else "invalid",
        "error": error,
        "model_id": model_id,
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
    }
