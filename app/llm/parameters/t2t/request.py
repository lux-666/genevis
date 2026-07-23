"""Strict model-facing request projection for T2T threshold planning."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.shared_parameters import SpeciesFacts

T2T_METHOD = "t2t.alignment.minimap2"
PARAMETER_RECOMMENDATION_CONTRACT = "parameter_recommendation_v1"


class T2TParameterRequest(BaseModel):
    """Exact allowlisted request DTO rendered for the T2T model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    recommendation_contract: Literal["parameter_recommendation_v1"] = (
        PARAMETER_RECOMMENDATION_CONTRACT
    )
    task: ParameterAttemptTask = "recommend_initial"
    method_id: Literal["t2t.alignment.minimap2"] = T2T_METHOD
    species_facts: SpeciesFacts | None = None
    resolved_telomere_motif: str = Field(min_length=2)
    ont_asset_count: int = Field(ge=1)
    adjustable_parameters: dict[str, dict[str, Any]]


def build_t2t_parameter_request(
    *,
    input_profile: Mapping[str, Any],
    catalog: MethodParameterCatalog,
) -> T2TParameterRequest:
    if catalog.method_id != T2T_METHOD:
        raise ValueError("T2T parameter request requires the minimap2 T2T catalog")
    adjustable = {
        name: {
            key: value
            for key, value in spec.model_dump(mode="json").items()
            if key in {"value_type", "allowed_values", "min", "max", "description", "notes"}
            and value is not None
        }
        for name, spec in catalog.parameters.items()
        if spec.level == 2
    }
    return T2TParameterRequest(
        species_facts=input_profile.get("species_facts"),
        resolved_telomere_motif=str(input_profile["resolved_telomere_motif"]),
        ont_asset_count=int(input_profile["ont_asset_count"]),
        adjustable_parameters=adjustable,
    )
