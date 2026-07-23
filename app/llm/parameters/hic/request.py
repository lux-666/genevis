"""Strict model-facing request projection for AutoHiC parameter planning."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.artifacts import ParameterAttemptTask
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AutoHiCRecommendationContext,
    CanonicalRecommendationFacts,
    QuantityFact,
)
from app.schemas.shared_parameters import SpeciesFacts


AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"
PARAMETER_RECOMMENDATION_CONTRACT = "parameter_recommendation_v1"


class _StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AutoHiCInputFacts(_StrictRequestModel):
    """Biological/library facts currently accepted by the AutoHiC planner."""

    library_protocols: list[str]
    provided_restriction_enzyme: str | None = None
    species_facts: SpeciesFacts | None = None


class AutoHiCParameterRequest(_StrictRequestModel):
    """Exact allowlisted request DTO rendered for the AutoHiC model."""

    schema_version: Literal[3] = 3
    recommendation_contract: Literal["parameter_recommendation_v1"] = (
        PARAMETER_RECOMMENDATION_CONTRACT
    )
    task: ParameterAttemptTask = "recommend_initial"
    method_id: Literal["hic_scaffolding.candidate.autohic"] = AUTOHIC_METHOD
    input_facts: AutoHiCInputFacts
    adjustable_params: dict[str, dict[str, Any]]
    attempt_history: list[dict[str, Any]] | None = None


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


def build_autohic_parameter_request(
    *,
    facts: CanonicalRecommendationFacts,
    context: AutoHiCRecommendationContext,
    catalog: MethodParameterCatalog,
    task: ParameterAttemptTask = "recommend_initial",
    attempt_history: list[dict[str, Any]] | None = None,
) -> AutoHiCParameterRequest:
    """Project typed recommendation inputs into the strict model-facing DTO.

    Only currently accepted biological/library facts cross the model boundary;
    run identity, paths, artifact references, asset IDs, and unrecognized
    metadata are deliberately absent from this DTO.
    """

    if catalog.method_id != "autohic":
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

    library_protocols = list(context.library_protocols)
    provided_restriction_enzyme = context.provided_restriction_enzyme

    return AutoHiCParameterRequest(
        task=task,
        input_facts=AutoHiCInputFacts(
            library_protocols=library_protocols,
            provided_restriction_enzyme=provided_restriction_enzyme,
            species_facts=_project_canonical_species(facts.species),
        ),
        adjustable_params=adjustable,
        attempt_history=attempt_history or None,
    )
