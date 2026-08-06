"""Typed checkpoint boundary for the assembly-review stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import ConfigDict, TypeAdapter, with_config
from typing_extensions import TypeAliasType, TypedDict

from app.schemas.artifacts import ArtifactRefJson
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)

SignalStatus = Literal["pending", "available", "missing", "failed"]
ReviewDecision = Literal[
    "accept",
    "pass",
    "rerun",
    "purge",
    "polish",
    "continue_low_confidence",
    "stop_human_review",
]
ReviewActionType = Literal["pass", "pass_low_confidence", "purge", "polish"]
ReviewActionExecutionMode = Literal["none", "external"]
if TYPE_CHECKING:
    CursorJsonValue: TypeAlias = (
        None
        | bool
        | int
        | float
        | str
        | list["CursorJsonValue"]
        | dict[str, "CursorJsonValue"]
    )
else:
    CursorJsonValue = TypeAliasType(
        "CursorJsonValue",
        None
        | bool
        | int
        | float
        | str
        | list["CursorJsonValue"]
        | dict[str, "CursorJsonValue"],
    )


@with_config(ConfigDict(extra="forbid"))
class AssemblyCandidateCursor(TypedDict):
    """Compact candidate view retained for review and routing."""

    candidate_id: str
    method_name: str
    source: str
    status: Literal["executed"]
    bundle_ref: ArtifactRefJson
    primary_output_ref: ArtifactRefJson


class AssemblyEvalSignalCursor(TypedDict, total=False):
    """JSON projection of :class:`AssemblyEvalSignal`."""

    signal_name: str
    method_name: str
    candidate_id: str
    status: SignalStatus
    metric_refs: list[str]
    notes: list[str]
    metadata: dict[str, CursorJsonValue]


class AssemblyReviewPlanCursor(TypedDict, total=False):
    """JSON projection of :class:`AssemblyReviewPlan`."""

    stage: str
    status: str
    candidate_ids: list[str]
    planned_signals: list[str]
    available_signals: list[str]
    missing_signals: list[str]
    decision: str
    selected_candidate_id: str | None
    rejected_candidate_ids: list[str]
    notes: list[str]
    metadata: dict[str, CursorJsonValue]


class QuastParametersCursor(TypedDict, total=False):
    threads: int


class BuscoParametersCursor(TypedDict, total=False):
    lineage_path: str
    lineage_name: str
    lineage_version: str
    lineage_source: str
    mode: str
    threads: int


@with_config(ConfigDict(extra="forbid"))
class AssemblyReviewCursor(TypedDict, total=False):
    """All JSON-only keys owned by ``stage_outputs.assembly_review``."""

    status: str
    reason: str
    failure_kind: str
    execution_mode: str
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    method_name: str
    execution_attempts: list[dict[str, CursorJsonValue]]
    artifact_refs: list[str]

    candidate_count: int
    candidates: list[AssemblyCandidateCursor]
    rejected_candidates: dict[str, CursorJsonValue] | list[str]
    review_plan: AssemblyReviewPlanCursor
    planned_signals: list[str]
    signals: list[AssemblyEvalSignalCursor]
    eval_bundle_ref: str
    metrics: dict[str, CursorJsonValue]

    quast_parameters: QuastParametersCursor
    busco_parameters: BuscoParametersCursor

    parameter_quality_gate: dict[str, CursorJsonValue] | None
    parameter_quality_checked: bool

    related_species_reference_dir: str
    related_species_evidence_pack: dict[str, CursorJsonValue]
    related_species_evidence_pack_ref: str
    related_species_statistics_pack: dict[str, CursorJsonValue]
    related_species_statistics_pack_ref: str
    assembly_review_comparative_notebook_ref: str
    comparative_figures: dict[str, str]
    candidate_comparison_ref: str

    review_policy: dict[str, CursorJsonValue]
    review_request_artifact_ref: dict[str, CursorJsonValue]
    auto_decidable: bool
    assembly_review_report_ref: str
    assembly_review_packet_ref: str

    decision: ReviewDecision
    selected_candidate_id: str | None
    final_assembly_ref: str | None
    pre_action_assembly_ref: str | None
    action_type: ReviewActionType
    action_execution_mode: ReviewActionExecutionMode
    action_method_name: str | None
    evidence_refs: list[str]
    evidence_notes: list[str]
    notes: list[str]
    decided_by: str
    rationale_artifact_ref: str | None
    review_request_id: str
    review_request_hash: str
    review_decision_artifact_ref: dict[str, CursorJsonValue]
    assembly_review_action_artifact_ref: str | None
    purged_assembly: str | None
    polished_assembly: str | None


ASSEMBLY_REVIEW_CURSOR_ADAPTER = TypeAdapter(AssemblyReviewCursor)
ASSEMBLY_CANDIDATE_CURSOR_ADAPTER = TypeAdapter(AssemblyCandidateCursor)
ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER = TypeAdapter(AssemblyEvalSignalCursor)
ASSEMBLY_REVIEW_PLAN_CURSOR_ADAPTER = TypeAdapter(AssemblyReviewPlanCursor)


def assembly_review_output(state: TopLevelState) -> AssemblyReviewCursor:
    """Read and validate the assembly-review cursor at its package boundary."""

    return validated_stage_output(
        state,
        "assembly_review",
        ASSEMBLY_REVIEW_CURSOR_ADAPTER,
    )


def update_assembly_review_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge, validate, and JSON-serialize an assembly-review cursor update."""

    return update_validated_stage_output(
        state,
        "assembly_review",
        payload,
        ASSEMBLY_REVIEW_CURSOR_ADAPTER,
    )
