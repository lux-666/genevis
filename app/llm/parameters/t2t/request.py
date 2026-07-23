"""Strict model-facing request projection for T2T threshold planning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    CanonicalRecommendationFacts,
    QuantityFact,
    T2TRecommendationContext,
)
from app.schemas.shared_parameters import SpeciesFacts

T2T_METHOD = "t2t.alignment.minimap2"
PARAMETER_RECOMMENDATION_CONTRACT = "parameter_recommendation_v1"


class T2TParameterRequest(BaseModel):
    """Exact allowlisted request DTO rendered for the T2T model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[3] = 3
    recommendation_contract: Literal["parameter_recommendation_v1"] = (
        PARAMETER_RECOMMENDATION_CONTRACT
    )
    task: ParameterAttemptTask = "recommend_initial"
    method_id: Literal["t2t.alignment.minimap2"] = T2T_METHOD
    species_facts: SpeciesFacts | None = None
    resolved_telomere_motif: str = Field(min_length=2)
    ont_asset_count: int = Field(ge=1)
    adjustable_parameters: dict[str, dict[str, Any]]


def _project_canonical_species(species: Any) -> SpeciesFacts | None:
    """Project CanonicalSpeciesFacts to SpeciesFacts, or None if no scientific name."""
    if species is None or not species.scientific_name:
        return None
    taxon_id: int | None = None
    if species.taxon_id is not None:
        try:
            taxon_id = int(species.taxon_id)
        except (ValueError, TypeError):
            taxon_id = None
    ploidy_fact = species.ploidy
    ploidy_int: int | None = int(ploidy_fact.value) if ploidy_fact is not None else None
    chrom_fact = species.expected_chromosome_count
    chrom_int: int | None = int(chrom_fact.value) if chrom_fact is not None else None
    return SpeciesFacts(
        scientific_name=species.scientific_name,
        taxon_id=taxon_id,
        clade=species.clade,
        ploidy=ploidy_int,
        expected_chromosome_count=chrom_int,
        telomere_motif=species.supplied_telomere_motif,
    )


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
            if key in {"value_type", "allowed_values", "min", "max", "description", "notes"}
            and value is not None
        }
        for name, spec in catalog.parameters.items()
        if spec.layers.l2 is not None
    }
    return T2TParameterRequest(
        species_facts=_project_canonical_species(facts.species),
        resolved_telomere_motif=context.resolved_telomere_motif,
        ont_asset_count=context.ont_asset_count,
        adjustable_parameters=adjustable,
    )
