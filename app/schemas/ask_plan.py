"""Ask planning and clarification contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.schemas.shared_parameters import SharedParameters
from app.schemas.warnings import PlanWarning as PlanWarning

AskClarificationKind = Literal[
    "stage_selection",
    "method_selection",
    "parameter_values",
    "confirm",
]


class AskPlanState(BaseModel):
    """Durable state for a staged ask planning flow."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    session_id: str
    data_dir: str
    request_text: str | None = (
        None  # original NL request, serialized for audit; not read back by code
    )
    entry_request: dict[str, Any]
    manifest_ref: str
    selected_stages: list[str] | None = None
    resolved_execution_order: list[str] | None = None
    selected_methods: dict[str, str | list[str]] | None = None
    # Three parameter scopes, threaded consistently through response -> merge ->
    # request -> run_config -> checkpoint: shared_parameters are pipeline-wide
    # (platform, genome size); method_parameters key off a method id (e.g. Flye
    # threads); stage_parameters are stage-scoped and not owned by one method
    # (e.g. function_annotation.database_provenance).
    shared_parameters: SharedParameters | None = None
    method_parameters: dict[str, dict[str, Any]] | None = None
    stage_parameters: dict[str, dict[str, Any]] | None = None
    confirmed: bool = False


class AskClarificationRequest(BaseModel):
    """Operator-facing request for the next ask clarification step."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["clarification_required", "confirm_required"]
    kind: AskClarificationKind
    session_id: str
    ask_plan_ref: str
    manifest_ref: str
    question: str
    summary: dict[str, Any] | None = None
    choices: dict[str, Any] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)
    response_schema: dict[str, Any]
    next_action: dict[str, Any]


class AskClarificationResponse(BaseModel):
    """Structured operator response to an ask clarification request."""

    model_config = ConfigDict(extra="forbid")

    kind: AskClarificationKind
    selected_stages: list[str] | None = None
    selected_methods: dict[str, str | list[str]] | None = None
    shared_parameters: SharedParameters | None = None
    method_parameters: dict[str, dict[str, Any]] | None = None
    stage_parameters: dict[str, dict[str, Any]] | None = None
    confirmed: bool | None = None

    @model_validator(mode="after")
    def _require_kind_payload(self) -> "AskClarificationResponse":
        if self.kind == "stage_selection" and not self.selected_stages:
            raise ValueError("stage_selection response requires selected_stages")
        if self.kind == "method_selection" and not self.selected_methods:
            raise ValueError("method_selection response requires selected_methods")
        if self.kind == "parameter_values" and not (
            self.method_parameters or self.shared_parameters or self.stage_parameters
        ):
            raise ValueError(
                "parameter_values response requires method_parameters, "
                "shared_parameters, or stage_parameters"
            )
        if self.kind == "confirm" and self.confirmed is None:
            raise ValueError("confirm response requires confirmed field")
        return self


def load_ask_plan(path: str | Path) -> AskPlanState:
    payload = _load_json_object(path, label="ask plan")
    try:
        return AskPlanState.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"ask plan failed validation: {exc}") from exc


def save_ask_plan(plan: AskPlanState, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def load_ask_clarification_response(path: str | Path) -> AskClarificationResponse:
    payload = _load_json_object(path, label="ask clarification response")
    try:
        return AskClarificationResponse.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"ask clarification response failed validation: {exc}"
        ) from exc


def _load_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    ref = Path(path)
    if not ref.exists():
        raise FileNotFoundError(f"{label} not found: {ref}")
    try:
        text = ref.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{label} is not readable: {ref}: {exc}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object at the top level")
    return payload
