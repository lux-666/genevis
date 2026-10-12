"""Shared prompt-invoke-restore core for parameter recommendation models.

Sparse parameter recommenders share prompt rendering and response parsing.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Literal, Mapping

from app.llm.factory import build_chat_model
from app.llm.json_response import parse_json_object
from app.llm.prompts import load_prompt_bundle, render_prompt_messages
from app.llm.provenance import ModelCallProvenance
from app.llm.recommender import StructuredRecommender
from app.schemas.parameter_catalog import MethodParameterCatalog

# Shared across all stages that use the v1 recommendation contract.
PARAMETER_RECOMMENDATION_CONTRACT: Literal["parameter_recommendation_v1"] = (
    "parameter_recommendation_v1"
)


@dataclass(frozen=True)
class ParameterDecision:
    """Raw provider output and its validated sparse recommendation."""

    raw_response: str
    recommendation: dict[str, Any] | None
    valid: bool
    error: str | None = None
    model_id: str | None = None
    model_call: ModelCallProvenance = field(default_factory=ModelCallProvenance)


def normalize_sparse_recommendation(response: dict[str, Any]) -> dict[str, Any]:
    """Parse the one accepted response shape without judging parameter values.

    Raises ``ValueError`` on any structural violation; the caller is
    responsible for catching and wrapping into a ``ParameterDecision``.
    """

    unexpected = sorted(set(response) - {"parameters", "summary"})
    if unexpected:
        raise ValueError(
            f"parameter recommendation contains unsupported fields: {unexpected}"
        )
    parameters = response.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("parameter recommendation requires a parameters object")
    wrapped = sorted(
        name for name, value in parameters.items() if isinstance(value, dict)
    )
    if wrapped:
        raise ValueError(
            "parameter values must be direct JSON values, not wrapper objects: "
            + ", ".join(wrapped)
        )
    summary = response.get("summary")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string when provided")
        summary = summary.strip()
    return {"parameters": deepcopy(parameters), "summary": summary}


def render_parameter_messages(
    bundle_name: str,
    payload: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Render the exact messages persisted and passed to the model."""

    return render_prompt_messages(
        load_prompt_bundle(bundle_name),
        {
            "prompt_payload_json": json.dumps(
                dict(payload), ensure_ascii=False, separators=(",", ":")
            )
        },
    )


def parameter_prompt_hash(bundle_name: str) -> str:
    """Hash the immutable prompt templates for a given bundle."""

    bundle = load_prompt_bundle(bundle_name)
    canonical = json.dumps(
        {"name": bundle.name, "system": bundle.system, "user": bundle.user_template},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()[:12]


def invoke_parameter_model(
    bundle_name: str,
    payload: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> ParameterDecision:
    """Invoke the LLM once; return a validated decision."""

    invocation_messages = messages or render_parameter_messages(bundle_name, payload)
    result = StructuredRecommender(normalize_sparse_recommendation).invoke(
        invocation_messages,
        model_factory=build_chat_model,
    )
    return ParameterDecision(
        result.raw_response,
        result.parsed,
        result.valid,
        result.error,
        result.model_id,
        result.model_call,
    )


def restore_parameter_decision(record: Mapping[str, Any]) -> ParameterDecision:
    """Rehydrate a decision from a persisted schema-v2 model-response record."""

    if record.get("schema_version") != 2:
        raise ValueError("persisted model response is not schema v2")
    raw = record.get("raw_response")
    validity = record.get("validity")
    error = record.get("error")
    model_id = record.get("model_id")
    if not isinstance(raw, str) or validity not in {"parsed", "invalid"}:
        raise ValueError("persisted model response is malformed")
    if error is not None and not isinstance(error, str):
        raise ValueError("persisted model response has an invalid error field")
    if model_id is not None and not isinstance(model_id, str):
        raise ValueError("persisted model response has an invalid model_id field")
    if validity == "invalid":
        return ParameterDecision(
            raw,
            None,
            False,
            error,
            model_id,
            ModelCallProvenance.model_validate(record.get("model_call", {})),
        )
    return ParameterDecision(
        raw,
        normalize_sparse_recommendation(parse_json_object(raw)),
        True,
        error,
        model_id,
        ModelCallProvenance.model_validate(record.get("model_call", {})),
    )


def build_adjustable_parameters(
    catalog: MethodParameterCatalog,
) -> dict[str, dict[str, Any]]:
    """Project L2-eligible catalog parameters into the model-facing allowlist."""

    return {
        name: {
            key: value
            for key, value in spec.model_dump(mode="json").items()
            if key
            in {
                "value_type",
                "allowed_values",
                "min",
                "max",
                "description",
                "notes",
            }
            and value is not None
        }
        for name, spec in catalog.parameters.items()
        if spec.layers.l2 is not None
    }
