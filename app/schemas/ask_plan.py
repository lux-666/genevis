"""Planner state and clarification contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.shared_parameters import SharedParameters
from app.schemas.warnings import PlanWarning as PlanWarning

AskClarificationKind = Literal[
    "stage_selection",
    "method_selection",
    "parameter_values",
    "confirm",
]


class AskPlanState(BaseModel):
    """Transient staged-planning view reconstructed from the TUI draft."""

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
    """Operator-facing request for the next planning clarification step."""

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
    """Structured operator response to a planning clarification request."""

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
