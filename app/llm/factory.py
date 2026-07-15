"""Factory for provider-specific chat model clients."""

from __future__ import annotations

from typing import Any

from app.llm.config import LLMProfile, load_llm_profile


def build_chat_model(profile: LLMProfile | None = None, *, chat_model_cls: type | None = None) -> Any:
    """Build a chat model for the configured Gene Agent LLM profile."""

    resolved = profile or load_llm_profile()
    if resolved.provider != "openai_compatible":
        raise ValueError(f"Unsupported LLM provider: {resolved.provider!r}")
    if not resolved.api_key:
        raise ValueError("GENE_AGENT_LLM_API_KEY is required for LLM calls.")

    cls = chat_model_cls or _load_chat_openai()
    kwargs: dict[str, Any] = {
        "model": resolved.model,
        "openai_api_key": resolved.api_key,
        "temperature": resolved.temperature,
        "max_tokens": resolved.max_tokens,
        "timeout": resolved.timeout_seconds,
        "default_headers": {
            "User-Agent": "gene-agent/1.0",
        },
    }
    if resolved.base_url:
        kwargs["openai_api_base"] = resolved.base_url
    return cls(**kwargs)


def _load_chat_openai() -> type:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("Install langchain-openai to use OpenAI-compatible LLM profiles.") from exc
    return ChatOpenAI
