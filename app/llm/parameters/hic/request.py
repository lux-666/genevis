"""Strict model-facing request projection for AutoHiC parameter planning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from app.llm.parameters.base import (
    PARAMETER_RECOMMENDATION_CONTRACT,
    build_adjustable_parameters,
)
from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AutoHiCRecommendationContext,
    CanonicalRecommendationFacts,
    CanonicalSpeciesFacts,
)

AUTOHIC_METHOD: Literal["hic_scaffolding.candidate.autohic"] = (
    "hic_scaffolding.candidate.autohic"
)


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
    """Project typed recommendation inputs into the strict model-facing DTO."""

    if (
        not isinstance(catalog, MethodParameterCatalog)
        or catalog.method_id != AUTOHIC_METHOD
    ):
        raise ValueError("AutoHiC model request requires the autohic catalog")
    adjustable = build_adjustable_parameters(catalog)
    if not adjustable:
        raise ValueError("AutoHiC model request requires at least one L2 parameter")
    return AutoHiCParameterRequest(
        schema_version=3,
        task=task,
        facts=facts.species,
        autohic_context=context,
        adjustable_parameters=adjustable,
    )
