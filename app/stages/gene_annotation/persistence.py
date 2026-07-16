"""Gene-annotation facade over stage-neutral immutable execution evidence."""

from __future__ import annotations

from typing import Any, Mapping

from app.execution.evidence import (
    execution_result_artifact_type,
    frozen_attempt_artifact_type,
    load_execution_result,
    load_frozen_attempt,
    persist_execution_result,
    persist_frozen_attempt,
)
from app.execution.results import ExecutionResult
from app.schemas.artifacts import ArtifactRef
from app.schemas.descriptors import FrozenAttemptSpec
from app.storage.paths import DataLayoutError


GENE_ATTEMPT_ARTIFACT_TYPE = frozen_attempt_artifact_type("gene_annotation")
GENE_EXECUTION_ARTIFACT_TYPE = execution_result_artifact_type("gene_annotation")


def persist_gene_attempt(
    state: Mapping[str, Any],
    attempt: FrozenAttemptSpec,
) -> ArtifactRef:
    if attempt.stage != "gene_annotation":
        raise DataLayoutError("persist_gene_attempt requires a gene_annotation attempt")
    return persist_frozen_attempt(state, attempt)


def load_gene_attempt(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_method_name: str | None = None,
) -> FrozenAttemptSpec:
    return load_frozen_attempt(
        state,
        value,
        expected_stage="gene_annotation",
        expected_method_name=expected_method_name,
    )


def persist_gene_execution(
    state: Mapping[str, Any],
    *,
    method_name: str,
    result: ExecutionResult,
) -> ArtifactRef:
    return persist_execution_result(
        state,
        stage="gene_annotation",
        method_name=method_name,
        result=result,
    )


def load_gene_execution(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_attempt_id: str | None = None,
    expected_method_name: str | None = None,
) -> ExecutionResult:
    return load_execution_result(
        state,
        value,
        expected_stage="gene_annotation",
        expected_attempt_id=expected_attempt_id,
        expected_method_name=expected_method_name,
    )
