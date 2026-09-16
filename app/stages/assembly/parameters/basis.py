"""Freeze the confirmed method's direct and cross-method recommendation basis."""

from __future__ import annotations

from typing import Any

from app.knowledge.assembly_cases import KnowledgeSnapshot, snapshot_hash
from app.knowledge.assembly_retrieval import retrieve_assembly_cases
from app.knowledge.assembly_runtime import (
    capture_knowledge,
    frozen_knowledge,
    initial_material_query,
)
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
)


def build_parameter_basis_snapshot(
    *,
    method_id: str,
    catalog: MethodParameterCatalog,
    facts: CanonicalRecommendationFacts,
    context: AssemblyRecommendationContext,
    state: Any = None,
    knowledge: KnowledgeSnapshot | None = None,
    material_query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if method_id != catalog.method_id:
        raise ValueError("parameter basis method does not match catalog")
    snapshot = knowledge or (
        frozen_knowledge(state) if state is not None else capture_knowledge()
    )
    assembly = (
        ((state.get("stage_outputs") or {}).get("assembly") or {}) if state else {}
    )
    query = material_query or (
        initial_material_query(state, assembly)
        if state is not None
        else {
            "text": facts.species.scientific_name or "",
            "platforms": [],
            "observations": [],
            "target_level": context.target_level,
        }
    )
    retrieved = retrieve_assembly_cases(
        snapshot, query, purpose="parameters", selected_method=method_id
    )
    selection = assembly.get("assembly_selection") or {}
    return {
        "schema_version": 3,
        "consumer_version": 5,
        "method_id": method_id,
        "catalog_snapshot": catalog.model_dump(mode="json"),
        "knowledge_sha256": snapshot_hash(snapshot),
        "knowledge_evidence": retrieved["evidence"],
        "retrieval_trace": retrieved["trace"],
        "material_query": query,
        "method_decision": {
            "method_id": method_id,
            "rationale": selection.get("rationale", ""),
        },
        "method_guidance": snapshot.software.get(method_id, {}).get("guidance", []),
    }
