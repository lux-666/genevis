"""Strict model-facing request projection for T2T threshold planning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    CanonicalRecommendationFacts,
    CanonicalSpeciesFacts,
    T2TRecommendationContext,
)

T2T_METHOD: Literal["t2t.alignment.minimap2"] = "t2t.alignment.minimap2"
PARAMETER_RECOMMENDATION_CONTRACT: Literal["parameter_recommendation_v1"] = (
    "parameter_recommendation_v1"
)


class T2TParameterRequest(BaseModel):
    """Exact allowlisted request DTO rendered for the T2T model."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[3]
    recommendation_contract: Literal["parameter_recommendation_v1"] = (
        PARAMETER_RECOMMENDATION_CONTRACT
    )
    task: Literal["recommend_initial"]
    method_id: Literal["t2t.alignment.minimap2"] = T2T_METHOD
    facts: CanonicalSpeciesFacts
    t2t_context: T2TRecommendationContext
    adjustable_parameters: dict[str, dict[str, Any]]

    @field_validator("facts", mode="before")
    @classmethod
    def _requires_typed_facts(cls, value: Any) -> CanonicalSpeciesFacts:
        if not isinstance(value, CanonicalSpeciesFacts):
            raise ValueError(
                "T2T parameter request requires typed canonical species facts"
            )
        return value

    @field_validator("t2t_context", mode="before")
    @classmethod
    def _requires_typed_context(cls, value: Any) -> T2TRecommendationContext:
        if not isinstance(value, T2TRecommendationContext):
            raise ValueError("T2T parameter request requires typed T2T context")
        return value


def build_t2t_parameter_request(
    *,
    facts: CanonicalRecommendationFacts,
    context: T2TRecommendationContext,
    catalog: MethodParameterCatalog,
) -> T2TParameterRequest:
    if catalog.method_id != T2T_METHOD:
        raise ValueError("T2T parameter request requires the minimap2 T2T catalog")
    adjustable = {
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
    return T2TParameterRequest(
        schema_version=3,
        task="recommend_initial",
        facts=facts.species,
        t2t_context=context,
        adjustable_parameters=adjustable,
    )
