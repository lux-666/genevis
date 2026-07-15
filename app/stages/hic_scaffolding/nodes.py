"""Hi-C scaffolding stage nodes for scaffold planning or explicit skip."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.policy import evaluate
from app.schemas.artifacts import (
    HIC_SCAFFOLD_PLAN,
    ArtifactMeta,
    HiCScaffoldDecisionArtifact,
    HiCScaffoldPlan,
    HiCSkipRecord,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.hic_scaffolding.method_application.lifecycle import hic_output as _hic_output
from app.stages.hic_scaffolding.method_application.lifecycle import update_hic_output as _update_hic_output

HIC_CANDIDATE_METHODS = [
    "hic_scaffolding.candidate.autohic",
]
HIC_CONTRACT_METHOD = "hic_scaffolding.contract"


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_hic_output(state, {"status": "failed", "reason": reason})


def enter_hic_scaffolding(state: TopLevelState) -> dict:
    """Mark Hi-C scaffolding as running."""
    return {"supervision_state": "running_stage"}


def resolve_hic_inputs(state: TopLevelState) -> dict:
    """Resolve reviewed assembly and optional Hi-C inputs."""
    from app.stages.base import resolve_current_assembly

    final_assembly_ref, source = resolve_current_assembly(
        state,
        prefer_stages=["assembly_review"]
    )
    if not final_assembly_ref:
        return _fail_update(state, "missing final_assembly_ref for hic_scaffolding")

    hic_assets = _hic_assets(state)
    target_level = _target_assembly_level(state)
    existing = _hic_output(state)
    return _update_hic_output(
        state,
        {
            **existing,
            "status": "resolved",
            "pre_hic_assembly_ref": final_assembly_ref,
            "final_assembly_for_annotation": final_assembly_ref,
            "final_assembly_for_downstream": final_assembly_ref,
            "hic_read_refs": [asset.get("asset_id", "") for asset in hic_assets if asset.get("asset_id")],
            "hic_read_paths": [str(asset.get("path")) for asset in hic_assets if asset.get("path")],
            "target_assembly_level": target_level,
            "assembly_source": source,  # Track provenance
        },
    )


def select_hic_mode(state: TopLevelState) -> dict:
    """Select scaffold or explicit skip mode."""
    hic_out = _hic_output(state)
    if hic_out.get("status") != "resolved":
        return {}
    if not (
        hic_out.get("hic_read_refs")
    ):
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
            "contact_map": str(work_dir / "contact_map.hic"),
            "assembly_file": str(work_dir / "chromosome.assembly"),
            "html_report": str(work_dir / "result.html"),
        },
        notes=_hic_plan_notes(hic_out),
        metadata={},
    )
    scaffold_plan_ref.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        scaffold_plan_ref,
        stage="hic_scaffolding",
        method_name=HIC_CONTRACT_METHOD,
        artifact_type=HIC_SCAFFOLD_PLAN,
        metadata={"mode": "scaffold", "execution_enabled": _local_method_enabled(hic_out)},
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
            "notes": plan.notes,
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
            "final_assembly_for_downstream": hic_out["pre_hic_assembly_ref"],
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
    candidates = dict(hic_out.get("method_candidates") or {})
    successful = [name for name, candidate in candidates.items() if candidate.get("status") == "executed"]
    decisions = ["accept_candidate", "skip", "stop_human_review"] if successful else ["select_method", "skip", "stop_human_review"]
    payload = ReviewPayload(
        stage="hic_scaffolding",
        reason="hic_scaffolding_human_decision_required",
        candidates=[{"method_name": method} for method in hic_out.get("candidate_methods", [])],
        decisions_allowed=decisions,
        notes=hic_out.get("notes", []),
        auto_decidable=False,
    )
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    request_ref = out_dir / "hic_scaffolding_review_request.json"
    request_ref.write_text(json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        request_ref,
        stage="hic_scaffolding",
        method_name=HIC_CONTRACT_METHOD,
        artifact_type="hic_scaffolding.request",
        metadata={"auto_decidable": False},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    outcome = evaluate("hic_scaffolding", payload, hic_out.get("review_policy"))
    pending_confirmation = outcome.action == "BLOCK_PENDING_CONFIRM"
    Ledger(data_dir).record_review_requested(
        session_id,
        "hic_scaffolding",
        reason="hic_scaffolding_human_decision_required",
        candidates=payload.candidates,
        review_request_artifact_ref=str(request_ref),
        auto_decidable=False,
        policy_mode=outcome.mode,
        pending_human_confirmation=pending_confirmation,
    )
    if outcome.action == "BLOCK_WITH_TIMEOUT":
        Ledger(data_dir).record_event(
            session_id,
            "blocked_with_timeout",
            {
                "stage": "hic_scaffolding",
                "max_blocked_seconds": outcome.max_blocked_seconds,
                "review_request_artifact_ref": str(request_ref),
                "policy_mode": outcome.mode,
            },
        )
    outputs = dict(state.get("stage_outputs") or {})
    outputs["hic_scaffolding"] = {
        **hic_out,
        "status": "blocked_human",
        "review_request_artifact_ref": str(request_ref),
        "review_policy_outcome": outcome.model_dump(mode="json"),
        "auto_decidable": False,
        "pending_human_confirmation": pending_confirmation,
    }
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "blocked_human",
        "stage_outputs": outputs,
        "active_interrupt": {
            "stage": "hic_scaffolding",
            "reason": "hic_scaffolding_human_decision_required",
            "candidate_methods": hic_out.get("candidate_methods", []),
            "decisions_allowed": payload.decisions_allowed,
            "auto_decidable": False,
            "pending_human_confirmation": pending_confirmation,
            "review_policy": hic_out.get("review_policy") or {"mode": outcome.mode},
            "review_request_artifact_ref": str(request_ref),
            "scaffold_plan_ref": hic_out.get("scaffold_plan_ref"),
            "pre_hic_assembly_ref": hic_out.get("pre_hic_assembly_ref"),
            "final_assembly_for_annotation": hic_out.get("final_assembly_for_annotation"),
            "chromosome_scaffolded_assembly_candidate_ref": hic_out.get("chromosome_scaffolded_assembly_candidate_ref"),
            "hic_review_bundle_ref": hic_out.get("hic_review_bundle_ref"),
            "method_candidates": candidates,
            "notes": hic_out.get("notes", []),
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def apply_hic_scaffolding_resume_decision(state: TopLevelState, request: ResumeReviewRequest) -> dict:
    """Apply a validated Hi-C scaffolding decision to a blocked state."""

    if request.stage != "hic_scaffolding":
        raise ResumeValidationError("Hi-C resume requires stage hic_scaffolding")
    hic_out = _hic_output(state)
    allowed = {"select_method", "accept_candidate", "skip", "stop_human_review"}
    if request.decision not in allowed:
        raise ResumeValidationError(f"unsupported hic_scaffolding decision: {request.decision}")
    if request.decision == "select_method":
        if not request.method_name:
            raise ResumeValidationError("hic_scaffolding select_method requires method_name")
        if request.method_name not in set(hic_out.get("candidate_methods") or []):
            raise ResumeValidationError(f"unknown hic_scaffolding method_name: {request.method_name}")
        if not request.notes and not request.metadata.get("contact_map_policy"):
            raise ResumeValidationError("hic_scaffolding select_method requires contact-map policy notes or metadata")
    elif request.decision == "accept_candidate":
        if not request.method_name:
            raise ResumeValidationError("hic_scaffolding accept_candidate requires method_name")
        candidate = dict((hic_out.get("method_candidates") or {}).get(request.method_name) or {})
        if candidate.get("status") != "executed" or not candidate.get("scaffold_fasta"):
            raise ResumeValidationError("hic_scaffolding accept_candidate requires an executed scaffold candidate")
    elif request.decision == "skip" and request.method_name:
        raise ResumeValidationError("hic_scaffolding skip decision must not include method_name")

    pre_hic_assembly_ref = hic_out.get("pre_hic_assembly_ref")
    final_assembly_for_annotation = hic_out.get("final_assembly_for_annotation") or pre_hic_assembly_ref
    if request.decision == "accept_candidate":
        final_assembly_for_annotation = str(hic_out["method_candidates"][request.method_name]["scaffold_fasta"])
    if not pre_hic_assembly_ref or not final_assembly_for_annotation:
        raise ResumeValidationError("hic_scaffolding resume requires pre_hic_assembly_ref")

    return _commit_hic_resume_decision(
        state,
        request,
        pre_hic_assembly_ref=str(pre_hic_assembly_ref),
        final_assembly_for_annotation=str(final_assembly_for_annotation),
    )


def _commit_hic_resume_decision(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    pre_hic_assembly_ref: str,
    final_assembly_for_annotation: str,
) -> dict:
    hic_out = _hic_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    decision_ref = out_dir / "hic_scaffold_decision.json"
    notes = list(request.notes)
    if request.decision == "select_method":
        notes.append("hic_scaffolding_execution_deferred_to_method_specific_task")
        notes.append("annotation_continues_on_pre_hic_assembly_fallback")
    skip_record_ref: str | None = None
    if request.decision == "skip":
        skip_record_ref = _write_human_hic_skip_record(
            state,
            pre_hic_assembly_ref=pre_hic_assembly_ref,
            final_assembly_for_annotation=final_assembly_for_annotation,
            hic_read_refs=hic_out.get("hic_read_refs", []),
            notes=notes,
            metadata=request.metadata,
        )

    artifact_payload = HiCScaffoldDecisionArtifact(
        session_id=session_id,
        decision=request.decision,
        method_name=request.method_name,
        scaffold_plan_ref=hic_out.get("scaffold_plan_ref"),
        pre_hic_assembly_ref=pre_hic_assembly_ref,
        final_assembly_for_annotation=final_assembly_for_annotation,
        hic_read_refs=hic_out.get("hic_read_refs", []),
        notes=notes,
        metadata=request.metadata,
    )
    decision_ref.write_text(json.dumps(artifact_payload.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        decision_ref,
        stage="hic_scaffolding",
        method_name=request.method_name or HIC_CONTRACT_METHOD,
        artifact_type="hic_scaffolding.decision",
        metadata={"decision": request.decision},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    Ledger(data_dir).record_review_decision(
        session_id,
        "hic_scaffolding",
        decision=request.decision,
        notes=notes,
        method_name=request.method_name,
        artifact_ref=str(artifact.file_path),
        metadata=request.metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
    )

    outputs = dict(state.get("stage_outputs") or {})
    outputs["hic_scaffolding"] = {
        **hic_out,
        "status": "failed_terminal" if request.decision == "stop_human_review" else "completed",
        "decision": request.decision,
        "selected_method_name": request.method_name,
        "pre_hic_assembly_ref": pre_hic_assembly_ref,
        "final_assembly_for_annotation": final_assembly_for_annotation,
        "final_assembly_for_downstream": final_assembly_for_annotation,
        "review_decision_artifact_ref": str(artifact.file_path),
        "notes": notes,
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
    }
    if skip_record_ref:
        outputs["hic_scaffolding"]["skip_record_ref"] = skip_record_ref
    if request.decision == "stop_human_review":
        return {
            "current_stage": "hic_scaffolding",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "stage_outputs": outputs,
            "last_error": "hic_scaffolding stopped by human review decision",
        }
    return {
        "current_stage": next_stage_name("hic_scaffolding"),
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": outputs,
        "route_trace": [*state.get("route_trace", []), "hic_scaffolding_stage"],
    }


def _write_human_hic_skip_record(
    state: TopLevelState,
    *,
    pre_hic_assembly_ref: str,
    final_assembly_for_annotation: str,
    hic_read_refs: list[str],
    notes: list[str],
    metadata: dict[str, Any],
) -> str:
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    skip_record_ref = out_dir / "hic_skip_record.json"
    record = HiCSkipRecord(
        session_id=session_id,
        mode="human_review_skip",
        skip_reason="human_review_skip",
        pre_hic_assembly_ref=pre_hic_assembly_ref,
        final_assembly_for_annotation=final_assembly_for_annotation,
        hic_read_refs=hic_read_refs,
        notes=notes,
        metadata=metadata,
    )
    skip_record_ref.write_text(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        skip_record_ref,
        stage="hic_scaffolding",
        method_name=HIC_CONTRACT_METHOD,
        artifact_type="hic_scaffolding.skip",
        metadata={"mode": "human_review_skip", "skip_reason": "human_review_skip"},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return str(skip_record_ref)


def fail_hic_scaffolding(state: TopLevelState) -> dict:
    """Stop on invalid Hi-C scaffolding inputs or preflight block."""
    hic_out = _hic_output(state)
    status = hic_out.get("status", "failed")
    default_reason = {
        "blocked_env": "hic_scaffolding blocked on missing conda environment",
        "missing_tool": "hic_scaffolding blocked on missing tool",
    }.get(status, "hic_scaffolding failed")
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "failed_terminal",
        "last_error": hic_out.get("reason", default_reason),
    }


def _hic_plan_notes(hic_out: dict[str, Any]) -> list[str]:
    notes = [
        "autohic_contact_map_review_required",
        "pre_hic_assembly_preserved_as_annotation_fallback",
    ]
    method = hic_out.get("primary_method") or hic_out.get("selected_method_name")
    if method == "hic_scaffolding.candidate.autohic" and hic_out.get("execution_mode") == "local":
        notes.append("autohic_execution_requested")
    else:
        notes.append("execution_deferred_to_method_specific_task")
    return notes


def _local_method_enabled(hic_out: dict[str, Any]) -> bool:
    method = hic_out.get("primary_method") or hic_out.get("selected_method_name")
    return hic_out.get("execution_mode") == "local" and method in set(HIC_CANDIDATE_METHODS)




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
