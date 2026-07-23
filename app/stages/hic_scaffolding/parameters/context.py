"""Build AutoHiCRecommendationContext from stage state."""

from __future__ import annotations

from typing import Any

from app.schemas.parameter_recommendation import AutoHiCRecommendationContext
from app.schemas.state import TopLevelState
from app.stages.base import stage_output


def build_autohic_recommendation_context(
    state: TopLevelState,
    *,
    hic_out: dict[str, Any] | None = None,
) -> AutoHiCRecommendationContext:
    """Build AutoHiCRecommendationContext from pipeline stage state."""

    if hic_out is None:
        hic_out = stage_output(state, "hic_scaffolding")

    # library_protocols: list of dicts, extract each .get("library_protocol")
    raw_metadata: list[dict[str, Any]] = list(hic_out.get("hic_library_metadata") or [])
    library_protocols: list[str] = []
    for entry in raw_metadata:
        if isinstance(entry, dict):
            protocol = entry.get("library_protocol")
            if protocol is not None and str(protocol).strip():
                library_protocols.append(str(protocol).strip())

    provided_restriction_enzyme = hic_out.get("provided_restriction_enzyme") or None

    return AutoHiCRecommendationContext(
        stage="hic_scaffolding",
        library_protocols=library_protocols,
        provided_restriction_enzyme=provided_restriction_enzyme,
        retry=None,
    )
