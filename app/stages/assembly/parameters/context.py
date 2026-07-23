"""Build AssemblyRecommendationContext from stage state."""

from __future__ import annotations

from typing import Any

from app.schemas.parameter_recommendation import AssemblyRecommendationContext, NormalizedReadType
from app.schemas.state import TopLevelState
from app.stages.base import stage_output


_READ_TYPE_MAP: dict[str, NormalizedReadType] = {
    "hifi": "hifi",
    "ont": "ont",
    "pacbio_raw": "pacbio_raw",
    "nano_raw": "nano_raw",
    "nano_hq": "nano_hq",
}

_ASSEMBLY_ROUTE_VALID = frozenset({"hifi", "ont", "cyclone"})


def _normalize_read_type(raw: Any) -> NormalizedReadType:
    if isinstance(raw, str):
        normalized = _READ_TYPE_MAP.get(raw.lower().strip())
        if normalized is not None:
            return normalized
        # Map common variants to canonical NormalizedReadType values
        lower = raw.lower().strip()
        if lower in ("pacbio_hifi", "pacbio-hifi"):
            return "hifi"
        if lower in ("ont_raw", "ont-raw", "nano-raw", "nanopore"):
            return "ont"
    return "hifi"


def _normalize_assembly_route(raw: Any) -> str:
    if isinstance(raw, str) and raw.lower().strip() in _ASSEMBLY_ROUTE_VALID:
        return raw.lower().strip()
    return "hifi"


def build_assembly_recommendation_context(
    state: TopLevelState,
    *,
    assembly_out: dict[str, Any] | None = None,
    task: str = "recommend_initial",
) -> AssemblyRecommendationContext:
    """Build AssemblyRecommendationContext from pipeline stage state."""

    if assembly_out is None:
        assembly_out = stage_output(state, "assembly")

    input_profile: dict[str, Any] = dict(assembly_out.get("input_profile") or {})

    # assembly_route
    raw_route = assembly_out.get("assembly_route") or input_profile.get("recommended_route")
    assembly_route = _normalize_assembly_route(raw_route)

    # read_type: prefer assembly_selection.read_type, then input_profile, then direct key
    selection: dict[str, Any] = dict(assembly_out.get("assembly_selection") or {})
    raw_read_type = (
        selection.get("read_type")
        or input_profile.get("read_type")
        or input_profile.get("primary_read_type")
        or assembly_out.get("read_type")
    )
    read_type = _normalize_read_type(raw_read_type)

    # target_level
    shared_params: dict[str, Any] = dict(state.get("shared_parameters") or {})
    raw_target = (
        shared_params.get("target_level")
        or input_profile.get("target_level")
    )
    valid_target_levels = {"draft", "near_t2t", "strict_t2t"}
    if isinstance(raw_target, str) and raw_target in valid_target_levels:
        target_level = raw_target
    else:
        target_level = "draft"

    return AssemblyRecommendationContext(
        stage="assembly",
        assembly_route=assembly_route,  # type: ignore[arg-type]
        read_type=read_type,
        target_level=target_level,  # type: ignore[arg-type]
        retry=None,
    )
