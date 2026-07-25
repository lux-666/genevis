"""Typed checkpoint boundary for the function-annotation stage cursor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias

from pydantic import (
    ConfigDict,
    TypeAdapter,
    with_config,
)
from pydantic import (
    JsonValue as PydanticJsonValue,
)
from typing_extensions import TypedDict

from app.schemas.execution import MethodExecutionAttemptStatus
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    update_validated_stage_output,
    validated_stage_output,
)

JsonObject: TypeAlias = dict[str, PydanticJsonValue]


@with_config(ConfigDict(extra="forbid"))
class ArtifactRefJson(TypedDict):
    """JSON representation of an immutable artifact reference."""

    schema_version: Literal[2]
    artifact_id: str
    path_ref: str
    sha256: str
    size_bytes: int
    artifact_type: str


@with_config(ConfigDict(extra="forbid"))
class MethodExecutionAttemptJson(TypedDict):
    """Compact direct-execution recovery cursor."""

    schema_version: Literal[2]
    attempt_id: str
    method_id: str
    status: MethodExecutionAttemptStatus
    frozen_attempt_ref: ArtifactRefJson
    execution_ref: ArtifactRefJson | None


@with_config(ConfigDict(extra="forbid"))
class DiamondParametersJson(TypedDict, total=False):
    """Executor-owned DIAMOND parameters."""

    threads: int


@with_config(ConfigDict(extra="forbid"))
class FunctionAnnotationCursor(TypedDict, total=False):
    """All compact JSON values owned by function annotation."""

    status: str
    reason: str
    failure_kind: str
    execution_mode: str
    primary_method: str
    selected_methods: list[str]
    env: dict[str, str]
    env_target: str
    missing_tools: list[str]
    method_name: str

    proteins_fasta_ref: str
    genes_gff3_ref: str | None
    transcripts_fasta_ref: str | None
    input_source: str

    candidate_methods: list[str]
    method_selection_required: bool
    expected_outputs: dict[str, str]
    notes: list[str]
    database_provenance: PydanticJsonValue
    plan_artifact_ref: str

    diamond_parameters: DiamondParametersJson
    execution_attempts: list[MethodExecutionAttemptJson]
    diamond_hits_ref: str | None
    diamond_hits_artifact_ref: ArtifactRefJson

    gene_function_tsv_ref: str | None
    go_annotation_table_ref: str | None
    kegg_annotation_table_ref: str | None
    pfam_interpro_table_ref: str | None
    annotated_gene_models_gff3_ref: str | None
    method_results: list[JsonObject]
    bundle_artifact_ref: str
    artifact_refs: list[str]


FUNCTION_ANNOTATION_CURSOR_ADAPTER = TypeAdapter(FunctionAnnotationCursor)


def function_annotation_output(state: TopLevelState) -> FunctionAnnotationCursor:
    """Validate and narrow the stage cursor at its read boundary."""

    return validated_stage_output(
        state,
        "function_annotation",
        FUNCTION_ANNOTATION_CURSOR_ADAPTER,
    )


def update_function_annotation_output(
    state: TopLevelState,
    payload: Mapping[str, object],
) -> StateUpdate:
    """Merge, validate, and JSON-serialize a stage cursor update."""

    return update_validated_stage_output(
        state,
        "function_annotation",
        payload,
        FUNCTION_ANNOTATION_CURSOR_ADAPTER,
    )
