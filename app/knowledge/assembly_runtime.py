"""Assembly knowledge access: frozen run contents, never live case fallback."""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.domains.assembly.material_query import build_material_query
from app.domains.assembly.software_knowledge import capture_software_knowledge
from app.knowledge.assembly_cases import KnowledgeSnapshot, load_case_papers
from app.schemas.artifacts import ArtifactRef
from app.stages.base import absolute_data_dir, session_paths
from app.storage.artifacts import resolve_artifact_ref


def capture_knowledge(root: str | None = None) -> KnowledgeSnapshot:
    return KnowledgeSnapshot(
        papers=load_case_papers(root), software=capture_software_knowledge()
    )


def frozen_knowledge(state: Any) -> KnowledgeSnapshot:
    if not state.get("data_dir"):
        return capture_knowledge()
    paths = session_paths(state)
    if paths.frozen_inputs_path.exists():
        from app.storage.frozen_inputs import validate_frozen_inputs

        bundle = validate_frozen_inputs(paths)
        if "assembly_knowledge" not in bundle.marker.files:
            raise ValueError(
                "Assembly v5 requires a new frozen run with assembly knowledge"
            )
        return KnowledgeSnapshot.model_validate_json(
            paths.assembly_knowledge_path.read_bytes()
        )
    if ((state.get("stage_seeds") or {}).get("assembly") or {}).get("case_source"):
        raise ValueError("case consumption requires frozen session inputs")
    # Standalone domain diagnostics have no run to freeze; no literature is read.
    return capture_knowledge()


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
