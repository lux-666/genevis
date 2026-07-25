"""OpenAI-compatible LLM profile configuration for Gene Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class LLMProfile:
    """Resolved configuration for one LLM usage profile."""

    provider: str
    base_url: str | None
    api_key: str | None
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float


def load_llm_profile(env: Mapping[str, str] | None = None) -> LLMProfile:
    """Load the Gene Agent LLM profile from environment variables."""

    source = env if env is not None else os.environ
    prefix = "GENE_AGENT_LLM_"
    provider = source.get(f"{prefix}PROVIDER", "openai_compatible")
    base_url = source.get(f"{prefix}BASE_URL")
    api_key = source.get(f"{prefix}API_KEY")
    model = source.get(f"{prefix}MODEL", "gpt-5-mini")
    temperature = _float(source.get(f"{prefix}TEMPERATURE", "0.2"), "temperature")
    max_tokens = _int(source.get(f"{prefix}MAX_TOKENS", "4096"), "max_tokens")
    timeout_seconds = _float(
        source.get(f"{prefix}TIMEOUT_SECONDS", "120"), "timeout_seconds"
    )

    return LLMProfile(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
    )


def _float(value: str | None, field_name: str) -> float:
    try:
        return float(value if value is not None else "")
    except ValueError as exc:
        raise ValueError(f"Invalid LLM {field_name}: {value!r}") from exc


def _int(value: str | None, field_name: str) -> int:
    try:
        return int(value if value is not None else "")
    except ValueError as exc:
        raise ValueError(f"Invalid LLM {field_name}: {value!r}") from exc
