"""T2T parameter recommender — thin wrapper around the shared LLM base."""

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

PROMPT_BUNDLE = "t2t.parameter_thresholds"

# Stage-specific alias kept for callers that reference the type by name.
T2TParameterDecision = ParameterDecision


def render_t2t_parameter_messages(
    payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    return render_parameter_messages(PROMPT_BUNDLE, payload)


def t2t_parameter_prompt_hash() -> str:
    return parameter_prompt_hash(PROMPT_BUNDLE)


def recommend_t2t_parameters(
    payload: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> ParameterDecision:
    return invoke_parameter_model(PROMPT_BUNDLE, payload, messages=messages)


def restore_t2t_parameter_decision(record: Mapping[str, Any]) -> ParameterDecision:
    return restore_parameter_decision(record)


# Public alias — imported directly by t2t workflow.py.
normalize_t2t_recommendation = normalize_sparse_recommendation
