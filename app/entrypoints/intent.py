"""Natural-language request parsing for workflow entry."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from pydantic import ValidationError

from app.llm.factory import build_chat_model
from app.llm.prompts import load_prompt_bundle, render_prompt_messages
from app.schemas.entry import EntryRequest

_LIFECYCLE_PATTERNS = [
    r"\bcontinue\b",
    r"\bresume\b",
    r"\binspect\b",
    r"\bretry\b",
    r"\brerun\b",
    r"\brebuild\b",
    "继续",
    "恢复",
    "查看",
    "检查",
    "重试",
    "重新运行",
    "重建",
]


def reject_lifecycle_request(request_text: str) -> None:
    """Reject lifecycle requests that must not be treated as new workflow starts."""

    for pattern in _LIFECYCLE_PATTERNS:
        if re.search(pattern, request_text, flags=re.IGNORECASE):
            raise ValueError(
                "Lifecycle requests such as continue/resume/inspect/retry/rerun/rebuild "
                "are not supported by the start parser. Use a structured run request, "
                "or wait for the review/resume command surface."
            )


def parse_intent_response(response_text: str, *, defaults: Mapping[str, Any] | None = None) -> EntryRequest:
    """Parse one LLM response into a validated entry request."""

    payload = _parse_single_json_object(response_text)
    merged = _merge_defaults(payload, defaults)
    try:
        return EntryRequest.model_validate(merged)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg', 'invalid value')}"
            for err in exc.errors()
        )
        raise ValueError(
            "Intent JSON did not match EntryRequest schema. "
            f"Details: {details}. Response snippet: {_snippet(response_text)}"
        ) from exc


def parse_request_with_llm(
    request_text: str,
    *,
    defaults: Mapping[str, Any] | None = None,
    model: Any | None = None,
) -> EntryRequest:
    """Use the intent prompt bundle to parse natural language outside the graph."""

    reject_lifecycle_request(request_text)
    bundle = load_prompt_bundle("intent.parse")
    messages = render_prompt_messages(
        bundle,
        {
            "request_text": request_text,
            "defaults": dict(defaults or {}),
        },
    )
    chat_model = model or build_chat_model()
    chat_model = _bind_intent_response_format(chat_model)
    response = chat_model.invoke(messages)
    response_text = _response_to_text(response)
    return parse_intent_response(response_text, defaults=defaults)


def _parse_single_json_object(response_text: str) -> dict[str, Any]:
    extracted = _extract_json_object(response_text)
    try:
        payload = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Intent parser expected a JSON object, but decoding failed: "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno}). "
            f"Response snippet: {_snippet(response_text)}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            "Intent parser expected a JSON object at top level. "
            f"Got {type(payload).__name__}. Response snippet: {_snippet(response_text)}"
        )
    return payload


def _merge_defaults(payload: Mapping[str, Any], defaults: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = dict(payload)
    for key in ("session_id", "data_dir"):
        if key not in merged and defaults and key in defaults and defaults[key] is not None:
            merged[key] = defaults[key]
    return merged


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Intent parser received an empty LLM response.")

    if stripped.startswith("["):
        return stripped

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        fenced_text = fenced.group(1).strip()
        if fenced_text.startswith("["):
            return fenced_text
        fenced_payloads = _extract_balanced_json_objects(fenced_text)
        if len(fenced_payloads) == 1:
            return fenced_payloads[0]
        if len(fenced_payloads) > 1:
            raise ValueError("Intent parser found multiple JSON objects inside one fenced block.")
        if fenced_text.startswith("{"):
            return fenced_text

    payloads = _extract_balanced_json_objects(stripped)
    if len(payloads) == 1:
        return payloads[0]
    if len(payloads) > 1:
        raise ValueError("Intent parser found multiple JSON objects in the LLM response.")
    if stripped.startswith("{"):
        return stripped

    raise ValueError("Could not find a JSON object in the LLM response.")


def _extract_balanced_json_objects(text: str) -> list[str]:
    payloads: list[str] = []
    start_index: int | None = None
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(text):
        if start_index is None:
            if char == "{":
                start_index = index
                depth = 1
            continue

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                payloads.append(text[start_index : index + 1])
                start_index = None

    return payloads


def _bind_intent_response_format(chat_model: Any) -> Any:
    bind = getattr(chat_model, "bind", None)
    if not callable(bind):
        return chat_model

    schema = EntryRequest.model_json_schema()
    schema["additionalProperties"] = False
    try:
        return bind(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "entry_request",
                    "schema": schema,
                    "strict": True,
                },
            }
        )
    except Exception:
        return bind(response_format={"type": "json_object"})


def _response_to_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    return str(content)


def _snippet(text: str, limit: int = 220) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return f"{one_line[:limit]}..."
