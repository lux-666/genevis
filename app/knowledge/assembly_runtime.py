"""Read the configured live case directory for each new recommendation."""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.domains.assembly.material_query import build_material_query
from app.domains.assembly.software_knowledge import capture_software_knowledge
from app.knowledge.assembly_cases import AssemblyKnowledge, load_case_papers
from app.schemas.artifacts import ArtifactRef
from app.stages.base import absolute_data_dir
from app.storage.artifacts import resolve_artifact_ref


def load_knowledge(root: str | None = None) -> AssemblyKnowledge:
    return AssemblyKnowledge(
        papers=load_case_papers(root), software=capture_software_knowledge()
    )


def current_knowledge(state: Any) -> AssemblyKnowledge:
    seed = (state.get("stage_seeds") or {}).get("assembly") or {}
    return load_knowledge(seed.get("case_source"))


def initial_material_query(state: Any, assembly: Mapping[str, Any]) -> dict[str, Any]:
    request_ref = assembly.get("assembly_method_model_request_ref")
    if request_ref:
        path = resolve_artifact_ref(
            absolute_data_dir(state),
            state["session_id"],
            ArtifactRef.model_validate(request_ref),
        )
        body = json.loads(path.read_text())
        query = body.get("material_query")
        if not isinstance(query, dict):
            raise ValueError(
                "pre-v5 method evidence cannot seed a parameter recommendation"
            )
        return query
    return build_material_query(state, assembly)
