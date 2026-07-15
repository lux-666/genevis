"""Canonical genome-size resolution shared by survey and assembly."""

from __future__ import annotations

from typing import Any, Mapping, TypedDict

from app.schemas.state import TopLevelState


class ResolvedGenomeSize(TypedDict):
    """One positive genome-size value with deterministic provenance."""

    value_bp: int
    source: str
    source_path: str


def resolve_explicit_genome_size(state: TopLevelState) -> ResolvedGenomeSize | None:
    """Resolve only operator-provided values, never a survey measurement."""

    assembly = _mapping((state.get("stage_outputs") or {}).get("assembly"))
    candidates = (
        (
            "assembly.flye_parameters.estimated_genome_size",
            _mapping(assembly.get("flye_parameters")).get("estimated_genome_size"),
        ),
        (
            "assembly.nextdenovo_parameters.genome_size",
            _mapping(assembly.get("nextdenovo_parameters")).get("genome_size"),
        ),
        (
            "shared_parameters.estimated_genome_size",
            _mapping(state.get("shared_parameters")).get("estimated_genome_size"),
        ),
        (
            "inventory.genome_size_estimate",
            _mapping((state.get("stage_outputs") or {}).get("inventory")).get("genome_size_estimate"),
        ),
    )
    for source_path, value in candidates:
        resolved = _resolved(value, "provided", source_path)
        if resolved:
            return resolved
    return None


def resolve_genome_size(state: TopLevelState) -> ResolvedGenomeSize | None:
    """Resolve the canonical provided-or-survey genome-size handoff."""

    explicit = resolve_explicit_genome_size(state)
    if explicit:
        return explicit

    survey = _mapping((state.get("stage_outputs") or {}).get("genome_survey"))
    if survey.get("genome_size_source") == "survey":
        return _resolved(survey.get("genome_size_prior"), "survey", "genome_survey.genome_size_prior")
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    return value if isinstance(value, Mapping) else {}


def _resolved(value: Any, source: str, source_path: str) -> ResolvedGenomeSize | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value_bp = int(value)
    except (TypeError, ValueError):
        return None
    if value_bp <= 0:
        return None
    return {"value_bp": value_bp, "source": source, "source_path": source_path}
