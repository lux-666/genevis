"""Hi-C scaffolding stage nodes for scaffold planning or explicit skip."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.schemas.artifacts import ArtifactMeta, HiCScaffoldPlan, HiCSkipRecord
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState

HIC_CANDIDATE_METHODS = [
    "hic_scaffolding.candidate.yahs",
    "hic_scaffolding.candidate.juicer_3ddna_juicebox",
]
HIC_CONTRACT_METHOD = "hic_scaffolding.contract"


def _hic_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("hic_scaffolding", {}))


def _update_hic_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["hic_scaffolding"] = {**_hic_output(state), **payload}
    return {"stage_outputs": outputs}


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_hic_output(state, {"status": "failed", "reason": reason})


def enter_hic_scaffolding(state: TopLevelState) -> dict:
    """Mark Hi-C scaffolding as running."""
    return {"supervision_state": "running_stage"}


def resolve_hic_inputs(state: TopLevelState) -> dict:
    """Resolve reviewed assembly and optional Hi-C inputs."""
    final_assembly_ref = _final_assembly_ref(state)
    if not final_assembly_ref:
        return _fail_update(state, "missing final_assembly_ref for hic_scaffolding")

    hic_assets = _hic_assets(state)
    target_level = _target_assembly_level(state)
    return _update_hic_output(
        state,
        {
            "status": "resolved",
            "pre_hic_assembly_ref": final_assembly_ref,
            "final_assembly_for_annotation": final_assembly_ref,
            "hic_read_refs": [asset.get("asset_id", "") for asset in hic_assets if asset.get("asset_id")],
            "hic_read_paths": [str(asset.get("path")) for asset in hic_assets if asset.get("path")],
            "target_assembly_level": target_level,
        },
    )


def select_hic_mode(state: TopLevelState) -> dict:
    """Select scaffold or explicit skip mode."""
    hic_out = _hic_output(state)
    if hic_out.get("status") != "resolved":
        return {}
    if not hic_out.get("hic_read_refs"):
        mode = "skipped_no_hic"
    elif hic_out.get("target_assembly_level") != "chromosome":
        mode = "skipped_not_chromosome_level"
    else:
        mode = "scaffold"
    return _update_hic_output(state, {"status": "selected", "mode": mode})


def plan_hic_scaffold(state: TopLevelState) -> dict:
    """Plan Hi-C scaffolding candidates and block for human method choice."""
    hic_out = _hic_output(state)
    if hic_out.get("mode") != "scaffold":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    missing = [method for method in HIC_CANDIDATE_METHODS if method not in registry.methods]
    if missing:
        return _fail_update(state, f"hic_scaffolding descriptors missing: {missing}")

    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    out_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id
    work_dir = data_dir / "work" / "hic_scaffolding" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    scaffold_plan_ref = out_dir / "hic_scaffold_plan.json"
    plan = HiCScaffoldPlan(
        session_id=session_id,
        status="planned",
        pre_hic_assembly_ref=hic_out["pre_hic_assembly_ref"],
        fallback_assembly_ref=hic_out["pre_hic_assembly_ref"],
        hic_read_refs=hic_out.get("hic_read_refs", []),
        candidate_methods=HIC_CANDIDATE_METHODS,
        expected_outputs={
            "hic_qc_report": str(work_dir / "hic_qc_report.json"),
            "chromosome_scaffolded_assembly": str(work_dir / "chromosome_scaffolded_assembly.fasta"),
            "juicebox_review_bundle": str(work_dir / "juicebox_review_bundle"),
        },
        notes=[
            "execution_deferred_to_method_specific_task",
            "contact_map_policy_required",
            "pre_hic_assembly_preserved_as_annotation_fallback",
        ],
    )
    scaffold_plan_ref.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        scaffold_plan_ref,
        stage="hic_scaffolding",
        method_name=HIC_CONTRACT_METHOD,
        artifact_type="hic_scaffolding.plan",
        metadata={"mode": "scaffold", "execution_enabled": False},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return _update_hic_output(
        state,
        {
            "status": "blocked_human",
            "mode": "scaffold",
            "candidate_methods": HIC_CANDIDATE_METHODS,
            "scaffold_plan_ref": str(scaffold_plan_ref),
            "artifact_refs": [str(artifact.file_path)],
        },
    )


def write_hic_skip(state: TopLevelState) -> dict:
    """Write explicit non-blocking Hi-C skip artifact."""
    hic_out = _hic_output(state)
    mode = hic_out.get("mode")
    if mode not in {"skipped_no_hic", "skipped_not_chromosome_level"}:
        return {}

    skip_reason = "no_hic_reads" if mode == "skipped_no_hic" else "not_chromosome_level_requested"
    record = HiCSkipRecord(
        session_id=state["session_id"],
        mode=mode,
        skip_reason=skip_reason,
        pre_hic_assembly_ref=hic_out["pre_hic_assembly_ref"],
        final_assembly_for_annotation=hic_out["pre_hic_assembly_ref"],
        hic_read_refs=hic_out.get("hic_read_refs", []),
        notes=["annotation_continues_on_pre_hic_assembly"],
        metadata={"target_assembly_level": hic_out.get("target_assembly_level")},
    )
    data_dir = Path(state.get("data_dir", "data"))
    out_dir = data_dir / "artifacts" / "hic_scaffolding" / state["session_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    skip_record_ref = out_dir / "hic_skip_record.json"
    skip_record_ref.write_text(
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        skip_record_ref,
        stage="hic_scaffolding",
        method_name=HIC_CONTRACT_METHOD,
        artifact_type="hic_scaffolding.skip",
        metadata={"mode": mode, "skip_reason": skip_reason},
    )
    append_artifact_records(data_dir, state["session_id"], [artifact])
    update = _update_hic_output(
        state,
        {
            "status": "skipped",
            "mode": mode,
            "skip_reason": skip_reason,
            "pre_hic_assembly_ref": hic_out["pre_hic_assembly_ref"],
            "final_assembly_for_annotation": hic_out["pre_hic_assembly_ref"],
            "skip_record_ref": str(skip_record_ref),
            "artifact_refs": [str(artifact.file_path)],
        },
    )
    update["skip_reasons"] = {
        **(state.get("skip_reasons") or {}),
        "hic_scaffolding": skip_reason,
    }
    return update


def commit_hic_scaffolding(state: TopLevelState) -> dict:
    """Advance after a Hi-C skip path."""
    trace = [*state.get("route_trace", []), "hic_scaffolding_stage"]
    return {
        "current_stage": next_stage_name("hic_scaffolding"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def block_hic_scaffolding(state: TopLevelState) -> dict:
    """Pause for human Hi-C scaffolding method/contact-map decision."""
    hic_out = _hic_output(state)
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "blocked_human",
        "active_interrupt": {
            "stage": "hic_scaffolding",
            "reason": "hic_scaffolding_human_decision_required",
            "candidate_methods": hic_out.get("candidate_methods", []),
            "scaffold_plan_ref": hic_out.get("scaffold_plan_ref"),
            "pre_hic_assembly_ref": hic_out.get("pre_hic_assembly_ref"),
            "final_assembly_for_annotation": hic_out.get("final_assembly_for_annotation"),
        },
    }


def fail_hic_scaffolding(state: TopLevelState) -> dict:
    """Stop on invalid Hi-C scaffolding inputs."""
    hic_out = _hic_output(state)
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "failed_terminal",
        "last_error": hic_out.get("reason", "hic_scaffolding failed"),
    }


def _final_assembly_ref(state: TopLevelState) -> str | None:
    review_out = (state.get("stage_outputs") or {}).get("assembly_review") or {}
    for key in ["final_assembly_for_scaffolding_or_annotation", "final_assembly_ref"]:
        value = review_out.get(key)
        if value:
            return str(value)
    return None


def _target_assembly_level(state: TopLevelState) -> str:
    inventory_out = (state.get("stage_outputs") or {}).get("inventory") or {}
    return str(inventory_out.get("target_assembly_level") or "contig")


def _hic_assets(state: TopLevelState) -> list[dict[str, Any]]:
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return []
    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return []
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return [
        asset
        for asset in registry.get("assets", [])
        if asset.get("asset_type") == "hic_reads_registered"
    ]
