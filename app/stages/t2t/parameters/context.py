"""Build T2TRecommendationContext from stage state."""

from __future__ import annotations

from typing import Literal

from app.schemas.parameter_recommendation import T2TRecommendationContext
from app.schemas.state import TopLevelState
from app.stages.t2t.state import T2TStageCursor, read_t2t_stage_output


def build_t2t_recommendation_context(
    state: TopLevelState,
    *,
    t2t_out: T2TStageCursor | None = None,
) -> T2TRecommendationContext:
    """Build T2TRecommendationContext from pipeline stage state."""

    if t2t_out is None:
        t2t_out = read_t2t_stage_output(state)

    resolved_telomere_motif = t2t_out.get("resolved_telomere_motif")
    if not resolved_telomere_motif or not isinstance(resolved_telomere_motif, str):
        raise ValueError(
            "resolved_telomere_motif is required and must be a non-empty string"
        )

    motif_source = t2t_out.get("resolved_telomere_motif_source")
    validated_motif_source: Literal["provided_species_fact", "llm_species_resolution"]
    if motif_source == "provided_species_fact":
        validated_motif_source = "provided_species_fact"
    elif motif_source == "llm_species_resolution":
        validated_motif_source = "llm_species_resolution"
    else:
        raise ValueError(
            "resolved_telomere_motif_source must be provided_species_fact or "
            "llm_species_resolution"
        )
    ont_read_inputs = t2t_out.get("ont_read_inputs")
    if not isinstance(ont_read_inputs, list) or not ont_read_inputs:
        raise ValueError(
            "ont_read_inputs must contain at least one canonical ONT asset"
        )

    return T2TRecommendationContext(
        stage="t2t",
        resolved_telomere_motif=resolved_telomere_motif,
        motif_source=validated_motif_source,
        ont_asset_count=len(ont_read_inputs),
    )
