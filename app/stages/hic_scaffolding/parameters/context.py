"""Build AutoHiCRecommendationContext from stage state."""

from __future__ import annotations

from app.schemas.parameter_recommendation import (
    AutoHiCRecommendationContext,
    RecommendationRetryContext,
)
from app.schemas.state import TopLevelState
from app.stages.hic_scaffolding.state import (
    HiCScaffoldingCursor,
    hic_scaffolding_output,
)


def build_autohic_recommendation_context(
    state: TopLevelState,
    *,
    hic_out: HiCScaffoldingCursor | None = None,
    retry: RecommendationRetryContext | None = None,
) -> AutoHiCRecommendationContext:
    """Build AutoHiCRecommendationContext from pipeline stage state."""

    if hic_out is None:
        hic_out = hic_scaffolding_output(state)

    # library_protocols: list of dicts, extract each .get("library_protocol")
    raw_metadata = hic_out.get("hic_library_metadata") or []
    library_protocols: list[str] = []
    for entry in raw_metadata:
        if isinstance(entry, dict):
            protocol = entry.get("library_protocol")
            if protocol is not None and str(protocol).strip():
                library_protocols.append(str(protocol).strip())

    run_parameters = hic_out.get("autohic_parameters") or {}
    provided_restriction_enzyme = (
        run_parameters.get("restriction_enzyme") or None
        if isinstance(run_parameters, dict)
        else None
    )

    return AutoHiCRecommendationContext(
        stage="hic_scaffolding",
        library_protocols=tuple(library_protocols),
        provided_restriction_enzyme=provided_restriction_enzyme,
        retry=retry,
    )
