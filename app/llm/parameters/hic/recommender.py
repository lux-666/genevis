"""AutoHiC parameter recommender — thin wrapper around the shared LLM base."""

from __future__ import annotations

from typing import Any, Mapping

from app.llm.parameters.base import (
    ParameterDecision,
    invoke_parameter_model,
    normalize_sparse_recommendation,
    parameter_prompt_hash,
    render_parameter_messages,
    restore_parameter_decision,
)

PROMPT_BUNDLE = "hic.parameter_autohic"

# Stage-specific alias kept for callers that reference the type by name.
AutoHiCParameterDecision = ParameterDecision


def render_autohic_parameter_messages(
    prompt_payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Render the exact messages persisted and passed to the model."""
    return render_parameter_messages(PROMPT_BUNDLE, prompt_payload)


def autohic_parameter_prompt_hash() -> str:
    """Hash the immutable AutoHiC parameter prompt templates."""
    return parameter_prompt_hash(PROMPT_BUNDLE)


def recommend_autohic_parameters(
    prompt_payload: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> ParameterDecision:
    """Ask the LLM for one attempt-local sparse AutoHiC mapping."""
    return invoke_parameter_model(PROMPT_BUNDLE, prompt_payload, messages=messages)


def restore_autohic_parameter_decision(
    record: Mapping[str, Any],
) -> ParameterDecision:
    """Rehydrate a decision from immutable model-response evidence."""
    return restore_parameter_decision(record)


# Public alias used by autohic.py and workflow.py.
_normalize_direct_recommendation = normalize_sparse_recommendation
