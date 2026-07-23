"""Shared shape-only helpers for JSON returned by chat models."""

from __future__ import annotations

import json
from typing import Any


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
