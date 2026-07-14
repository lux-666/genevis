"""Shared LLM decision boundary for assembly-stage decisions."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from app.domains.assembly.methods_evidence import (
    _require_final_attested_hifiasm_methods_prompt_payload,
    normalize_hifiasm_methods_response,
)
from app.llm.assembly_parameter_context import build_assembly_parameter_context
from app.llm.assembly_parameter_prompts import render_assembly_parameter_messages
from app.llm.factory import build_chat_model

AssemblyDecisionKind = Literal["route_selection", "parameter_recommendation"]
AssemblyPromptContract = Literal["legacy", "hifiasm_methods_v1"]


class AssemblyDecisionRequest(BaseModel):
    """Rendered decision request passed to the shared assembly advisor."""

    kind: AssemblyDecisionKind
    session_id: str
    # Required out-of-band dispatch contract. It is intentionally not part of
    # ``prompt_payload`` and therefore never reaches the model user JSON.
    prompt_contract: AssemblyPromptContract
    # ``dict[str, Any]`` makes Pydantic rebuild dict subclasses and strips the
    # validation-owned Task 4 attestation. ``Any`` plus this identity-preserving
    # validator retains the exact object crossing the decision boundary.
    prompt_payload: Any
    system_prompt: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    allow_default_on_error: bool = False
    _validated_prompt_contract: AssemblyPromptContract | None = PrivateAttr(default=None)

    @field_validator("prompt_payload", mode="before")
    @classmethod
    def _retain_attested_prompt_identity(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("prompt_payload must be an object")
        return value

    @model_validator(mode="after")
    def _validate_explicit_prompt_contract(self) -> "AssemblyDecisionRequest":
        if self.prompt_contract == "hifiasm_methods_v1" and self.kind != "parameter_recommendation":
            raise ValueError("hifiasm_methods_v1 is only valid for parameter recommendations")
        if self.kind == "route_selection" and not self.system_prompt:
            raise ValueError("route_selection requires system_prompt")
        self._validated_prompt_contract = self.prompt_contract
        self._require_current_prompt_attestation()
        return self

    def _require_current_prompt_attestation(self) -> dict[str, Any]:
        # Use the contract captured at validated construction time. Mutating the
        # public field later cannot downgrade an already-established boundary.
        contract = self._validated_prompt_contract or self.prompt_contract
        if contract == "hifiasm_methods_v1":
            return _require_final_attested_hifiasm_methods_prompt_payload(
                self.prompt_payload
            )
        return self.prompt_payload


class AssemblyDecisionResult(BaseModel):
    """Parsed decision result plus raw response metadata."""

    kind: AssemblyDecisionKind
    raw_response: str
    parsed: dict[str, Any] | None = None
    valid: bool = False
    error: str | None = None
    model_id: str | None = None


def make_assembly_decision(request: AssemblyDecisionRequest) -> AssemblyDecisionResult:
    """Render prompt, call the model, and parse a JSON decision."""

    prompt_payload = request._require_current_prompt_attestation()
    model_prompt_payload = (
        build_assembly_parameter_context(prompt_payload)
        if request.kind == "parameter_recommendation"
        else prompt_payload
    )
    if request.system_prompt:
        messages = [
            {"role": "system", "content": request.system_prompt},
            {
                "role": "user",
                "content": json.dumps(model_prompt_payload, ensure_ascii=False, indent=2),
            },
        ]
    else:
        messages = render_assembly_parameter_messages(
            model_prompt_payload,
            prompt_contract=request._validated_prompt_contract or request.prompt_contract,
        )
    try:
        model = build_chat_model()
        response = model.invoke(messages)
    except Exception as exc:
        if request.kind == "parameter_recommendation" and request.allow_default_on_error:
            try:
                prompt_payload = request._require_current_prompt_attestation()
                fallback = _default_parameter_recommendation(
                    prompt_payload,
                    request._validated_prompt_contract or request.prompt_contract,
                )
                return AssemblyDecisionResult(
                    kind=request.kind,
                    raw_response=json.dumps(fallback),
                    parsed=fallback,
                    valid=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as fallback_exc:
                return AssemblyDecisionResult(
                    kind=request.kind,
                    raw_response="",
                    parsed=None,
                    valid=False,
                    error=(
                        f"{type(exc).__name__}: {exc}; conservative fallback failed: "
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    ),
                )
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response="",
            parsed=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    raw_content = str(getattr(response, "content", response))
    try:
        parsed = json.loads(raw_content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON must be an object")
        if request.kind == "route_selection":
            _validate_route_selection(parsed, request.allowed_actions)
        elif request.kind == "parameter_recommendation":
            prompt_payload = request._require_current_prompt_attestation()
            parsed = _validate_parameter_recommendation(
                parsed,
                prompt_payload,
                request._validated_prompt_contract or request.prompt_contract,
            )
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response=raw_content,
            parsed=parsed,
            valid=True,
            model_id=type(model).__name__,
        )
    except Exception as exc:
        return AssemblyDecisionResult(
            kind=request.kind,
            raw_response=str(locals().get("raw_content", "")),
            parsed=None,
            valid=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _validate_route_selection(parsed: dict[str, Any], allowed_actions: list[str]) -> None:
    recommended = parsed.get("recommended_route")
    if recommended not in set(allowed_actions):
        raise ValueError(f"recommended_route {recommended!r} not in allowed actions")


def _validate_parameter_recommendation(
    parsed: dict[str, Any],
    prompt_payload: dict[str, Any],
    prompt_contract: AssemblyPromptContract,
) -> dict[str, Any]:
    if prompt_contract == "hifiasm_methods_v1":
        return normalize_hifiasm_methods_response(parsed, prompt_payload)
    if "params" not in parsed:
        raise ValueError("parameter recommendation requires params")
    if not isinstance(parsed.get("params"), dict):
        raise ValueError("params must be an object")
    if "parameter_evidence" in parsed:
        parameter_evidence = parsed.get("parameter_evidence")
        if not isinstance(parameter_evidence, dict):
            raise ValueError("parameter_evidence must be an object")
        invalid_entries = sorted(
            str(parameter)
            for parameter, evidence in parameter_evidence.items()
            if not isinstance(evidence, dict)
        )
        if invalid_entries:
            raise ValueError(
                "parameter_evidence must map each parameter to one object; "
                f"non-object entries={invalid_entries}"
            )
    return parsed


def _default_parameter_recommendation(
    prompt_payload: dict[str, Any],
    prompt_contract: AssemblyPromptContract,
) -> dict[str, Any]:
    reason = "model unavailable; using catalog defaults for all L2 parameters"
    if prompt_contract == "hifiasm_methods_v1":
        is_retry = prompt_payload.get("task") != "recommend_initial"
        previous_params = (
            (prompt_payload.get("prompt_profile") or {})
            .get("previous_attempt", {})
            .get("params", {})
        )
        retry_evidence = prompt_payload.get("retry_evidence") or {}
        retry_signals = list(retry_evidence.get("signal_ids") or [])
        if is_retry:
            reason = "model unavailable; preserve prior values for human approval"
        decisions: dict[str, dict[str, Any]] = {}
        for parameter, spec in (prompt_payload.get("adjustable_params") or {}).items():
            has_default = isinstance(spec, dict) and spec.get("default") is not None
            has_previous = is_retry and parameter in previous_params
            basis = "retry_failure" if has_previous and retry_signals else "catalog_default"
            decision = {
                "action": (
                    "keep_previous"
                    if has_previous
                    else "keep_default" if has_default else "omit"
                ),
                "basis": basis,
                "supporting_case_ids": [],
                "supporting_span_ids": [],
                "reason": reason,
            }
            if has_previous:
                decision["value"] = previous_params[parameter]
                decision["retry_signals"] = retry_signals
            elif has_default:
                decision["value"] = spec["default"]
            decisions[parameter] = decision
        recommendation = {"adjustable_params": decisions}
        if is_retry:
            recommendation["retry_disposition"] = "retry"
        return normalize_hifiasm_methods_response(
            {
                "case_assessments": [],
                "recommendation": recommendation,
                "selected_case_ids": [],
                "summary": reason,
            },
            prompt_payload,
        )
    return {"params": {}, "reasoning": reason}
