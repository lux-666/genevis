"""One Assembly model contract: target parameters with cross-method evidence."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domains.assembly.material_query import visible_query
from app.knowledge.assembly_cases import canonical_tool
from app.knowledge.assembly_retrieval import project_assembly_evidence
from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
)


class AssemblyParameterRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3]
    recommendation_contract: Literal["parameter_recommendation_v1"]
    method_id: str = Field(min_length=1)
    task: ParameterAttemptTask
    facts: CanonicalRecommendationFacts
    assembly_context: AssemblyRecommendationContext
    adjustable_parameters: dict[str, dict[str, Any]]
    knowledge_evidence: dict[str, Any]
    material_context: dict[str, Any]
    method_decision: dict[str, Any]
    method_guidance: list[dict[str, Any]]

    @model_validator(mode="after")
    def validate_retry_context(self) -> AssemblyParameterRequestDTO:
        if (self.task != "recommend_initial") != (
            self.assembly_context.retry is not None
        ):
            raise ValueError(
                "Assembly retry tasks require typed assembly_context.retry"
            )
        return self


def build_parameter_request_dto(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    facts: CanonicalRecommendationFacts,
    context: AssemblyRecommendationContext,
    task: ParameterAttemptTask,
    basis_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    if method_id != catalog.method_id or basis_snapshot.get("method_id") != method_id:
        raise ValueError("parameter request basis method does not match the catalog")
    if basis_snapshot.get("consumer_version") != 5:
        raise ValueError("pre-v5 Assembly basis cannot be used; start a new run")
    evidence = basis_snapshot["knowledge_evidence"]
    if evidence["purpose"] != "parameters" or canonical_tool(
        evidence.get("target_method", "")
    ) != canonical_tool(catalog.tool):
        raise ValueError("parameter evidence purpose or target does not match catalog")
    fields = ("value_type", "allowed_values", "min", "max", "description", "notes")
    adjustable = {
        name: {
            k: v
            for k, v in spec.model_dump(mode="json").items()
            if k in fields and v is not None
        }
        for name, spec in catalog.parameters.items()
        if spec.layers.l2 is not None
    }
    return AssemblyParameterRequestDTO(
        schema_version=3,
        recommendation_contract="parameter_recommendation_v1",
        method_id=method_id,
        task=task,
        facts=facts,
        assembly_context=context,
        adjustable_parameters=adjustable,
        knowledge_evidence=project_assembly_evidence(evidence),
        material_context=deepcopy(visible_query(basis_snapshot["material_query"])),
        method_decision=deepcopy(basis_snapshot["method_decision"]),
        method_guidance=deepcopy(basis_snapshot["method_guidance"]),
    ).model_dump(mode="json", exclude_none=True)
