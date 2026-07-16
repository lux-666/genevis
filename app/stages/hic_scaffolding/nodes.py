"""Nodes for the selected, AutoHiC-only Hi-C scaffolding stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.graph.routing import next_stage_name
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import ArtifactRef, HiCScaffoldDecisionArtifact
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir
from app.stages.hic_scaffolding.method_application.lifecycle import (
    hic_output as _hic_output,
)
from app.stages.hic_scaffolding.method_application.lifecycle import (
    update_hic_output as _update_hic_output,
)


AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"
HIC_CONTRACT_METHOD = "hic_scaffolding.contract"


def enter_hic_scaffolding(state: TopLevelState) -> dict:
    """Mark the selected Hi-C stage as running."""

    return {"supervision_state": "running_stage"}


def resolve_hic_inputs(state: TopLevelState) -> dict:
    """Resolve and validate everything required before recommending parameters."""

    from app.stages.base import resolve_current_assembly

    final_assembly_ref, source = resolve_current_assembly(
        state,
        prefer_stages=["assembly_review"],
    )
    hic_assets = _hic_assets(state)
    hic_read_refs = [
        str(asset.get("asset_id")) for asset in hic_assets if asset.get("asset_id")
    ]
    hic_read_paths = [
        str(asset.get("path")) for asset in hic_assets if asset.get("path")
    ]
    existing = _hic_output(state)
    resolved = {
        **existing,
        "pre_hic_assembly_ref": final_assembly_ref,
        "final_assembly_for_annotation": final_assembly_ref,
        "final_assembly_for_downstream": final_assembly_ref,
        "hic_read_refs": hic_read_refs,
        "hic_read_paths": hic_read_paths,
        "hic_library_metadata": [
            dict(asset.get("metadata") or {}) for asset in hic_assets
            if asset.get("metadata")
        ],
        "target_assembly_level": _target_assembly_level(state),
        "assembly_source": source,
        "method_name": AUTOHIC_METHOD,
    }

    missing: list[str] = []
    if not final_assembly_ref:
        missing.append("pre_hic_assembly_ref")
    elif not Path(str(final_assembly_ref)).is_file():
        missing.append("materialized pre_hic_assembly_ref")
    if len(hic_read_paths) < 2 or len(hic_read_paths) % 2:
        missing.append("paired hic_read_paths (R1/R2)")
    else:
        missing.extend(
            f"materialized hic_read_path: {path}"
            for path in hic_read_paths
            if not Path(path).is_file()
        )
    if existing.get("execution_mode") != "local":
        missing.append("hic_scaffolding local execution")
    try:
        descriptor_present = (
            AUTOHIC_METHOD
            in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods
        )
    except (OSError, ValueError):
        descriptor_present = False
    if not descriptor_present:
        missing.append(f"descriptor: {AUTOHIC_METHOD}")

    if missing:
        return _update_hic_output(
            state,
            {
                **resolved,
                "status": "blocked_missing_requirements",
                "missing_requirements": list(dict.fromkeys(missing)),
                "reason": "AutoHiC requirements are incomplete: "
                + ", ".join(dict.fromkeys(missing)),
            },
        )
    return _update_hic_output(
        state,
        {
            **resolved,
            "status": "resolved",
            "missing_requirements": [],
        },
    )


def prepare_autohic_adoption_review(state: TopLevelState) -> dict:
    """Open the separate final gate that adopts or rejects the AutoHiC result."""

    hic_out = _hic_output(state)
    if hic_out.get("status") != "autohic_review_ready":
        return {}
    candidate = dict((hic_out.get("method_candidates") or {}).get(AUTOHIC_METHOD) or {})
    if candidate.get("status") != "executed" or not candidate.get("scaffold_fasta"):
        return _fail_update(
            state,
            "AutoHiC completed without a reviewable chromosome assembly",
            failure_kind="execution",
        )

    payload = ReviewPayload(
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason="autohic_adoption_review_required",
        candidates=[
            {
                "method_name": AUTOHIC_METHOD,
                "scaffold_fasta": candidate.get("scaffold_fasta"),
                "contact_map": candidate.get("contact_map"),
                "assembly_file": candidate.get("assembly_file"),
                "html_report": candidate.get("html_report"),
            }
        ],
        decisions_allowed=[
            "accept_candidate",
            "keep_prior_assembly",
            "stop_human_review",
        ],
        notes=list(hic_out.get("notes") or []),
        auto_decidable=False,
        report_refs={
            key: str(candidate[key])
            for key in ("contact_map", "assembly_file", "html_report")
            if candidate.get(key)
        },
    )
    persisted = persist_review_request(
        state,
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason="autohic_adoption_review_required",
        payload=payload,
        method_name=HIC_CONTRACT_METHOD,
        artifact_type="hic_scaffolding.adoption_review.request",
    )
    interrupt = persisted.compact_interrupt(
        stage="hic_scaffolding",
        gate_id="hic_scaffolding",
        reason="autohic_adoption_review_required",
    )
    Ledger(absolute_data_dir(state)).record_review_requested(
        state["session_id"],
        "hic_scaffolding",
        reason="autohic_adoption_review_required",
        candidates=payload.candidates,
        review_request_artifact_ref=persisted.request_ref.path_ref,
        review_request_id=persisted.review_request_id,
        auto_decidable=False,
        pending_human_confirmation=True,
    )
    return _update_hic_output(
        state,
        {
            "status": "blocked_human",
            "block_reason": "autohic_adoption_review_required",
            "active_interrupt": interrupt,
            "review_request_artifact_ref": persisted.request_ref.model_dump(
                mode="json"
            ),
        },
    )


def block_hic_scaffolding(state: TopLevelState) -> dict:
    """Surface an already-persisted parameter or adoption review interrupt."""

    hic_out = _hic_output(state)
    interrupt = dict(hic_out.get("active_interrupt") or {})
    if not interrupt:
        update = _fail_update(
            state,
            "Hi-C review block has no persisted interrupt",
            failure_kind="review",
        )
        return {
            **update,
            "current_stage": "hic_scaffolding",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            "last_error": "Hi-C review block has no persisted interrupt",
        }
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "blocked_human",
        "active_interrupt": interrupt,
    }


def block_hic_requirements(state: TopLevelState) -> dict:
    """Stop the selected stage with a distinct missing-requirements outcome."""

    hic_out = _hic_output(state)
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "failed_terminal",
        "active_interrupt": None,
        "last_error": hic_out.get("reason", "AutoHiC requirements are incomplete"),
    }


def apply_hic_scaffolding_resume_decision(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> dict:
    """Apply the final human adoption decision for one completed AutoHiC run."""

    if request.stage != "hic_scaffolding" or request.gate_id != "hic_scaffolding":
        raise ResumeValidationError(
            "AutoHiC adoption review requires hic_scaffolding/hic_scaffolding"
        )
    hic_out = _hic_output(state)
    allowed = {"accept_candidate", "keep_prior_assembly", "stop_human_review"}
    if request.decision not in allowed:
        raise ResumeValidationError(
            f"unsupported AutoHiC adoption decision: {request.decision}"
        )
    if request.method_name and request.method_name != AUTOHIC_METHOD:
        raise ResumeValidationError(
            f"AutoHiC adoption method must be {AUTOHIC_METHOD}"
        )

    candidate = dict((hic_out.get("method_candidates") or {}).get(AUTOHIC_METHOD) or {})
    pre_hic_assembly_ref = hic_out.get("pre_hic_assembly_ref")
    if not pre_hic_assembly_ref:
        raise ResumeValidationError("AutoHiC adoption requires pre_hic_assembly_ref")
    if request.decision == "accept_candidate":
        if candidate.get("status") != "executed" or not candidate.get("scaffold_fasta"):
            raise ResumeValidationError(
                "accept_candidate requires a completed AutoHiC chromosome assembly"
            )
        final_assembly = str(candidate["scaffold_fasta"])
    else:
        final_assembly = str(pre_hic_assembly_ref)

    active_interrupt = state.get("active_interrupt") or hic_out.get("active_interrupt") or {}
    review_request_id = request.review_request_id or active_interrupt.get(
        "review_request_id"
    )
    review_request_hash = request.review_request_hash or active_interrupt.get(
        "request_hash"
    )
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError("AutoHiC adoption decision is missing request binding")
    try:
        review_request_ref = ArtifactRef.model_validate(
            active_interrupt.get("request_ref")
        )
        review_request_record = load_review_request(
            absolute_data_dir(state),
            review_request_ref,
            session_id=state["session_id"],
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC adoption review request is missing or changed"
        ) from exc
    if (
        review_request_ref.sha256 != str(review_request_hash)
        or review_request_record.get("review_request_id") != str(review_request_id)
        or review_request_record.get("stage") != "hic_scaffolding"
        or review_request_record.get("gate_id") != "hic_scaffolding"
    ):
        raise ResumeValidationError("AutoHiC adoption review request binding mismatch")

    artifact_payload = HiCScaffoldDecisionArtifact(
        session_id=state["session_id"],
        decision=request.decision,
        method_name=AUTOHIC_METHOD,
        pre_hic_assembly_ref=str(pre_hic_assembly_ref),
        final_assembly_for_annotation=final_assembly,
        hic_read_refs=list(hic_out.get("hic_read_refs") or []),
        notes=list(request.notes),
        metadata=request.metadata,
    )
    try:
        decision_ref = persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=str(review_request_id),
            stage="hic_scaffolding",
            gate_id="hic_scaffolding",
            decision=artifact_payload,
            request_hash=str(review_request_hash),
            request_ref=review_request_ref,
            method_name=AUTOHIC_METHOD,
            artifact_type="hic_scaffolding.adoption_review.decision",
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            "AutoHiC adoption decision could not be persisted"
        ) from exc
    Ledger(absolute_data_dir(state)).record_review_decision(
        state["session_id"],
        "hic_scaffolding",
        gate_id="hic_scaffolding",
        decision=request.decision,
        notes=list(request.notes),
        method_name=AUTOHIC_METHOD,
        artifact_ref=decision_ref.path_ref,
        metadata=request.metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        review_request_id=str(review_request_id),
    )

    outputs = dict(state.get("stage_outputs") or {})
    outputs["hic_scaffolding"] = {
        **hic_out,
        "status": (
            "failed_terminal"
            if request.decision == "stop_human_review"
            else "completed"
        ),
        "decision": request.decision,
        "selected_method_name": AUTOHIC_METHOD,
        "pre_hic_assembly_ref": str(pre_hic_assembly_ref),
        "final_assembly_for_annotation": final_assembly,
        "final_assembly_for_downstream": final_assembly,
        "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
        "notes": list(request.notes),
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
        "active_interrupt": None,
    }
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
        "route_trace": [
            *state.get("route_trace", []),
            "hic_scaffolding_stage",
        ],
    }


def fail_hic_scaffolding(state: TopLevelState) -> dict:
    """Stop after recommendation, execution, or adoption preparation fails."""

    hic_out = _hic_output(state)
    return {
        "current_stage": "hic_scaffolding",
        "supervision_state": "failed_terminal",
        "active_interrupt": None,
        "last_error": hic_out.get("reason", "hic_scaffolding failed"),
    }


def _fail_update(
    state: TopLevelState,
    reason: str,
    *,
    failure_kind: str,
) -> dict:
    return _update_hic_output(
        state,
        {"status": "failed", "failure_kind": failure_kind, "reason": reason},
    )


def _target_assembly_level(state: TopLevelState) -> str:
    inventory_out = (state.get("stage_outputs") or {}).get("inventory") or {}
    return str(inventory_out.get("target_assembly_level") or "contig")


def _hic_assets(state: TopLevelState) -> list[dict[str, Any]]:
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return []
    registry_path = Path(registry_ref)
    if not registry_path.is_file():
        return []
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        asset
        for asset in registry.get("assets", [])
        if isinstance(asset, dict)
        and asset.get("asset_type") == "hic_reads_registered"
    ]
