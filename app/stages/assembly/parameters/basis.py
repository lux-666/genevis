"""Resolve Assembly parameter knowledge basis and retry normalization inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domains.assembly.case_selector import select_evidence_cases
from app.domains.assembly.method_guidance import load_method_guidance
from app.knowledge.packs import load_knowledge_pack
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.stages.assembly.parameters.profile import build_prompt_profile


def build_parameter_basis_snapshot(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    full_profile: dict[str, Any],
    knowledge_pack_id: str = "parameter_recommender",
    knowledge_root: Path | None = None,
) -> dict[str, Any]:
    """Select the full immutable knowledge basis for an initial recommendation."""

    if method_id != catalog.method_id:
        raise ValueError(
            f"parameter basis method {method_id!r} does not match catalog {catalog.method_id!r}"
        )
    prompt_profile = build_prompt_profile(full_profile, "recommend_initial")
    knowledge_pack = load_knowledge_pack(knowledge_pack_id, root=knowledge_root)
    literature_cases, literature_selection = select_evidence_cases(
        knowledge_pack["cases"],
        method_id=method_id,
        task="recommend_initial",
        prompt_profile=prompt_profile,
        strategy=knowledge_pack.get("strategies"),
        source_root=knowledge_pack.get("pack_root"),
    )
    return {
        "schema_version": 2,
        "method_id": catalog.method_id,
        "catalog_snapshot": catalog.model_dump(mode="json"),
        "literature_selection": dict(literature_selection),
        "literature_cases": list(literature_cases),
        "method_guidance": _method_guidance(method_id),
    }


def _method_guidance(method_id: str) -> list[dict[str, Any]]:
    if method_id not in {"hifiasm", "hifiasm_ont"}:
        return []
    return load_method_guidance().model_cards_for_method(method_id)
