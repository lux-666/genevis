"""Build the typed Assembly parameter-recommendation context."""

from __future__ import annotations

from typing import Literal, cast

from app.domains.assembly.material_query import resolved_material_context
from app.domains.assembly.selection import (
    AssemblySelection,
    validate_assembly_selection,
)
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    RecommendationRetryContext,
)
from app.schemas.state import TopLevelState
from app.stages.assembly.state import AssemblyStageCursor, assembly_stage_output

AssemblyContextRoute = Literal["hifi", "ont", "cyclone"]
AssemblyTargetLevel = Literal["draft", "near_t2t", "strict_t2t"]

_ASSEMBLY_ROUTES = frozenset({"hifi", "ont", "cyclone"})
_TARGET_LEVELS = frozenset({"draft", "near_t2t", "strict_t2t"})


def build_assembly_recommendation_context(
    state: TopLevelState,
    *,
    assembly_out: AssemblyStageCursor | None = None,
    retry_context: RecommendationRetryContext | None = None,
) -> AssemblyRecommendationContext:
    """Build context only from the canonical selection and explicit stage fields."""

    if assembly_out is None:
        assembly_out = assembly_stage_output(state)

    selection = _require_selection(assembly_out.get("assembly_selection"))
    assembly_route = _require_route(
        assembly_out.get("selected_route") or assembly_out.get("assembly_route")
    )
    target_level = _require_target_level(assembly_out.get("target_level"))

    return AssemblyRecommendationContext(
        stage="assembly",
        assembly_route=assembly_route,
        read_type=selection.read_type,
        target_level=target_level,
        material_inputs=resolved_material_context(assembly_out),
        retry=retry_context,
    )


def _require_selection(raw: object) -> AssemblySelection:
    if raw is None:
        raise ValueError("assembly recommendation context requires assembly_selection")
    try:
        return validate_assembly_selection(raw)
    except ValueError as exc:
        raise ValueError(
            "assembly recommendation context has invalid assembly_selection"
        ) from exc


def _require_route(raw: object) -> AssemblyContextRoute:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "assembly recommendation context requires explicit assembly_route"
        )
    route = raw.strip().lower()
    if route not in _ASSEMBLY_ROUTES:
        raise ValueError(
            f"assembly recommendation context has unknown assembly_route {raw!r}"
        )
    return cast(AssemblyContextRoute, route)


def _require_target_level(raw: object) -> AssemblyTargetLevel:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "assembly recommendation context requires explicit target_level"
        )
    target_level = raw.strip().lower()
    if target_level not in _TARGET_LEVELS:
        raise ValueError(
            f"assembly recommendation context has unknown target_level {raw!r}"
        )
    return cast(AssemblyTargetLevel, target_level)
