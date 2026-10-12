"""Credential-free model invocation identity, separate from client implementation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelCallProvenance(BaseModel):
    """Unknown provider facts remain null; never infer them from a class name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    configured_model: str | None = None
    returned_model: str | None = None
    client_implementation: str | None = None
    output_method: Literal["prompt_json", "json_schema", "function_calling"] | None = (
        None
    )
    sampling: dict[str, float | int | bool | None] = Field(default_factory=dict)


def model_call_provenance(model: Any, method: str) -> ModelCallProvenance:
    name = getattr(model, "model_name", None) or getattr(model, "model", None)
    sampling = {}
    for key in ("temperature", "top_p", "seed", "max_tokens", "max_completion_tokens"):
        value = getattr(model, key, None)
        if isinstance(value, (int, float, bool)):
            sampling[key] = value
    return ModelCallProvenance.model_validate(
        {
            "configured_model": name if isinstance(name, str) else None,
            "client_implementation": (
                f"{type(model).__module__}.{type(model).__qualname__}"
            ),
            "output_method": method,
            "sampling": sampling,
        }
    )


def returned_model_provenance(
    call: ModelCallProvenance, response: Any
) -> ModelCallProvenance:
    metadata = getattr(response, "response_metadata", None)
    if not isinstance(metadata, dict):
        return call
    name = metadata.get("model_name") or metadata.get("model")
    return call.model_copy(
        update={"returned_model": name if isinstance(name, str) else None}
    )
