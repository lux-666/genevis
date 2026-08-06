"""Nodes for the selected, AutoHiC-only Hi-C scaffolding stage."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from app.configuration import load_descriptor_registry
from app.control.lifecycle import ResumeValidationError
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
    persist_review_request,
)
from app.schemas.artifacts import (
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactRef,
    HiCScaffoldDecisionArtifact,
)
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, append_route_trace
from app.stages.hic_scaffolding.method_application.support import (
    hic_output as _hic_output,
)
from app.stages.hic_scaffolding.method_application.support import (
    update_hic_output as _update_hic_output,
)
from app.stages.hic_scaffolding.parameters.workflow import (
    prepare_autohic_review_retry,
)
from app.stages.hic_scaffolding.state import HIC_SCAFFOLDING_CURSOR_ADAPTER
from app.stages.read_resolution import resolve_hic_pairs
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import assembly_resource_spec
from app.storage.artifacts import artifact_ref_for_path
from app.storage.ledger import Ledger
from app.workflow import next_stage_name

AUTOHIC_METHOD = "hic_scaffolding.candidate.autohic"
HIC_CONTRACT_METHOD = "hic_scaffolding.contract"


def enter_hic_scaffolding(state: TopLevelState) -> StateUpdate:
    """Mark the selected Hi-C stage as running."""

    return {"supervision_state": "running_stage"}


def resolve_hic_inputs(state: TopLevelState) -> StateUpdate:
    """Resolve and validate everything required before recommending parameters."""

    assembly = resolve_stage_resource(
        state,
        resource_spec=assembly_resource_spec(),
    )
    final_assembly_ref, source = assembly.path, assembly.source
    try:
        hic_assets = _hic_assets(state)
    except ValueError as exc:
        return _fail_update(
            state,
            f"Hi-C provenance is invalid: {exc}",
            failure_kind="invalid_input",
        )
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
        "output_assembly_ref": final_assembly_ref,
        "hic_read_refs": hic_read_refs,
        "hic_read_paths": hic_read_paths,
        "hic_library_metadata": [
            dict(asset.get("metadata") or {})
            for asset in hic_assets
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
        descriptor_present = AUTOHIC_METHOD in load_descriptor_registry().methods
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


def prepare_autohic_adoption_review(state: TopLevelState) -> StateUpdate:
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
            "request_parameter_retry",
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


def block_hic_scaffolding(state: TopLevelState) -> StateUpdate:
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
        "active_interrupt": cast(ActiveInterrupt, interrupt),
    }


def block_hic_requirements(state: TopLevelState) -> StateUpdate:
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
) -> StateUpdate:
    """Apply the final human adoption decision for one completed AutoHiC run."""

    if request.stage != "hic_scaffolding" or request.gate_id != "hic_scaffolding":
        raise ResumeValidationError(
            "AutoHiC adoption review requires hic_scaffolding/hic_scaffolding"
        )
    hic_out = _hic_output(state)
    allowed = {
        "accept_candidate",
        "keep_prior_assembly",
        "request_parameter_retry",
        "stop_human_review",
    }
    if request.decision not in allowed:
        raise ResumeValidationError(
            f"unsupported AutoHiC adoption decision: {request.decision}"
        )
    if request.method_name and request.method_name != AUTOHIC_METHOD:
        raise ResumeValidationError(f"AutoHiC adoption method must be {AUTOHIC_METHOD}")

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

    active_interrupt = (
        state.get("active_interrupt") or hic_out.get("active_interrupt") or {}
    )
    review_request_id = request.review_request_id or active_interrupt.get(
        "review_request_id"
    )
    review_request_hash = request.review_request_hash or active_interrupt.get(
        "request_hash"
    )
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError(
            "AutoHiC adoption decision is missing request binding"
        )
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

    adoption_resource_metadata: dict[str, Any] | None = None
    if request.decision == "accept_candidate":
        if request.decided_by != "human":
            raise ResumeValidationError(
                "AutoHiC candidate adoption requires a human review decision"
            )
        try:
            current_assembly = resolve_stage_resource(
                state,
                resource_spec=assembly_resource_spec(),
            )
            if (
                not current_assembly.found
                or current_assembly.path is None
                or Path(current_assembly.path).resolve(strict=False)
                != Path(str(pre_hic_assembly_ref)).resolve(strict=False)
            ):
                raise ValueError(
                    "current assembly no longer matches pre_hic_assembly_ref"
                )
            primary_output_ref = artifact_ref_for_path(
                absolute_data_dir(state),
                state["session_id"],
                final_assembly,
                expected_type=HIC_SCAFFOLDED_ASSEMBLY,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ResumeValidationError(
                f"AutoHiC adopted assembly provenance is invalid: {exc}"
            ) from exc
        adoption_resource_metadata = {
            "decision": request.decision,
            "pre_hic_assembly_ref": str(pre_hic_assembly_ref),
            "primary_output_ref": primary_output_ref.model_dump(mode="json"),
        }
        if current_assembly.metadata.get("candidate_bundle_ref") is not None:
            adoption_resource_metadata["input_candidate_bundle_ref"] = (
                current_assembly.metadata["candidate_bundle_ref"]
            )
        if current_assembly.metadata.get("primary_output_ref") is not None:
            adoption_resource_metadata["input_primary_output_ref"] = (
                current_assembly.metadata["primary_output_ref"]
            )

    decision_metadata = {
        **dict(request.metadata),
        "review_evidence": {
            key: candidate.get(key)
            for key in (
                "scaffold_fasta",
                "contact_map",
                "assembly_file",
                "html_report",
                "review_bundle_ref",
                "qc_report_ref",
            )
            if candidate.get(key)
        },
    }
    decision = cast(
        Literal[
            "accept_candidate",
            "keep_prior_assembly",
            "request_parameter_retry",
            "stop_human_review",
        ],
        request.decision,
    )
    artifact_payload = HiCScaffoldDecisionArtifact(
        session_id=state["session_id"],
        decision=decision,
        method_name=AUTOHIC_METHOD,
        pre_hic_assembly_ref=str(pre_hic_assembly_ref),
        output_assembly_ref=final_assembly,
        hic_read_refs=list(hic_out.get("hic_read_refs") or []),
        notes=list(request.notes),
        metadata=decision_metadata,
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
        metadata=decision_metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        review_request_id=str(review_request_id),
    )

    if request.decision == "request_parameter_retry":
        retry_update = prepare_autohic_review_retry(state, decision_ref)
        outputs = dict(retry_update.get("stage_outputs") or {})
        retry_out = HIC_SCAFFOLDING_CURSOR_ADAPTER.validate_python(
            outputs.get("hic_scaffolding") or {}
        )
        stage_update = _update_hic_output(
            state,
            {
                **retry_out,
                "active_interrupt": None,
                "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
            },
        )
        retry_exhausted = (
            retry_out.get("status") == "review_parameter_retries_exhausted"
        )
        result: StateUpdate = {
            "current_stage": "hic_scaffolding",
            "supervision_state": (
                "failed_terminal" if retry_exhausted else "running_stage"
            ),
            "active_interrupt": None,
            **stage_update,
        }
        if retry_exhausted:
            result["last_error"] = retry_out.get(
                "reason",
                "AutoHiC review retry budget exhausted",
            )
        return result

    stage_update = _update_hic_output(
        state,
        {
            "status": (
                "failed_terminal"
                if request.decision == "stop_human_review"
                else "completed"
            ),
            "decision": request.decision,
            "selected_method_name": AUTOHIC_METHOD,
            "pre_hic_assembly_ref": str(pre_hic_assembly_ref),
            "output_assembly_ref": final_assembly,
            "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
            "notes": list(request.notes),
            "decided_by": request.decided_by,
            "rationale_artifact_ref": request.rationale_artifact_ref,
            "active_interrupt": None,
        },
    )
    if request.decision == "stop_human_review":
        return {
            "current_stage": "hic_scaffolding",
            "supervision_state": "failed_terminal",
            "active_interrupt": None,
            **stage_update,
            "last_error": "hic_scaffolding stopped by human review decision",
        }
    update: StateUpdate = {
        "current_stage": next_stage_name("hic_scaffolding"),
        "supervision_state": "running_stage",
        "active_interrupt": None,
        **stage_update,
        "route_trace": append_route_trace(state, "hic_scaffolding_stage"),
    }
    if request.decision == "keep_prior_assembly":
        return update

    if adoption_resource_metadata is None:
        raise ResumeValidationError("AutoHiC adoption metadata is unavailable")
    update["resource_refs"] = update_resource_ref(
        state,
        "assembly",
        [final_assembly],
        "hic_scaffolding",
        adoption_resource_metadata,
    )
    return update


def fail_hic_scaffolding(state: TopLevelState) -> StateUpdate:
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
) -> StateUpdate:
    return _update_hic_output(
        state,
        {"status": "failed", "failure_kind": failure_kind, "reason": reason},
    )


def _target_assembly_level(state: TopLevelState) -> str:
    inventory_out = (state.get("stage_outputs") or {}).get("inventory") or {}
    return str(inventory_out.get("target_assembly_level") or "contig")


def _hic_assets(state: TopLevelState) -> list[dict[str, Any]]:
    pairs = resolve_hic_pairs(state)
    assets: list[dict[str, Any]] = []
    for pair in pairs:
        for mate, member in (("R1", pair.r1), ("R2", pair.r2)):
            assets.append(
                {
                    "asset_id": member.member_id,
                    "asset_type": "hic_reads_registered",
                    "path": member.path,
                    "metadata": {
                        "manifest_entry_ordinal": pair.manifest_entry_ordinal,
                        "pair_index": pair.pair_index,
                        "mate": mate,
                        "platform": member.platform,
                        "format": member.format,
                    },
                }
            )
    return assets
