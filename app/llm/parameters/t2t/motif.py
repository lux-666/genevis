"""LLM boundary for resolving a species telomere repeat motif."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.llm.factory import build_chat_model
from app.llm.json_response import parse_json_object, response_text
from app.llm.prompts import load_prompt_bundle, render_prompt_messages
from app.schemas.parameter_recommendation import (
    T2T_MOTIF_RESOLVER_ID,
    CanonicalSpeciesFacts,
)
from app.schemas.shared_parameters import DnaMotif, normalize_dna_motif

PROMPT_BUNDLE = "t2t.telomere_motif"
MOTIF_RESOLUTION_CONTRACT = "t2t_telomere_motif_v1"


class T2TMotifRequest(BaseModel):
    """Model-visible species context for one motif-resolution call."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[3] = 3
    resolution_contract: Literal["t2t_telomere_motif_v1"] = MOTIF_RESOLUTION_CONTRACT
    resolver_id: Literal["t2t.telomere_motif_resolver"] = T2T_MOTIF_RESOLVER_ID
    facts: CanonicalSpeciesFacts

    @field_validator("facts", mode="before")
    @classmethod
    def _requires_typed_facts(cls, value: Any) -> CanonicalSpeciesFacts:
        if not isinstance(value, CanonicalSpeciesFacts):
            raise ValueError("T2T motif request requires typed canonical species facts")
        return value

    @model_validator(mode="after")
    def _requires_context(self) -> "T2TMotifRequest":
        if not self.facts.scientific_name:
            raise ValueError("T2T motif resolution requires facts.scientific_name")
        return self


@dataclass(frozen=True)
class T2TMotifDecision:
    """Raw provider output and the shape-validated resolution."""

    raw_response: str
    status: Literal["resolved", "unresolved"] | None
    resolved_telomere_motif: DnaMotif | None
    summary: str | None
    valid: bool
    error: str | None = None
    model_id: str | None = None


def build_t2t_motif_request(
    *,
    facts: CanonicalSpeciesFacts,
) -> dict[str, Any]:
    """Build a small, allowlisted DTO without paths or execution metadata."""

    return T2TMotifRequest(
        facts=facts,
    ).model_dump(mode="json", exclude_none=True)


def render_t2t_motif_messages(
    request_dto: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Render the exact provider-neutral prompt messages."""

    return render_prompt_messages(
        load_prompt_bundle(PROMPT_BUNDLE),
        {
            "motif_request_json": json.dumps(
                dict(request_dto), ensure_ascii=False, separators=(",", ":")
            )
        },
    )


def t2t_motif_prompt_hash() -> str:
    """Hash the immutable motif prompt templates for provenance."""

    bundle = load_prompt_bundle(PROMPT_BUNDLE)
    canonical = json.dumps(
        {"name": bundle.name, "system": bundle.system, "user": bundle.user_template},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def resolve_t2t_motif(
    request_dto: Mapping[str, Any],
    *,
    messages: list[dict[str, str]] | None = None,
) -> T2TMotifDecision:
    """Ask once and validate only the declared response shape and DNA syntax."""

    invocation_messages = (
        messages if messages is not None else render_t2t_motif_messages(request_dto)
    )
    try:
        model = build_chat_model()
        response = model.invoke(invocation_messages)
    except Exception as exc:
        return T2TMotifDecision(
            raw_response="",
            status=None,
            resolved_telomere_motif=None,
            summary=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    raw = response_text(response)
    try:
        parsed = normalize_t2t_motif_resolution(parse_json_object(raw))
    except Exception as exc:
        return T2TMotifDecision(
            raw_response=raw,
            status=None,
            resolved_telomere_motif=None,
            summary=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
            model_id=type(model).__name__,
        )
    return T2TMotifDecision(
        raw_response=raw,
        status=parsed["status"],
        resolved_telomere_motif=parsed["telomere_motif"],
        summary=parsed.get("summary"),
        valid=True,
        model_id=type(model).__name__,
    )


def restore_t2t_motif_decision(record: Mapping[str, Any]) -> T2TMotifDecision:
    """Rehydrate a decision from an immutable model-response artifact."""

    if record.get("schema_version") != 2:
        raise ValueError("persisted T2T motif model response is not schema v2")
    raw = record.get("raw_response")
    validity = record.get("validity")
    error = record.get("error")
    model_id = record.get("model_id")
    if not isinstance(raw, str) or validity not in {"parsed", "invalid"}:
        raise ValueError("persisted T2T motif model response is malformed")
    if error is not None and not isinstance(error, str):
        raise ValueError("persisted T2T motif model response has an invalid error")
    if model_id is not None and not isinstance(model_id, str):
        raise ValueError("persisted T2T motif model response has an invalid model_id")
    if validity == "invalid":
        return T2TMotifDecision(
            raw_response=raw,
            status=None,
            resolved_telomere_motif=None,
            summary=None,
            valid=False,
            error=error,
            model_id=model_id,
        )
    parsed = normalize_t2t_motif_resolution(parse_json_object(raw))
    return T2TMotifDecision(
        raw_response=raw,
        status=parsed["status"],
        resolved_telomere_motif=parsed["telomere_motif"],
        summary=parsed.get("summary"),
        valid=True,
        error=error,
        model_id=model_id,
    )


def normalize_t2t_motif_resolution(response: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the strict resolution response without biological inference."""

    unexpected = sorted(set(response) - {"status", "telomere_motif", "summary"})
    if unexpected:
        raise ValueError(
            f"T2T motif response contains unsupported fields: {unexpected}"
        )
    missing = sorted({"status", "telomere_motif"} - set(response))
    if missing:
        raise ValueError(f"T2T motif response is missing required fields: {missing}")
    status = response.get("status")
    if status not in {"resolved", "unresolved"}:
        raise ValueError("T2T motif response status must be resolved or unresolved")
    motif = response.get("telomere_motif")
    if status == "resolved":
        if not isinstance(motif, str):
            raise ValueError("resolved T2T motif response requires a string motif")
        motif = normalize_dna_motif(motif)
    elif motif is not None:
        raise ValueError("unresolved T2T motif response must use a null motif")
    summary = response.get("summary")
    if summary is not None:
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string when provided")
        summary = summary.strip()
    return {"status": status, "telomere_motif": motif, "summary": summary}
