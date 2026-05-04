"""Assembly review stage nodes.

Assembly review is the main human gate.  It does not invent thresholds.
It collects or references evaluation signals and routes to:
- deterministic safe path (existing assembly input only)
- human review gate (multiple candidates or insufficient signals)
- failure (no assembly available)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.review.base import ReviewPayload, ReviewSignal
from app.schemas.artifacts import (
    ArtifactMeta,
    AssemblyEvalBundle,
    AssemblyEvalSignal,
    ReviewDecisionArtifact,
)
from app.schemas.plan import AssemblyReviewPlan
from app.schemas.state import TopLevelState

ASSEMBLY_REVIEW_CONTRACT_METHOD = "assembly_review.contract"


def enter_assembly_review(state: TopLevelState) -> dict:
    """Mark assembly_review as running.  No side effects."""
    return {"supervision_state": "running_stage"}


def resolve_candidates(state: TopLevelState) -> dict:
    """Resolve available assembly candidates from prior stage outputs.

    Accepts:
    - existing assembly inputs routed directly from inventory
    - candidate assembly bundles produced by the assembly stage
    """
    stage_outputs = state.get("stage_outputs") or {}

    # Check for existing assembly route from inventory
    inventory_out = stage_outputs.get("inventory", {})
    entry_mode = inventory_out.get("entry_mode")
    registry_ref = state.get("asset_registry_ref")

    candidates: list[dict[str, Any]] = []
    rejected_candidates = dict((stage_outputs.get("assembly") or {}).get("rejected_candidates") or {})

    if entry_mode == "existing_assembly" and registry_ref:
        final_assembly_ref = _existing_assembly_ref(registry_ref)
        candidates.append({
            "candidate_id": "existing_assembly",
            "method_name": "user_provided",
            "source": "inventory",
            "registry_ref": registry_ref,
            "final_assembly_ref": final_assembly_ref,
        })

    # Check for assembly stage candidate bundles
    assembly_out = stage_outputs.get("assembly", {})
    assembly_candidates = _load_selected_candidate_refs(assembly_out.get("selected_candidate_refs") or [])
    assembly_candidates.extend(assembly_out.get("candidate_bundles", []))
    for bundle in assembly_candidates:
        candidates.append({
            "candidate_id": bundle.get("candidate_id", "unknown"),
            "method_name": bundle.get("method_name", "unknown"),
            "source": "assembly_stage",
            "bundle_ref": bundle.get("candidate_ref") or bundle.get("bundle_ref"),
            "status": bundle.get("status", "planned"),
            "final_assembly_ref": (bundle.get("expected_outputs") or {}).get("draft_assembly_fasta"),
        })

    # Build review plan
    review_plan = AssemblyReviewPlan(
        status="evaluating" if candidates else "failed",
        candidate_ids=[c["candidate_id"] for c in candidates],
        notes=[],
    )

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = {
        "status": "resolved",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "rejected_candidates": rejected_candidates,
        "review_plan": review_plan.model_dump(mode="json"),
    }

    if not candidates:
        outputs["assembly_review"]["status"] = "failed"
        outputs["assembly_review"]["reason"] = "no_assembly_candidates_found"

    return {"stage_outputs": outputs}


def plan_review_metrics(state: TopLevelState) -> dict:
    """Plan evaluation signals based on available inputs.

    Plans signals only when inputs exist; does not execute tools.
    Thresholds are not stored here.
    """
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})

    if ar_out.get("status") == "failed":
        return {}

    # Determine available inputs from asset registry or prior stages
    has_short_reads, has_long_reads, available_inputs, missing_inputs = _available_review_inputs(state)

    read_qc_out = stage_outputs.get("read_qc", {})
    if read_qc_out:
        if read_qc_out.get("clean_long_reads") is not None:
            has_long_reads = True
            available_inputs.add("clean_long_reads")
        if read_qc_out.get("clean_short_dna_reads") is not None:
            has_short_reads = True
            available_inputs.add("clean_short_dna_reads")

    planned_signals: list[str] = ["quast", "busco"]
    if has_short_reads:
        planned_signals.append("merqury")
    if has_long_reads:
        planned_signals.append("mapping_coverage")
        planned_signals.append("purge_dups_check")
    planned_signals.append("contamination_check")

    review_plan_data = ar_out.get("review_plan", {})
    review_plan_data["planned_signals"] = planned_signals
    review_plan_data["status"] = "evaluating"

    signals: list[dict[str, Any]] = []
    eval_signals: list[AssemblyEvalSignal] = []
    candidate_ids = [candidate.get("candidate_id", "unknown") for candidate in ar_out.get("candidates", [])]
    for signal_name in planned_signals:
        method_name = _review_signal_method_name(signal_name)
        signals.append(ReviewSignal(
            signal_name=signal_name,
            method_name=method_name,
            status="pending",
        ).model_dump(mode="json"))
        for candidate_id in candidate_ids:
            eval_signals.append(
                AssemblyEvalSignal(
                    signal_name=signal_name,
                    method_name=method_name,
                    candidate_id=candidate_id,
                    status="pending",
                    notes=["execution_deferred_to_method_specific_task"],
                )
            )

    eval_bundle = AssemblyEvalBundle(
        session_id=state["session_id"],
        candidate_ids=candidate_ids,
        signals=eval_signals,
        available_inputs=sorted(available_inputs),
        missing_inputs=sorted(missing_inputs),
        notes=["thresholds_not_applied_without_human_approval"],
    )
    eval_bundle_ref = _write_json_artifact(
        state,
        "assembly_eval_bundle.json",
        eval_bundle.model_dump(mode="json"),
        artifact_type="assembly_review.eval_bundle",
    )

    ar_out["review_plan"] = review_plan_data
    ar_out["planned_signals"] = planned_signals
    ar_out["signals"] = signals
    ar_out["eval_bundle_ref"] = eval_bundle_ref

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = ar_out
    return {"stage_outputs": outputs}


def evaluate_review_rules(state: TopLevelState) -> dict:
    """Evaluate structural rules without inventing thresholds.

    Returns:
    - deterministic_safe: True only for existing-assembly-only input
    - needs_human: True when multiple candidates or any candidate needs evaluation
    """
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})

    if ar_out.get("status") == "failed":
        return {}

    candidates = ar_out.get("candidates", [])
    entry_mode = (stage_outputs.get("inventory", {})).get("entry_mode")

    # Deterministic safe path: only existing assembly, no candidates from
    # assembly stage
    only_existing = (
        entry_mode == "existing_assembly"
        and len(candidates) == 1
        and candidates[0].get("source") == "inventory"
    )

    # Multiple candidates always need review
    multiple_candidates = len(candidates) > 1

    # Any candidate from assembly stage needs review
    has_assembly_candidates = any(
        c.get("source") == "assembly_stage" for c in candidates
    )

    needs_human = multiple_candidates or has_assembly_candidates
    deterministic_safe = only_existing and not needs_human

    ar_out["deterministic_safe"] = deterministic_safe
    ar_out["needs_human"] = needs_human

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = ar_out
    return {"stage_outputs": outputs}


def human_review_gate(state: TopLevelState) -> dict:
    """Set an active interrupt for unresolved assembly review decisions."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})
    candidates = ar_out.get("candidates", [])
    signals = ar_out.get("signals", [])

    payload = ReviewPayload(
        stage="assembly_review",
        reason="assembly_review_human_decision_required",
        candidates=candidates,
        signals=[ReviewSignal.model_validate(s) for s in signals],
        decisions_allowed=[
            "accept",
            "rerun",
            "purge",
            "continue_low_confidence",
            "stop_human_review",
        ],
        notes=[
            "Thresholds for BUSCO, QUAST, Merqury, and purge_dups "
            "require human approval before application.",
        ],
    )

    # Record ledger event
    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    ledger.record_review_requested(
        state["session_id"],
        "assembly_review",
        reason="assembly_review_human_decision_required",
        signals=[s.model_dump(mode="json") for s in payload.signals],
        candidates=candidates,
    )

    request_ref = _write_json_artifact(
        state,
        "assembly_review_request.json",
        payload.model_dump(mode="json"),
        artifact_type="assembly_review.request",
    )
    outputs = dict(stage_outputs)
    outputs["assembly_review"] = {
        **ar_out,
        "status": "blocked_human",
        "review_request_artifact_ref": request_ref,
    }
    return {
        "active_interrupt": payload.model_dump(mode="json"),
        "supervision_state": "blocked_human",
        "stage_outputs": outputs,
    }


def commit_assembly_review(state: TopLevelState) -> dict:
    """Commit the assembly review decision and advance routing.

    For deterministic safe path (existing assembly only), auto-accepts.
    For human-gated paths, reads the decision from stage_outputs.
    """
    from app.graph.routing import next_stage_name

    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})
    candidates = ar_out.get("candidates", [])

    # Determine decision
    deterministic_safe = ar_out.get("deterministic_safe", False)
    if deterministic_safe and candidates:
        decision = "accept"
        selected_candidate_id = candidates[0]["candidate_id"]
        rejected_candidates: list[str] = []
        final_assembly_ref = candidates[0].get("final_assembly_ref")
    else:
        # Human decision should be in stage_outputs or active_interrupt
        decision = ar_out.get("decision", "stop_human_review")
        selected_candidate_id = ar_out.get("selected_candidate_id")
        rejected_candidates = ar_out.get("rejected_candidates", [])
        final_assembly_ref = ar_out.get("final_assembly_ref")

    trace = [*state.get("route_trace", []), "assembly_review_stage"]
    next_stage = next_stage_name("assembly_review")

    # Record ledger event
    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    ledger.record_review_decision(
        state["session_id"],
        "assembly_review",
        decision=decision,
        candidate_id=selected_candidate_id,
        rejected_candidates=rejected_candidates,
        notes=ar_out.get("notes", []),
    )

    decision_artifact = ReviewDecisionArtifact(
        session_id=state["session_id"],
        stage="assembly_review",
        decision=decision,
        candidate_id=selected_candidate_id,
        rejected_candidates=rejected_candidates if isinstance(rejected_candidates, list) else list(rejected_candidates),
        final_assembly_ref=final_assembly_ref,
        notes=ar_out.get("notes", []),
    )
    decision_ref = _write_json_artifact(
        state,
        "assembly_review_decision.json",
        decision_artifact.model_dump(mode="json"),
        artifact_type="assembly_review.decision",
    )

    update: dict[str, Any] = {
        "current_stage": next_stage,
        "supervision_state": "completed" if next_stage == "done" else "running_stage",
        "route_trace": trace,
    }

    # Publish final assembly reference
    if selected_candidate_id:
        update["stage_outputs"] = {
            **stage_outputs,
            "assembly_review": {
                **ar_out,
                "status": "completed",
                "decision": decision,
                "selected_candidate_id": selected_candidate_id,
                "rejected_candidates": rejected_candidates,
                "final_assembly_ref": final_assembly_ref,
                "review_decision_artifact_ref": decision_ref,
            },
        }

    return update


def fail_assembly_review(state: TopLevelState) -> dict:
    """Record assembly review failure and stop."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})
    return {
        "current_stage": "assembly_review",
        "supervision_state": "failed_terminal",
        "last_error": ar_out.get("reason", "assembly_review failed"),
    }


def _load_selected_candidate_refs(candidate_refs: list[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for candidate_ref in candidate_refs:
        path = Path(candidate_ref)
        if not path.exists():
            candidates.append(
                {
                    "candidate_id": candidate_ref,
                    "method_name": "unknown",
                    "status": "missing",
                    "bundle_ref": candidate_ref,
                }
            )
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["bundle_ref"] = str(path)
        candidates.append(payload)
    return candidates


def _existing_assembly_ref(registry_ref: str) -> str | None:
    path = Path(registry_ref)
    if not path.exists():
        return None
    registry = json.loads(path.read_text(encoding="utf-8"))
    for asset in registry.get("assets", []):
        if asset.get("asset_type") == "assembly_fasta_registered":
            return str(asset.get("path"))
    return None


def _available_review_inputs(state: TopLevelState) -> tuple[bool, bool, set[str], set[str]]:
    available_inputs: set[str] = set()
    missing_inputs: set[str] = set()
    registry_ref = state.get("asset_registry_ref")
    if registry_ref and Path(registry_ref).exists():
        registry = json.loads(Path(registry_ref).read_text(encoding="utf-8"))
        asset_types = {asset.get("asset_type") for asset in registry.get("assets", [])}
        for asset_type in ["clean_short_dna_reads", "clean_long_reads"]:
            if asset_type in asset_types:
                available_inputs.add(asset_type)
            else:
                missing_inputs.add(asset_type)
    has_short_reads = "clean_short_dna_reads" in available_inputs
    has_long_reads = "clean_long_reads" in available_inputs
    return has_short_reads, has_long_reads, available_inputs, missing_inputs


def _review_signal_method_name(signal_name: str) -> str:
    return {
        "quast": "assembly_review.eval.quast",
        "busco": "assembly_review.eval.busco",
        "merqury": "assembly_review.eval.merqury",
        "mapping_coverage": "assembly_review.eval.mapping_coverage",
        "purge_dups_check": "assembly_review.check.purge_dups",
        "contamination_check": "assembly_review.check.contamination",
    }[signal_name]


def _write_json_artifact(
    state: TopLevelState,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
) -> str:
    data_dir = Path(state.get("data_dir", "data"))
    out_dir = data_dir / "artifacts" / "assembly_review" / state["session_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        path,
        stage="assembly_review",
        method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
        artifact_type=artifact_type,
    )
    append_artifact_records(data_dir, state["session_id"], [artifact])
    return str(path)
