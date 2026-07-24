"""Strict model-facing request projection for AutoHiC parameter planning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AutoHiCRecommendationContext,
    CanonicalRecommendationFacts,
    CanonicalSpeciesFacts,
)

AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"
PARAMETER_RECOMMENDATION_CONTRACT = "parameter_recommendation_v1"


class _StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AutoHiCParameterRequest(_StrictRequestModel):
    """Exact allowlisted request DTO rendered for the AutoHiC model."""

    schema_version: Literal[3]
    recommendation_contract: Literal["parameter_recommendation_v1"] = (
        PARAMETER_RECOMMENDATION_CONTRACT
    )
    task: ParameterAttemptTask
    method_id: Literal["hic_scaffolding.candidate.autohic"] = AUTOHIC_METHOD
    facts: CanonicalSpeciesFacts
    autohic_context: AutoHiCRecommendationContext
    adjustable_parameters: dict[str, dict[str, Any]]

    @model_validator(mode="after")
    def validate_retry_context(self) -> "AutoHiCParameterRequest":
        has_retry = self.autohic_context.retry is not None
        if (self.task != "recommend_initial") != has_retry:
            raise ValueError("AutoHiC retry tasks require typed context.retry")
        return self


def build_autohic_parameter_request(
    *,
    facts: CanonicalRecommendationFacts,
    context: AutoHiCRecommendationContext,
    catalog: MethodParameterCatalog,
    task: ParameterAttemptTask = "recommend_initial",
) -> AutoHiCParameterRequest:
    """Project typed recommendation inputs into the strict model-facing DTO.

    Only canonical species facts and the typed stage context cross the model
    boundary; paths, artifact references, and run identity remain excluded.
    """

    if catalog.method_id != AUTOHIC_METHOD:
        raise ValueError("AutoHiC model request requires the autohic catalog")
    adjustable = {
        name: {
            key: value
            for key, value in specification.model_dump(mode="json").items()
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
        for name, specification in catalog.parameters.items()
        if specification.layers.l2 is not None
    }
    if not adjustable:
        raise ValueError("AutoHiC model request requires at least one L2 parameter")

    return AutoHiCParameterRequest(
        schema_version=3,
        task=task,
        facts=facts.species,
        autohic_context=context,
        adjustable_parameters=adjustable,
    )
