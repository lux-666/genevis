"""Repeat annotation stage nodes for planning repeat library and masking contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.schemas.artifacts import ArtifactMeta, RepeatAnnotationBundle, RepeatAnnotationPlan
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState

REPEAT_LIBRARY_CANDIDATES = [
    "repeat_annotation.library.edta",
    "repeat_annotation.library.repeatmodeler",
]
REPEAT_HELPER_METHODS = ["repeat_annotation.helper.trf"]
REPEAT_MASKING_METHOD = "repeat_annotation.masking.repeatmasker"
REPEAT_CONTRACT_METHOD = "repeat_annotation.contract"


def _repeat_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("repeat_annotation", {}))


def _update_repeat_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["repeat_annotation"] = {**_repeat_output(state), **payload}
    return {"stage_outputs": outputs}


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_repeat_output(state, {"status": "failed", "reason": reason})


def enter_repeat_annotation(state: TopLevelState) -> dict:
    """Mark repeat annotation as running."""
    return {"supervision_state": "running_stage"}


def resolve_repeat_inputs(state: TopLevelState) -> dict:
    """Resolve assembly and optional provided repeat library inputs."""
    final_assembly_ref = _final_assembly_for_annotation(state)
    if not final_assembly_ref:
        return _fail_update(state, "missing final_assembly_for_annotation for repeat_annotation")

    repeat_libraries = _repeat_library_assets(state)
    confidence, low_confidence_reasons = _repeat_confidence(state)
    return _update_repeat_output(
        state,
        {
            "status": "resolved",
            "final_assembly_for_annotation": final_assembly_ref,
            "provided_repeat_library_refs": [asset.get("asset_id", "") for asset in repeat_libraries if asset.get("asset_id")],
            "provided_repeat_library_paths": [str(asset.get("path")) for asset in repeat_libraries if asset.get("path")],
            "confidence": confidence,
            "low_confidence_reasons": low_confidence_reasons,
        },
    )


def select_repeat_strategy(state: TopLevelState) -> dict:
    """Select provided-library or de novo repeat annotation planning."""
    repeat_out = _repeat_output(state)
    if repeat_out.get("status") != "resolved":
        return {}

    if repeat_out.get("provided_repeat_library_paths"):
        strategy = "provided"
    else:
        strategy = "de_novo"
    return _update_repeat_output(
        state,
        {
            "status": "strategy_selected",
            "strategy": strategy,
            "repeat_library_source": strategy,
        },
    )


def plan_repeat_library(state: TopLevelState) -> dict:
    """Plan repeat library discovery or record provided library use."""
    repeat_out = _repeat_output(state)
    if repeat_out.get("status") != "strategy_selected":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    strategy = repeat_out["strategy"]
    if strategy == "provided":
        library_ref = repeat_out["provided_repeat_library_paths"][0]
        return _update_repeat_output(
            state,
            {
                "status": "library_planned",
                "repeat_library_ref": library_ref,
                "library_candidate_methods": [],
                "helper_methods": [],
                "method_selection_required": False,
                "notes": ["provided_repeat_library_skips_de_novo_discovery"],
            },
        )

    missing = [method for method in [*REPEAT_LIBRARY_CANDIDATES, *REPEAT_HELPER_METHODS] if method not in registry.methods]
    if missing:
        return _fail_update(state, f"repeat_annotation descriptors missing: {missing}")

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    library_ref = data_dir / "work" / "repeat_annotation" / session_id / "repeat_library.fa"
    return _update_repeat_output(
        state,
        {
            "status": "library_planned",
            "repeat_library_ref": str(library_ref),
            "library_candidate_methods": REPEAT_LIBRARY_CANDIDATES,
            "helper_methods": REPEAT_HELPER_METHODS,
            "method_selection_required": True,
            "notes": [
                "execution_deferred_to_method_specific_task",
                "repeat_library_method_selection_requires_descriptor_or_human_policy",
            ],
        },
    )


def plan_repeat_masking(state: TopLevelState) -> dict:
    """Plan RepeatMasker masking outputs and write the repeat plan artifact."""
    repeat_out = _repeat_output(state)
    if repeat_out.get("status") != "library_planned":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    if REPEAT_MASKING_METHOD not in registry.methods:
        return _fail_update(state, f"repeat_annotation descriptors missing: ['{REPEAT_MASKING_METHOD}']")

    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "repeat_annotation" / session_id
    work_dir = data_dir / "work" / "repeat_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)

    expected_outputs = {
        "repeat_library": repeat_out["repeat_library_ref"],
        "repeat_annotation_gff3": str(work_dir / "repeat_annotation.gff3"),
        "softmasked_genome": str(work_dir / "softmasked_genome.fasta"),
        "hardmasked_genome": str(work_dir / "hardmasked_genome.fasta"),
    }
    candidate_methods = [
        *repeat_out.get("library_candidate_methods", []),
        *repeat_out.get("helper_methods", []),
        REPEAT_MASKING_METHOD,
    ]
    notes = [*repeat_out.get("notes", []), "softmasked_genome_is_default_gene_annotation_input"]
    provided_paths = repeat_out.get("provided_repeat_library_paths", [])
    provided_repeat_library_ref = provided_paths[0] if provided_paths else None
    plan = RepeatAnnotationPlan(
        session_id=session_id,
        status="planned",
        strategy=repeat_out["strategy"],
        repeat_library_source=repeat_out["repeat_library_source"],
        final_assembly_for_annotation=repeat_out["final_assembly_for_annotation"],
        provided_repeat_library_ref=provided_repeat_library_ref,
        library_candidate_methods=repeat_out.get("library_candidate_methods", []),
        helper_methods=repeat_out.get("helper_methods", []),
        masking_method=REPEAT_MASKING_METHOD,
        expected_outputs=expected_outputs,
        confidence=repeat_out.get("confidence", "high"),
        low_confidence_reasons=repeat_out.get("low_confidence_reasons", []),
        method_selection_required=bool(repeat_out.get("method_selection_required")),
        notes=notes,
        metadata={"execution_enabled": False},
    )
    plan_ref = out_dir / "repeat_annotation_plan.json"
    plan_ref.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        plan_ref,
        stage="repeat_annotation",
        method_name=REPEAT_CONTRACT_METHOD,
        artifact_type="repeat_annotation.plan",
        metadata={
            "strategy": repeat_out["strategy"],
            "repeat_library_source": repeat_out["repeat_library_source"],
            "confidence": repeat_out.get("confidence", "high"),
            "execution_enabled": False,
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return _update_repeat_output(
        state,
        {
            "status": "masking_planned",
            "masking_method": REPEAT_MASKING_METHOD,
            "candidate_methods": candidate_methods,
            "expected_outputs": expected_outputs,
            "repeat_annotation_gff3_ref": expected_outputs["repeat_annotation_gff3"],
            "softmasked_genome_ref": expected_outputs["softmasked_genome"],
            "hardmasked_genome_ref": expected_outputs["hardmasked_genome"],
            "plan_artifact_ref": str(plan_ref),
            "artifact_refs": [str(artifact.file_path)],
            "notes": notes,
        },
    )


def commit_repeat_annotation(state: TopLevelState) -> dict:
    """Publish the repeat annotation bundle and advance to gene annotation."""
    repeat_out = _repeat_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "repeat_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle = RepeatAnnotationBundle(
        session_id=session_id,
        status="planned",
        strategy=repeat_out["strategy"],
        repeat_library_source=repeat_out["repeat_library_source"],
        final_assembly_for_annotation=repeat_out["final_assembly_for_annotation"],
        repeat_library_ref=repeat_out["repeat_library_ref"],
        repeat_annotation_gff3_ref=repeat_out["repeat_annotation_gff3_ref"],
        softmasked_genome_ref=repeat_out["softmasked_genome_ref"],
        hardmasked_genome_ref=repeat_out.get("hardmasked_genome_ref"),
        confidence=repeat_out.get("confidence", "high"),
        low_confidence_reasons=repeat_out.get("low_confidence_reasons", []),
        candidate_methods=repeat_out.get("candidate_methods", []),
        plan_artifact_ref=repeat_out.get("plan_artifact_ref"),
        notes=repeat_out.get("notes", []),
        metadata={"execution_enabled": False},
    )
    bundle_ref = out_dir / "repeat_annotation_bundle.json"
    bundle_ref.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        bundle_ref,
        stage="repeat_annotation",
        method_name=REPEAT_CONTRACT_METHOD,
        artifact_type="repeat_annotation.bundle",
        metadata={
            "strategy": repeat_out["strategy"],
            "repeat_library_source": repeat_out["repeat_library_source"],
            "confidence": repeat_out.get("confidence", "high"),
            "execution_enabled": False,
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])

    outputs = dict(state.get("stage_outputs") or {})
    outputs["repeat_annotation"] = {
        **repeat_out,
        "status": "completed",
        "repeat_annotation": repeat_out["repeat_annotation_gff3_ref"],
        "softmasked_genome": repeat_out["softmasked_genome_ref"],
        "bundle_artifact_ref": str(bundle_ref),
        "artifact_refs": [*repeat_out.get("artifact_refs", []), str(artifact.file_path)],
    }
    trace = [*state.get("route_trace", []), "repeat_annotation_stage"]
    return {
        "stage_outputs": outputs,
        "current_stage": next_stage_name("repeat_annotation"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def fail_repeat_annotation(state: TopLevelState) -> dict:
    """Stop on invalid repeat annotation inputs or descriptors."""
    repeat_out = _repeat_output(state)
    return {
        "current_stage": "repeat_annotation",
        "supervision_state": "failed_terminal",
        "last_error": repeat_out.get("reason", "repeat_annotation failed"),
    }


def _final_assembly_for_annotation(state: TopLevelState) -> str | None:
    outputs = state.get("stage_outputs") or {}
    hic_out = outputs.get("hic_scaffolding") or {}
    value = hic_out.get("final_assembly_for_annotation")
    if value:
        return str(value)

    review_out = outputs.get("assembly_review") or {}
    for key in ["final_assembly_for_scaffolding_or_annotation", "final_assembly_ref"]:
        value = review_out.get(key)
        if value:
            return str(value)
    return None


def _repeat_library_assets(state: TopLevelState) -> list[dict[str, Any]]:
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return []
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return []
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return [asset for asset in registry.get("assets", []) if asset.get("asset_type") == "repeat_library"]


def _repeat_confidence(state: TopLevelState) -> tuple[str, list[str]]:
    review_out = (state.get("stage_outputs") or {}).get("assembly_review") or {}
    reasons: list[str] = []
    for reason in review_out.get("low_confidence_reasons", []) or []:
        reasons.append(str(reason))

    metadata = dict(review_out.get("metadata") or {})
    fragmentation = metadata.get("assembly_fragmentation") or review_out.get("assembly_fragmentation")
    if str(fragmentation).lower() in {"high", "fragmented", "low_confidence"}:
        if "fragmented_assembly" not in reasons:
            reasons.append("fragmented_assembly")

    decision = review_out.get("decision") or review_out.get("review_decision")
    if decision == "continue_low_confidence" and "assembly_review_continue_low_confidence" not in reasons:
        reasons.append("assembly_review_continue_low_confidence")

    confidence = "low" if reasons or review_out.get("confidence") == "low" else "high"
    return confidence, reasons
