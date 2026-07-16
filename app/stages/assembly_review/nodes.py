"""Assembly review stage nodes.

Assembly review is the main human gate.  It does not invent thresholds.
It collects or references evaluation signals and routes to:
- deterministic safe path (existing assembly input only)
- human review gate (multiple candidates or insufficient signals)
- failure (no assembly available)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

logger = logging.getLogger(__name__)

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.evidence import load_execution_evidence, persist_execution_result
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.reference.ncbi_metazoa import (
    build_related_species_evidence_pack as _build_related_species_evidence_pack,
)
from app.reference.ncbi_metazoa import (
    build_related_species_statistics_pack as _build_related_species_statistics_pack,
)
from app.reference.ncbi_metazoa import species_name_from_metadata
from app.review.base import ResumeReviewRequest, ReviewPayload, ReviewSignal
from app.review.persistence import persist_review_decision, persist_review_request
from app.review.policy import evaluate
from app.review.recommender import RecommendationError, recommend_review_decision
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_CANDIDATE_COMPARISON,
    ASSEMBLY_REVIEW_ACTION,
    ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
    ASSEMBLY_REVIEW_EVAL_BUNDLE,
    ASSEMBLY_REVIEW_PACKET,
    ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
    ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
    ArtifactMeta,
    AssemblyCandidateBundle,
    AssemblyEvalBundle,
    AssemblyEvalSignal,
    AssemblyReviewActionArtifact,
    ReviewDecisionArtifact,
)
from app.schemas.plan import AssemblyReviewPlan
from app.schemas.state import TopLevelState
from app.stages.assembly_review.method_application.lifecycle import (
    read_eval_bundle as _read_eval_bundle,
)
from app.stages.assembly_review.method_application.lifecycle import (
    write_json_artifact as _write_json_artifact,
)
from app.stages.assembly_review.notebook import (
    build_assembly_review_notebook,
    render_statistics_figures,
    write_notebook,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    is_fail_status,
    resolve_run_ref,
    session_paths,
    stage_artifacts_dir,
    stage_work_dir,
)
from app.storage.publish import shell_publish_file

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

    # A successful dedup stage adds a second candidate; it never replaces raw.
    dedup_out = stage_outputs.get("assembly_dedup", {})
    purged_ref = dedup_out.get("purged_assembly_ref")
    if purged_ref:
        candidates.append({
            "candidate_id": f"{dedup_out.get('candidate_id') or 'assembly'}:assembly_dedup.purge_dups",
            "method_name": "assembly_dedup.purge_dups",
            "source": "assembly_dedup_stage",
            "bundle_ref": dedup_out.get("purged_candidate_ref"),
            "status": dedup_out.get("status", "completed"),
            "final_assembly_ref": purged_ref,
            "pre_dedup_assembly_ref": dedup_out.get("input_assembly_ref"),
            "purged_assembly_ref": purged_ref,
        })

    # Build review plan
    review_plan = AssemblyReviewPlan(
        status="evaluating" if candidates else "failed",
        candidate_ids=[c["candidate_id"] for c in candidates],
        notes=[],
    )

    outputs = dict(stage_outputs)
    existing_review = dict(stage_outputs.get("assembly_review") or {})
    outputs["assembly_review"] = {
        **existing_review,
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
    _has_short_reads, _has_long_reads, available_inputs, missing_inputs = _available_review_inputs(state)

    read_qc_out = stage_outputs.get("read_qc", {})
    if read_qc_out:
        if read_qc_out.get("clean_long_reads") is not None:
            available_inputs.add("clean_long_reads")
        if read_qc_out.get("clean_short_dna_reads") is not None:
            available_inputs.add("clean_short_dna_reads")

    planned_signals: list[str] = ["quast", "busco"]

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
        "assembly_eval_bundle.planned.json",
        eval_bundle.model_dump(mode="json"),
        artifact_type=ASSEMBLY_REVIEW_EVAL_BUNDLE,
    )

    preserved = {key: ar_out[key] for key in ["execution_mode", "env", "quast_parameters"] if key in ar_out}
    ar_out["review_plan"] = review_plan_data
    ar_out["planned_signals"] = planned_signals
    ar_out["signals"] = [signal.model_dump(mode="json") for signal in eval_signals]
    ar_out["eval_bundle_ref"] = eval_bundle_ref
    ar_out.update(preserved)

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = ar_out
    return {"stage_outputs": outputs}


def evaluate_review_rules(state: TopLevelState) -> dict:
    """Evaluate structural rules and record candidate count for routing."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})

    if ar_out.get("status") == "failed":
        return {}

    candidates = ar_out.get("candidates", [])
    ar_out["candidate_count"] = len(candidates)

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = ar_out
    return {"stage_outputs": outputs}


def parameter_quality_gate(state: TopLevelState) -> dict:
    """Route Phase10 assembly-review failures back to assembly parameter replan."""

    stage_outputs = dict(state.get("stage_outputs") or {})
    assembly_out = dict(stage_outputs.get("assembly") or {})
    if not assembly_out.get("parameter_attempts"):
        return {}

    ar_out = dict(stage_outputs.get("assembly_review") or {})
    metrics = _phase10_review_metrics(ar_out, assembly_out)
    if metrics and not ar_out.get("metrics"):
        ar_out["metrics"] = metrics
        stage_outputs["assembly_review"] = ar_out

    from app.stages.assembly.nodes import (
        parameter_quality_gate as assembly_parameter_quality_gate,
    )

    gate_update = assembly_parameter_quality_gate({**state, "stage_outputs": stage_outputs})
    updated_outputs = dict(gate_update.get("stage_outputs") or stage_outputs)
    updated_assembly = dict(updated_outputs.get("assembly") or {})
    quality_gate = updated_assembly.get("quality_gate")

    if updated_assembly.get("status") == "retry_parameters_after_review_failure":
        updated_outputs["assembly_review"] = {
            **ar_out,
            "status": "parameter_replan_requested",
            "parameter_quality_gate": quality_gate,
        }
        return {
            "current_stage": "assembly",
            "supervision_state": "running_stage",
            "active_interrupt": None,
            "stage_outputs": updated_outputs,
        }

    if updated_assembly.get("status") == "review_parameter_retries_exhausted":
        updated_outputs["assembly_review"] = {
            **ar_out,
            "parameter_quality_gate": quality_gate,
            "parameter_retries_exhausted": True,
            "parameter_quality_checked": True,
        }
        return {"stage_outputs": updated_outputs}

    updated_outputs["assembly_review"] = {
        **ar_out,
        "parameter_quality_gate": quality_gate,
        "parameter_quality_checked": True,
    }
    return {"stage_outputs": updated_outputs}


def build_related_species_evidence_pack(state: TopLevelState) -> dict:
    """Build non-blocking local NCBI evidence for the review report and LLM."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = dict(stage_outputs.get("assembly_review") or {})
    if is_fail_status(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out)
    if reference_dir:
        evidence_pack = _build_related_species_evidence_pack(species_name, reference_dir=reference_dir)
    else:
        evidence_pack = _build_related_species_evidence_pack(species_name)
    existing_ref = ar_out.get("related_species_evidence_pack_ref")
    if existing_ref and _json_artifact_payload_matches(existing_ref, evidence_pack):
        return {}

    evidence_ref = _write_json_artifact(
        state,
        "related_species_evidence_pack.json",
        evidence_pack,
        artifact_type=ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
    )
    outputs = dict(stage_outputs)
    outputs["assembly_review"] = {
        **ar_out,
        "related_species_evidence_pack": evidence_pack,
        "related_species_evidence_pack_ref": evidence_ref,
    }
    return {"stage_outputs": outputs}


def _related_species_reference_dir(ar_out: dict[str, Any]) -> str | Path | None:
    """Return validated NCBI reference directory path from stage output or environment.

    Returns None if no valid path is configured, triggering default reference directory.
    """
    value = ar_out.get("related_species_reference_dir") or os.environ.get("GENE_AGENT_NCBI_METAZOA_REFERENCE_DIR")
    if not value:
        return None

    ref_path = Path(value)
    manifest_path = ref_path / "reference_manifest.json"

    if not ref_path.exists():
        logger.warning(
            f"Configured NCBI reference directory does not exist: {ref_path}. "
            "Falling back to default reference directory."
        )
        return None

    if not manifest_path.exists():
        logger.warning(
            f"NCBI reference directory missing manifest file: {manifest_path}. "
            "Falling back to default reference directory."
        )
        return None

    return ref_path


def build_related_species_statistics_and_notebook(state: TopLevelState) -> dict:
    """Build statistics pack and comparative notebook for assembly review."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = dict(stage_outputs.get("assembly_review") or {})
    if is_fail_status(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out)

    target_metrics = _extract_target_metrics_from_signals(ar_out)

    if reference_dir:
        statistics_pack = _build_related_species_statistics_pack(
            species_name, reference_dir=reference_dir, target_metrics=target_metrics
        )
    else:
        statistics_pack = _build_related_species_statistics_pack(species_name, target_metrics=target_metrics)

    existing_stats_ref = ar_out.get("related_species_statistics_pack_ref")
    statistics_pack_unchanged = (
        existing_stats_ref and _json_artifact_payload_matches(existing_stats_ref, statistics_pack)
    )

    if statistics_pack_unchanged:
        existing_notebook_ref = ar_out.get("assembly_review_comparative_notebook_ref")
        if existing_notebook_ref and Path(existing_notebook_ref).exists():
            comparative_figures = ar_out.get("comparative_figures", {})
            all_figures_exist = all(Path(p).exists() for p in comparative_figures.values())
            if all_figures_exist:
                # Full skip: re-register artifacts for ledger consistency, then return
                data_dir = absolute_data_dir(state)
                session_id = state["session_id"]

                notebook_artifact = ArtifactMeta.from_file(
                    Path(existing_notebook_ref),
                    stage="assembly_review",
                    method_name="assembly_review.contract",
                    artifact_type=ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
                    metadata={"session_id": session_id},
                )
                append_artifact_records(data_dir, session_id, [notebook_artifact])

                figure_artifacts = [
                    ArtifactMeta.from_file(
                        Path(abs_path),
                        stage="assembly_review",
                        method_name="assembly_review.contract",
                        artifact_type=f"assembly_review.figure.{logical_name}",
                        metadata={"figure_type": logical_name},
                    )
                    for logical_name, abs_path in comparative_figures.items()
                ]
                if figure_artifacts:
                    append_artifact_records(data_dir, session_id, figure_artifacts)

                return {}

    if statistics_pack_unchanged:
        statistics_ref = existing_stats_ref
    else:
        statistics_ref = _write_json_artifact(
            state,
            "related_species_statistics_pack.json",
            statistics_pack,
            artifact_type=ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
        )

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    figures_dir = stage_artifacts_dir(state, "assembly_review", "figures")
    figure_paths = render_statistics_figures(statistics_pack, figures_dir, target_metrics=target_metrics)

    figure_artifacts = []
    for logical_name, abs_path in figure_paths.items():
        figure_artifacts.append(
            ArtifactMeta.from_file(
                Path(abs_path),
                stage="assembly_review",
                method_name="assembly_review.contract",
                artifact_type=f"assembly_review.figure.{logical_name}",
                metadata={"figure_type": logical_name},
            )
        )
    if figure_artifacts:
        append_artifact_records(data_dir, session_id, figure_artifacts)

    out_dir = stage_artifacts_dir(state, "assembly_review")
    statistics_pack_relpath = "related_species_statistics_pack.json"
    figures_relpaths = {name: f"figures/{Path(path).name}" for name, path in figure_paths.items()}
    evidence_pack = ar_out.get("related_species_evidence_pack", {})

    notebook = build_assembly_review_notebook(
        statistics_pack=statistics_pack,
        evidence_pack=evidence_pack,
        target_metrics=target_metrics,
        session_id=session_id,
        statistics_pack_relpath=statistics_pack_relpath,
        figures_relpaths=figures_relpaths,
    )

    notebook_path = out_dir / "assembly_review_report.ipynb"
    write_notebook(notebook, notebook_path)

    notebook_artifact = ArtifactMeta.from_file(
        notebook_path,
        stage="assembly_review",
        method_name="assembly_review.contract",
        artifact_type=ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
        metadata={"session_id": session_id, "figure_count": len(figure_paths)},
    )
    append_artifact_records(data_dir, session_id, [notebook_artifact])

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = {
        **ar_out,
        "related_species_statistics_pack": statistics_pack,
        "related_species_statistics_pack_ref": statistics_ref,
        "assembly_review_comparative_notebook_ref": str(notebook_path),
        "comparative_figures": figure_paths,
    }
    return {"stage_outputs": outputs}


def _extract_target_metrics_from_signals(ar_out: dict[str, Any]) -> dict[str, float] | None:
    """Extract target assembly metrics from available signals for percentile computation.

    Returns None when multiple candidates exist (no single target to position).
    """
    candidates = ar_out.get("candidates", [])
    if len(candidates) != 1:
        return None

    candidate_id = candidates[0].get("candidate_id")
    if not candidate_id:
        return None

    signals = ar_out.get("signals", [])
    candidate_metrics = _candidate_metrics(candidate_id, signals)

    target_metrics: dict[str, float] = {}

    # Map QUAST metrics
    quast = candidate_metrics.get("quast", {})
    if quast.get("n50") is not None:
        target_metrics["n50_bp"] = float(quast["n50"])
    if quast.get("total_length") is not None:
        target_metrics["assembly_size_bp"] = float(quast["total_length"])
    if quast.get("gc_percent") is not None:
        target_metrics["gc_percent"] = float(quast["gc_percent"])

    # Map BUSCO metrics
    busco = candidate_metrics.get("busco", {})
    if busco.get("complete_percent") is not None:
        target_metrics["busco_complete_percent"] = float(busco["complete_percent"])
    if busco.get("duplicated_percent") is not None:
        target_metrics["busco_duplicated_percent"] = float(busco["duplicated_percent"])

    return target_metrics if target_metrics else None


def _phase10_review_metrics(ar_out: dict[str, Any], assembly_out: dict[str, Any]) -> dict[str, Any]:
    if isinstance(ar_out.get("metrics"), dict):
        return dict(ar_out["metrics"])
    candidates = ar_out.get("candidates") or []
    signals = ar_out.get("signals") or []
    candidate_id = None
    if candidates and isinstance(candidates[0], dict):
        candidate_id = candidates[0].get("candidate_id")
    if not candidate_id and signals and isinstance(signals[0], dict):
        candidate_id = signals[0].get("candidate_id")
    if not candidate_id:
        return {}
    nested = _candidate_metrics(str(candidate_id), signals)
    quast = dict(nested.get("quast") or {})
    busco = dict(nested.get("busco") or {})
    metrics: dict[str, Any] = {}
    if quast.get("total_length") is not None:
        metrics["assembly_size"] = quast.get("total_length")
        genome_size = assembly_out.get("genome_size_prior")
        if genome_size:
            try:
                metrics["assembly_size_ratio"] = float(quast["total_length"]) / float(genome_size)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    if quast.get("n50") is not None:
        metrics["contig_n50"] = quast.get("n50")
    if quast.get("num_contigs") is not None:
        metrics["contig_count"] = quast.get("num_contigs")
    if busco.get("complete_percent") is not None:
        metrics["busco_complete"] = busco.get("complete_percent")
    if busco.get("duplicated_percent") is not None:
        metrics["busco_duplicate"] = busco.get("duplicated_percent")
    return metrics


def human_review_gate(state: TopLevelState) -> dict:
    """Set an active interrupt for unresolved assembly review decisions."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})
    ar_out, report_refs, comparison_context = _ensure_assembly_review_reports(state, ar_out)
    candidates = ar_out.get("candidates", [])
    signals = ar_out.get("signals", [])

    payload = ReviewPayload(
        stage="assembly_review",
        gate_id="assembly_review",
        reason="assembly_review_human_decision_required",
        candidates=candidates,
        signals=[ReviewSignal.model_validate(s) for s in signals],
        decisions_allowed=[
            "accept",
            "rerun",
            "stop_human_review",
        ],
        notes=[
            "Thresholds for BUSCO and QUAST require human approval before application.",
            "Accept continues with the selected assembly to Hi-C scaffolding, then T2T, then annotation stages.",
            "Rerun keeps the workflow at assembly review after a new assembly attempt; stop leaves downstream stages unstarted.",
        ],
        auto_decidable=_assembly_review_auto_decidable(ar_out),
        report_refs=report_refs,
        candidate_comparison_context=comparison_context,
        related_species_evidence_pack=dict(ar_out.get("related_species_evidence_pack") or {}),
    )

    persisted_request = persist_review_request(
        state,
        stage="assembly_review",
        gate_id="assembly_review",
        reason="assembly_review_human_decision_required",
        payload=payload,
        method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
        artifact_type="assembly_review.request",
    )
    request_ref = persisted_request.request_ref
    outcome = evaluate("assembly_review", payload, ar_out.get("review_policy"))
    pending_confirmation = outcome.action == "BLOCK_PENDING_CONFIRM"
    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    ledger.record_review_requested(
        state["session_id"],
        "assembly_review",
        reason="assembly_review_human_decision_required",
        signals=[s.model_dump(mode="json") for s in payload.signals],
        candidates=candidates,
        review_request_artifact_ref=request_ref.model_dump(mode="json"),
        auto_decidable=payload.auto_decidable,
        policy_mode=outcome.mode,
        pending_human_confirmation=pending_confirmation,
    )

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = {
        **ar_out,
        "status": "blocked_human",
        "review_request_artifact_ref": request_ref.model_dump(mode="json"),
        "review_policy_outcome": outcome.model_dump(mode="json"),
        "auto_decidable": payload.auto_decidable,
        "pending_human_confirmation": pending_confirmation,
    }
    blocked_update = {
        "active_interrupt": persisted_request.compact_interrupt(
            stage="assembly_review",
            gate_id="assembly_review",
            reason="assembly_review_human_decision_required",
        ),
        "supervision_state": "blocked_human",
        "stage_outputs": outputs,
    }
    if outcome.action == "BLOCK_WITH_TIMEOUT":
        ledger.record_event(
            state["session_id"],
            "blocked_with_timeout",
            {
                "stage": "assembly_review",
                "max_blocked_seconds": outcome.max_blocked_seconds,
                "review_request_artifact_ref": request_ref.model_dump(mode="json"),
                "policy_mode": outcome.mode,
            },
        )
    if outcome.action != "AUTO_DECIDE_NOW":
        return blocked_update

    try:
        recommendation = recommend_review_decision(
            session_id=state["session_id"],
            stage="assembly_review",
            review_request_id=persisted_request.review_request_id,
            request_ref=persisted_request.request_ref,
            request_hash=persisted_request.request_hash,
            data_dir=data_dir,
        )
        from app.control.lifecycle import resume_review_state

        auto_update = resume_review_state({**state, **blocked_update}, recommendation)
    except (RecommendationError, ResumeValidationError, ValueError) as exc:
        ledger.record_event(
            state["session_id"],
            "auto_recommendation_failed",
            {
                "stage": "assembly_review",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "review_request_artifact_ref": request_ref.model_dump(mode="json"),
                "policy_mode": outcome.mode,
            },
        )
        return blocked_update
    ledger.record_event(
        state["session_id"],
        "auto_decision_committed",
        {
            "stage": "assembly_review",
            "decision": recommendation.decision,
            "candidate_id": recommendation.candidate_id,
            "decided_by": recommendation.decided_by,
            "rationale_artifact_ref": recommendation.rationale_artifact_ref,
            "review_request_artifact_ref": request_ref.model_dump(mode="json"),
            "policy_mode": outcome.mode,
        },
    )
    return auto_update


def commit_assembly_review(state: TopLevelState) -> dict:
    """Commit the assembly review decision and advance routing."""
    from app.graph.routing import next_stage_name, skipped_stages_until_next

    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})

    decision = ar_out.get("decision", "stop_human_review")
    selected_candidate_id = ar_out.get("selected_candidate_id")
    rejected_candidates = ar_out.get("rejected_candidates", [])
    final_assembly_ref = ar_out.get("final_assembly_ref")
    pre_action_ref = ar_out.get("pre_action_assembly_ref") or final_assembly_ref
    action_type = ar_out.get("action_type", "pass")
    action_execution_mode = ar_out.get("action_execution_mode", "none")
    method_name = ar_out.get("action_method_name")
    evidence_refs = list(ar_out.get("evidence_refs") or [])
    evidence_notes = list(ar_out.get("evidence_notes") or [])

    final_for_downstream = ar_out.get("final_assembly_for_scaffolding_or_annotation") or final_assembly_ref
    trace = append_route_trace(state, "assembly_review_stage")
    next_stage = next_stage_name(
        "assembly_review",
        state.get("requested_stages"),
    )
    skip_reasons = dict(state.get("skip_reasons") or {})
    for skipped_stage in skipped_stages_until_next(
        "assembly_review",
        state.get("requested_stages"),
    ):
        skip_reasons.setdefault(skipped_stage, "not_requested")
    decided_by = ar_out.get("decided_by", "human")
    rationale_artifact_ref = ar_out.get("rationale_artifact_ref")
    action_metadata = {
        "action_type": action_type,
        "action_execution_mode": action_execution_mode,
        "pre_action_assembly_ref": pre_action_ref,
        "post_action_assembly_ref": final_assembly_ref,
        "final_assembly_for_scaffolding_or_annotation": final_for_downstream,
        "method_name": method_name,
        "evidence_refs": evidence_refs,
        "evidence_notes": evidence_notes,
        "decided_by": decided_by,
        "rationale_artifact_ref": rationale_artifact_ref,
    }

    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    decision_artifact = ReviewDecisionArtifact(
        session_id=state["session_id"],
        stage="assembly_review",
        decision=decision,
        candidate_id=selected_candidate_id,
        rejected_candidates=rejected_candidates if isinstance(rejected_candidates, list) else list(rejected_candidates),
        final_assembly_ref=final_assembly_ref,
        notes=ar_out.get("notes", []),
        metadata=action_metadata,
    )
    review_request_id = ar_out.get("review_request_id")
    review_request_hash = ar_out.get("review_request_hash")
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError("assembly_review decision is missing review request binding")
    decision_ref = persist_review_decision(
        data_dir,
        session_id=state["session_id"],
        review_request_id=review_request_id,
        stage="assembly_review",
        gate_id="assembly_review",
        decision=decision_artifact,
        request_hash=review_request_hash,
        method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
        artifact_type="assembly_review.decision",
    )
    ledger.record_review_decision(
        state["session_id"],
        "assembly_review",
        decision=decision,
        candidate_id=selected_candidate_id,
        rejected_candidates=rejected_candidates,
        notes=ar_out.get("notes", []),
        artifact_ref=decision_ref.model_dump(mode="json"),
        metadata=action_metadata,
        decided_by=decided_by,
        rationale_artifact_ref=rationale_artifact_ref,
        review_request_id=review_request_id,
    )
    action_ref = None
    if selected_candidate_id and pre_action_ref and final_assembly_ref and final_for_downstream:
        action_artifact = AssemblyReviewActionArtifact(
            session_id=state["session_id"],
            decision=decision,
            action_type=action_type,
            candidate_id=selected_candidate_id,
            pre_action_assembly_ref=pre_action_ref,
            post_action_assembly_ref=final_assembly_ref,
            final_assembly_for_scaffolding_or_annotation=final_for_downstream,
            action_execution_mode=action_execution_mode,
            method_name=method_name,
            evidence_refs=evidence_refs,
            evidence_notes=evidence_notes,
            notes=ar_out.get("notes", []),
            metadata={"review_decision_artifact_ref": decision_ref.model_dump(mode="json")},
        )
        action_ref = _write_json_artifact(
            state,
            "assembly_review_action.json",
            action_artifact.model_dump(mode="json"),
            artifact_type=ASSEMBLY_REVIEW_ACTION,
        )

    update: dict[str, Any] = {
        "current_stage": next_stage,
        "supervision_state": "completed" if next_stage == "done" else "running_stage",
        "route_trace": trace,
        "skip_reasons": skip_reasons,
    }

    if selected_candidate_id:
        committed_out = {
            **ar_out,
            "status": "completed",
            "decision": decision,
            "selected_candidate_id": selected_candidate_id,
            "rejected_candidates": rejected_candidates,
            "final_assembly_ref": final_assembly_ref,
            "final_assembly_for_scaffolding_or_annotation": final_for_downstream,
            "pre_action_assembly_ref": pre_action_ref,
            "action_type": action_type,
            "action_execution_mode": action_execution_mode,
            "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
            "assembly_review_action_artifact_ref": action_ref,
            "decided_by": decided_by,
            "rationale_artifact_ref": rationale_artifact_ref,
        }
        if action_type == "purge":
            committed_out["purged_assembly"] = final_assembly_ref
        if action_type == "polish":
            committed_out["polished_assembly"] = final_assembly_ref
        if method_name:
            committed_out["action_method_name"] = method_name
        update["stage_outputs"] = {**stage_outputs, "assembly_review": committed_out}

    return update


def apply_assembly_review_resume_decision(state: TopLevelState, request: ResumeReviewRequest) -> dict:
    """Apply a validated assembly review decision to a blocked state."""

    if request.stage != "assembly_review":
        raise ResumeValidationError("assembly review resume requires stage assembly_review")
    ar_out = dict((state.get("stage_outputs") or {}).get("assembly_review") or {})
    active_interrupt = state.get("active_interrupt") or {}
    review_request_id = request.review_request_id or active_interrupt.get("review_request_id")
    review_request_hash = request.review_request_hash or active_interrupt.get("request_hash")
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError("assembly_review decision is missing review request binding")
    candidates = ar_out.get("candidates") or []
    candidates_by_id = {candidate.get("candidate_id"): candidate for candidate in candidates}
    allowed = {"accept", "pass", "purge", "polish", "continue_low_confidence", "stop_human_review", "rerun"}
    if request.decision not in allowed:
        raise ResumeValidationError(f"unsupported assembly_review decision: {request.decision}")

    if request.decision == "stop_human_review":
        return _stop_assembly_review(state, request)

    if not request.candidate_id:
        raise ResumeValidationError("assembly_review resume requires candidate_id")
    if request.candidate_id not in candidates_by_id:
        raise ResumeValidationError(f"unknown assembly_review candidate_id: {request.candidate_id}")

    if request.decision == "rerun":
        return _apply_rerun_decision(state, request)

    candidate_final_ref = candidates_by_id[request.candidate_id].get("final_assembly_ref")
    action_payload = _assembly_review_action_payload(request, candidate_final_ref)

    stage_outputs = dict(state.get("stage_outputs") or {})
    stage_outputs["assembly_review"] = {
        **ar_out,
        "decision": request.decision,
        "selected_candidate_id": request.candidate_id,
        "rejected_candidates": request.rejected_candidates,
        "final_assembly_ref": action_payload["final_assembly_ref"],
        "final_assembly_for_scaffolding_or_annotation": action_payload["final_assembly_for_scaffolding_or_annotation"],
        "pre_action_assembly_ref": action_payload["pre_action_assembly_ref"],
        "action_type": action_payload["action_type"],
        "action_execution_mode": action_payload["action_execution_mode"],
        "action_method_name": action_payload.get("action_method_name"),
        "evidence_refs": action_payload.get("evidence_refs", []),
        "evidence_notes": action_payload.get("evidence_notes", []),
        "notes": request.notes,
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
        "review_request_id": review_request_id,
        "review_request_hash": review_request_hash,
    }
    if action_payload["action_type"] == "purge":
        stage_outputs["assembly_review"]["purged_assembly"] = action_payload["final_assembly_ref"]
    if action_payload["action_type"] == "polish":
        stage_outputs["assembly_review"]["polished_assembly"] = action_payload["final_assembly_ref"]
    updated_state = {**state, "stage_outputs": stage_outputs, "active_interrupt": None, "supervision_state": "running_stage"}
    commit_update = commit_assembly_review(updated_state)
    return {**commit_update, "active_interrupt": None}


def _assembly_review_action_payload(request: ResumeReviewRequest, candidate_final_ref: str | None) -> dict[str, Any]:
    if request.decision in {"accept", "pass", "continue_low_confidence"}:
        if not request.final_assembly_ref:
            raise ResumeValidationError("assembly_review resume requires final_assembly_ref")
        if candidate_final_ref and request.final_assembly_ref != candidate_final_ref:
            raise ResumeValidationError("assembly_review final_assembly_ref does not match selected candidate")
        action_type = "pass_low_confidence" if request.decision == "continue_low_confidence" else "pass"
        return {
            "final_assembly_ref": request.final_assembly_ref,
            "final_assembly_for_scaffolding_or_annotation": request.final_assembly_ref,
            "pre_action_assembly_ref": request.final_assembly_ref,
            "action_type": action_type,
            "action_execution_mode": "none",
            "evidence_refs": list(request.metadata.get("evidence_refs") or []),
            "evidence_notes": list(request.metadata.get("evidence_notes") or []),
        }

    if not candidate_final_ref:
        raise ResumeValidationError("assembly_review transform action requires selected candidate final_assembly_ref")
    metadata_output_ref = request.metadata.get("action_output_ref")
    if metadata_output_ref and request.final_assembly_ref and metadata_output_ref != request.final_assembly_ref:
        raise ResumeValidationError(f"assembly_review {request.decision} action_output_ref must match final_assembly_ref")
    action_output_ref = metadata_output_ref or request.final_assembly_ref
    if not action_output_ref:
        raise ResumeValidationError(f"assembly_review {request.decision} requires action_output_ref")
    if action_output_ref == candidate_final_ref:
        raise ResumeValidationError(f"assembly_review {request.decision} output must differ from pre-action assembly")
    if not Path(str(action_output_ref)).exists():
        raise ResumeValidationError(f"assembly_review {request.decision} action output ref must exist")
    # Trust boundary: human_approved_action is user-asserted, not system-verified.
    # This documents that a human performed and approved an external action.
    # Additional validation (output exists, differs from input, has evidence) reduces risk.
    if not (request.metadata.get("human_approved_action") is True or request.metadata.get("approval") == "human"):
        raise ResumeValidationError(f"assembly_review {request.decision} requires human-approved action metadata")
    evidence_refs = list(request.metadata.get("evidence_refs") or [])
    evidence_notes = list(request.metadata.get("evidence_notes") or [])
    if not evidence_refs and not evidence_notes:
        raise ResumeValidationError(f"assembly_review {request.decision} requires action evidence metadata")
    method_name = request.method_name or request.metadata.get("method_name")
    if not method_name and not request.metadata.get("provenance_ref") and not request.metadata.get("provenance"):
        raise ResumeValidationError(f"assembly_review {request.decision} requires method or provenance metadata")
    if request.decision == "polish" and method_name in {"nextpolish_shortread", "pilon_shortread"}:
        if not request.metadata.get("short_read_evidence_refs") and not request.metadata.get("short_read_evidence_notes"):
            raise ResumeValidationError("short-read polishing requires explicit short-read evidence")
    return {
        "final_assembly_ref": str(action_output_ref),
        "final_assembly_for_scaffolding_or_annotation": str(action_output_ref),
        "pre_action_assembly_ref": candidate_final_ref,
        "action_type": request.decision,
        "action_execution_mode": "external",
        "action_method_name": method_name,
        "evidence_refs": evidence_refs,
        "evidence_notes": evidence_notes,
    }


def _stop_assembly_review(state: TopLevelState, request: ResumeReviewRequest) -> dict:
    stage_outputs = dict(state.get("stage_outputs") or {})
    ar_out = dict(stage_outputs.get("assembly_review") or {})
    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    decision_artifact = ReviewDecisionArtifact(
        session_id=state["session_id"],
        stage="assembly_review",
        decision="stop_human_review",
        candidate_id=request.candidate_id,
        rejected_candidates=request.rejected_candidates,
        final_assembly_ref=request.final_assembly_ref,
        notes=request.notes,
        metadata=request.metadata,
    )
    active_interrupt = state.get("active_interrupt") or {}
    review_request_id = request.review_request_id or active_interrupt.get("review_request_id")
    review_request_hash = request.review_request_hash or active_interrupt.get("request_hash")
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError("assembly_review stop decision is missing review request binding")
    decision_ref = persist_review_decision(
        data_dir,
        session_id=state["session_id"],
        review_request_id=review_request_id,
        stage="assembly_review",
        gate_id="assembly_review",
        decision=decision_artifact,
        request_hash=review_request_hash,
        method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
        artifact_type="assembly_review.decision",
    )
    ledger.record_review_decision(
        state["session_id"],
        "assembly_review",
        decision="stop_human_review",
        candidate_id=request.candidate_id,
        rejected_candidates=request.rejected_candidates,
        notes=request.notes,
        artifact_ref=decision_ref.model_dump(mode="json"),
        metadata=request.metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        review_request_id=review_request_id,
    )
    stage_outputs["assembly_review"] = {
        **ar_out,
        "status": "failed_terminal",
        "decision": "stop_human_review",
        "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
        "notes": request.notes,
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
    }
    return {
        "current_stage": "assembly_review",
        "supervision_state": "failed_terminal",
        "active_interrupt": None,
        "stage_outputs": stage_outputs,
        "last_error": "assembly_review stopped by human review decision",
    }


def _apply_rerun_decision(state: TopLevelState, request: ResumeReviewRequest) -> dict:
    """Execute a Flye rerun with adjusted parameters and re-enter assembly_review."""
    from app.stages.assembly.nodes import FLYE_METHOD, FLYE_READ_TYPE_FLAGS

    stage_outputs = dict(state.get("stage_outputs") or {})
    ar_out = dict(stage_outputs.get("assembly_review") or {})
    assembly_out = dict(stage_outputs.get("assembly") or {})
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    ledger = Ledger(data_dir)

    rerun_params = request.metadata.get("rerun_parameters")
    if not isinstance(rerun_params, dict):
        raise ResumeValidationError("rerun requires metadata.rerun_parameters dict")

    params, error = _validate_rerun_flye_parameters(rerun_params, FLYE_READ_TYPE_FLAGS)
    if error:
        raise ResumeValidationError(f"rerun flye_parameters invalid: {error}")

    max_reruns = ar_out.get("max_reruns", 3)
    existing_refs = list(assembly_out.get("selected_candidate_refs") or [])
    selected_rerun_indices = _rerun_indices_from_refs(existing_refs)
    completed_rerun_count = len(selected_rerun_indices)
    if completed_rerun_count >= max_reruns:
        raise ResumeValidationError(
            f"rerun limit reached ({completed_rerun_count}/{max_reruns}); "
            "adjust max_reruns in run_config or accept a candidate"
        )

    input_ref = assembly_out.get("input_ref")
    input_type = assembly_out.get("input_type")
    if not input_ref:
        raise ResumeValidationError("rerun requires assembly stage input_ref (no inventory rebuild)")

    candidate_dir = stage_artifacts_dir(state, "assembly", "candidates")
    read_type = params["read_type"]
    genome_size = params["estimated_genome_size"]
    threads = params["threads"]
    rerun_index, existing_bundle_ref = _next_rerun_index(
        state,
        candidate_dir,
        max_reruns=max_reruns,
        selected_rerun_indices=selected_rerun_indices,
    )
    active_interrupt = state.get("active_interrupt") or {}
    review_request_id = request.review_request_id or active_interrupt.get("review_request_id")
    review_request_hash = request.review_request_hash or active_interrupt.get("request_hash")
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError("assembly_review rerun is missing review request binding")
    review_decision_ref = persist_review_decision(
        data_dir,
        session_id=session_id,
        review_request_id=review_request_id,
        stage="assembly_review",
        gate_id="assembly_review",
        decision=request,
        request_hash=review_request_hash,
        method_name=ASSEMBLY_REVIEW_CONTRACT_METHOD,
        artifact_type="assembly_review.decision",
    )
    ledger.record_review_decision(
        session_id,
        "assembly_review",
        decision="rerun",
        candidate_id=request.candidate_id,
        notes=list(request.notes),
        artifact_ref=review_decision_ref.model_dump(mode="json"),
        metadata=request.metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        review_request_id=review_request_id,
    )
    candidate_id = f"{session_id}:{FLYE_METHOD}:rerun_{rerun_index}"
    working_dir = stage_work_dir(state, "assembly", f"flye_rerun_{rerun_index}")
    flye_out_dir = working_dir / "flye_out"
    artifact_dir = stage_artifacts_dir(state, "assembly", f"flye_rerun_{rerun_index}")
    draft_assembly = artifact_dir / "draft_assembly.fasta"
    script_path = working_dir / "run_flye.sh"
    bundle_path = candidate_dir / f"flye_rerun_{rerun_index}_candidate.json"

    if existing_bundle_ref is not None:
        _load_completed_rerun_bundle_if_matching(
            bundle_path,
            session_id=session_id,
            candidate_id=candidate_id,
            method_name=FLYE_METHOD,
            input_ref=input_ref,
            input_type=input_type,
            params=params,
            source_candidate_id=request.candidate_id,
            rerun_index=rerun_index,
        )
        if existing_bundle_ref not in existing_refs:
            existing_refs = [*existing_refs, existing_bundle_ref]
        assembly_out["selected_candidate_refs"] = existing_refs
        preserved_keys = {"execution_mode", "env", "quast_parameters", "busco_parameters", "review_policy", "max_reruns"}
        preserved = {k: ar_out[k] for k in preserved_keys if k in ar_out}
        stage_outputs["assembly"] = assembly_out
        stage_outputs["assembly_review"] = preserved
        return {
            "current_stage": "assembly_review",
            "supervision_state": "running_stage",
            "active_interrupt": None,
            "stage_outputs": stage_outputs,
        }

    ledger.record_event(session_id, "rerun_requested", {
        "candidate_id": candidate_id,
        "rerun_index": rerun_index,
        "parameters": params,
        "rerun_reason": request.metadata.get("rerun_reason"),
        "source_candidate_id": request.candidate_id,
    })

    read_paths, read_error = _rerun_read_paths(input_ref, input_type)
    if read_error:
        ledger.record_event(session_id, "rerun_failed", {
            "candidate_id": candidate_id, "error": read_error,
        })
        raise ResumeValidationError(f"rerun read path resolution failed: {read_error}")

    env_spec, _packages, blocked = resolve_method_env(
        assembly_out,
        stage="assembly",
        method_name=FLYE_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        ledger.record_event(session_id, "rerun_failed", {
            "candidate_id": candidate_id, "error": "env_blocked",
        })
        raise ResumeValidationError("rerun blocked on environment resolution")

    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    read_flag = FLYE_READ_TYPE_FLAGS[read_type]
    reads_arg = " ".join(shlex.quote(str(p)) for p in read_paths)
    script_path.write_text(
        "set -e\n"
        f"flye {read_flag} {reads_arg}"
        f" --out-dir {shlex.quote(str(flye_out_dir))}"
        f" --genome-size {shlex.quote(str(genome_size))}"
        f" --threads {threads}\n"
        f"test -f {shlex.quote(str(flye_out_dir / 'assembly.fasta'))}\n"
        f"{shell_publish_file(flye_out_dir / 'assembly.fasta', draft_assembly)}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:assembly:flye:rerun_{rerun_index}",
        session_id=session_id,
        stage="assembly",
        method_name=FLYE_METHOD,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[draft_assembly],
        env=assembly_out.get("env"),
        env_spec=env_spec,
        metadata={
            "input_ref": input_ref,
            "input_type": input_type,
            "read_paths": [str(p) for p in read_paths],
            "read_profile": read_type,
            "estimated_genome_size": genome_size,
            "threads": threads,
            "flye_output_dir": str(flye_out_dir),
            "draft_assembly_path": str(draft_assembly),
            "rerun_index": rerun_index,
            "source_candidate_id": request.candidate_id,
        },
    )

    # Rerun is a resume-path special case: it reuses the shared run core (so
    # attempt_started/attempt_failed bookkeeping matches normal runs) but adds a
    # rerun_failed event and raises ResumeValidationError instead of writing a
    # failed stage status. cleanup_outputs=False preserves partial outputs for
    # post-mortem, matching the pre-helper behavior.
    outcome = run_frozen_attempt(
        attempt,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label="flye rerun",
        cleanup_outputs=False,
        ledger=ledger,
    )
    result = outcome.result
    execution_ref = persist_execution_result(
        state,
        stage="assembly",
        method_name=FLYE_METHOD,
        result=result,
        error=outcome.error,
    )
    execution = load_execution_evidence(
        state,
        execution_ref,
        expected_stage="assembly",
        expected_attempt_id=attempt.attempt_id,
        expected_method_name=FLYE_METHOD,
    )

    if not outcome.success:
        ledger.record_event(session_id, "rerun_failed", {
            "candidate_id": candidate_id, "error": outcome.error, "rerun_index": rerun_index,
        })
        raise ResumeValidationError(f"rerun Flye execution failed: {outcome.error}")

    if not draft_assembly.exists():
        error_msg = f"rerun Flye draft assembly missing: {draft_assembly}"
        ledger.record_attempt_failed(session_id, attempt, return_code=0, error=error_msg)
        ledger.record_event(session_id, "rerun_failed", {
            "candidate_id": candidate_id, "error": error_msg,
        })
        raise ResumeValidationError(error_msg)

    artifacts = [
        ArtifactMeta.from_file(
            draft_assembly,
            stage="assembly",
            method_name=FLYE_METHOD,
            artifact_type="assembly.draft_assembly",
            metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id, "rerun_index": rerun_index},
        )
    ]
    append_artifact_records(data_dir, session_id, artifacts)
    artifact_payloads = [a.model_dump(mode="json") for a in artifacts]
    ledger.record_attempt_succeeded(session_id, attempt, artifacts=artifact_payloads)
    for a in artifact_payloads:
        ledger.record_artifact_registered(session_id, a, attempt_id=attempt.attempt_id)

    bundle = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id=candidate_id,
        method_name=FLYE_METHOD,
        status="executed",
        input_ref=str(input_ref),
        input_type=str(input_type),
        genome_size_prior=assembly_out.get("genome_size_prior"),
        expected_outputs={"draft_assembly_fasta": str(draft_assembly)},
        logs={
            "stdout": execution.stdout_ref.path_ref,
            "stderr": execution.stderr_ref.path_ref,
        },
        metadata={
            "execution_enabled": True,
            "attempt_id": attempt.attempt_id,
            "draft_assembly_artifact_ref": str(draft_assembly),
            "draft_assembly_ref_status": "materialized",
            "read_profile": read_type,
            "estimated_genome_size": genome_size,
            "threads": threads,
            "rerun_index": rerun_index,
            "source_candidate_id": request.candidate_id,
            "rerun_reason": request.metadata.get("rerun_reason"),
            "execution_ref": execution_ref.model_dump(mode="json"),
        },
    )
    candidate_dir.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bundle_artifact = ArtifactMeta.from_file(
        bundle_path, stage="assembly", method_name=FLYE_METHOD,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={"candidate_id": candidate_id, "execution_enabled": True, "rerun_index": rerun_index},
    )
    append_artifact_records(data_dir, session_id, [bundle_artifact])
    ledger.record_artifact_registered(session_id, bundle_artifact.model_dump(mode="json"), attempt_id=attempt.attempt_id)

    bundle_ref = str(bundle_path)
    updated_refs = existing_refs if bundle_ref in existing_refs else [*existing_refs, bundle_ref]
    assembly_out["selected_candidate_refs"] = updated_refs

    ledger.record_event(session_id, "rerun_completed", {
        "candidate_id": candidate_id,
        "rerun_index": rerun_index,
        "parameters": params,
        "draft_assembly_ref": str(draft_assembly),
        "bundle_ref": str(bundle_path),
    })

    preserved_keys = {"execution_mode", "env", "quast_parameters", "busco_parameters", "review_policy", "max_reruns"}
    preserved = {k: ar_out[k] for k in preserved_keys if k in ar_out}

    stage_outputs["assembly"] = assembly_out
    stage_outputs["assembly_review"] = preserved

    return {
        "current_stage": "assembly_review",
        "supervision_state": "running_stage",
        "active_interrupt": None,
        "stage_outputs": stage_outputs,
    }


def _validate_rerun_flye_parameters(
    params: dict[str, Any],
    read_type_flags: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    read_type = params.get("read_type")
    if not read_type or read_type not in read_type_flags:
        return {}, f"read_type must be one of {sorted(read_type_flags)}"
    raw_genome_size = params.get("estimated_genome_size")
    try:
        genome_size = int(raw_genome_size)
    except (TypeError, ValueError):
        return {}, "estimated_genome_size must be a positive integer"
    if genome_size <= 0:
        return {}, "estimated_genome_size must be a positive integer"
    raw_threads = params.get("threads")
    try:
        threads = int(raw_threads)
    except (TypeError, ValueError):
        return {}, "threads must be a positive integer"
    if threads <= 0:
        return {}, "threads must be a positive integer"
    return {"read_type": read_type, "estimated_genome_size": genome_size, "threads": threads}, None


def _next_rerun_index(
    state: TopLevelState,
    candidate_dir: Path,
    *,
    max_reruns: int,
    selected_rerun_indices: set[int],
) -> tuple[int, str | None]:
    index = 1
    while index <= max_reruns:
        if index in selected_rerun_indices:
            index += 1
            continue
        bundle_path = candidate_dir / f"flye_rerun_{index}_candidate.json"
        if bundle_path.exists():
            return index, str(bundle_path)
        working_dir = stage_work_dir(state, "assembly", f"flye_rerun_{index}")
        artifact_dir = stage_artifacts_dir(state, "assembly", f"flye_rerun_{index}")
        if working_dir.exists() or artifact_dir.exists():
            index += 1
            continue
        return index, None
    raise ResumeValidationError(
        f"rerun limit reached ({max_reruns}/{max_reruns}); "
        "adjust max_reruns in run_config or accept a candidate"
    )


def _rerun_indices_from_refs(refs: list[str]) -> set[int]:
    indices: set[int] = set()
    for ref in refs:
        match = re.search(r"rerun_(\d+)", str(ref))
        if match:
            indices.add(int(match.group(1)))
    return indices


def _load_completed_rerun_bundle_if_matching(
    bundle_path: Path,
    *,
    session_id: str,
    candidate_id: str,
    method_name: str,
    input_ref: Any,
    input_type: Any,
    params: dict[str, Any],
    source_candidate_id: str | None,
    rerun_index: int,
) -> dict[str, Any] | None:
    if not bundle_path.exists():
        return None
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ResumeValidationError(f"existing rerun bundle is invalid JSON: {bundle_path}") from exc

    metadata = bundle.get("metadata") or {}
    expected = {
        "session_id": session_id,
        "candidate_id": candidate_id,
        "method_name": method_name,
        "status": "executed",
        "input_ref": str(input_ref),
        "input_type": str(input_type),
    }
    for key, value in expected.items():
        if bundle.get(key) != value:
            raise ResumeValidationError(
                f"existing rerun bundle conflicts with requested rerun ({key}: {bundle.get(key)!r} != {value!r})"
            )
    metadata_expected = {
        "rerun_index": rerun_index,
        "source_candidate_id": source_candidate_id,
        "read_profile": params["read_type"],
        "estimated_genome_size": params["estimated_genome_size"],
        "threads": params["threads"],
    }
    for key, value in metadata_expected.items():
        if metadata.get(key) != value:
            raise ResumeValidationError(
                f"existing rerun bundle conflicts with requested rerun (metadata.{key}: {metadata.get(key)!r} != {value!r})"
            )

    draft_ref = (bundle.get("expected_outputs") or {}).get("draft_assembly_fasta")
    if not draft_ref or not Path(str(draft_ref)).exists():
        raise ResumeValidationError(f"existing rerun bundle is incomplete; draft assembly missing: {draft_ref}")
    return bundle


def _rerun_read_paths(input_ref: Any, input_type: Any) -> tuple[list[Path], str | None]:
    if not input_ref:
        return [], "rerun requires assembly input_ref"
    if input_type == "long_reads.fofn":
        fofn = Path(str(input_ref))
        if not fofn.exists():
            return [], f"rerun FOFN not found: {fofn}"
        paths = [Path(line.strip()) for line in fofn.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif input_type == "clean_long_reads":
        paths = [Path(str(input_ref))]
    else:
        return [], f"rerun does not support input_type: {input_type}"
    if not paths:
        return [], "rerun requires at least one read path"
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return [], f"rerun read paths not found: {missing}"
    return paths, None


def generate_candidate_comparison(state: TopLevelState) -> dict:
    """Generate a candidate comparison artifact when multiple candidates exist."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})
    candidates = ar_out.get("candidates", [])

    if len(candidates) < 2:
        return {}

    signals = ar_out.get("signals", [])
    assembly_out = (state.get("stage_outputs") or {}).get("assembly", {})
    comparison_candidates = []
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id", "unknown")
        entry = {
            "candidate_id": candidate_id,
            "method_name": candidate.get("method_name"),
            "source": candidate.get("source"),
            "status": candidate.get("status", "unknown"),
            "parameters": _candidate_parameters(candidate, assembly_out),
            "metrics": _candidate_metrics(candidate_id, signals),
        }
        comparison_candidates.append(entry)

    comparison_payload = {
        "session_id": state["session_id"],
        "candidates": comparison_candidates,
        "notes": ["thresholds_not_applied_without_human_approval"],
    }
    existing_ref = ar_out.get("candidate_comparison_ref")
    if existing_ref and _json_artifact_payload_matches(existing_ref, comparison_payload):
        return {}

    comparison_ref = _write_json_artifact(
        state,
        "candidate_comparison.json",
        comparison_payload,
        artifact_type=ASSEMBLY_CANDIDATE_COMPARISON,
    )

    ledger = Ledger(state.get("data_dir", "data"))
    ledger.record_event(state["session_id"], "candidate_comparison_generated", {
        "candidate_ids": [c["candidate_id"] for c in comparison_candidates],
        "candidate_count": len(comparison_candidates),
        "comparison_artifact_ref": comparison_ref,
    })

    outputs = dict(stage_outputs)
    outputs["assembly_review"] = {**ar_out, "candidate_comparison_ref": comparison_ref}
    return {"stage_outputs": outputs}


def _json_artifact_payload_matches(artifact_ref: Any, payload: dict[str, Any]) -> bool:
    path = Path(str(artifact_ref))
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return existing == payload


def _target_species_name(state: TopLevelState) -> str | None:
    """Read optional species metadata without making manifest parsing a gate."""
    manifest_ref = state.get("input_manifest_ref")
    if not manifest_ref:
        return None
    path = resolve_run_ref(state, str(manifest_ref))
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    metadata = dict(raw.get("user_metadata") or {})
    for key in ("target_species", "species_name", "scientific_name", "organism", "species", "chinese_name"):
        if key in raw:
            metadata.setdefault(key, raw[key])
    return species_name_from_metadata(metadata)


def _ensure_assembly_review_reports(
    state: TopLevelState,
    ar_out: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    candidates = ar_out.get("candidates", [])
    report_kind = "candidate_comparison_review" if len(candidates) >= 2 else "single_candidate_review"
    payload = _assembly_review_report_payload(state, ar_out, report_kind=report_kind)
    data_ref = _write_report_json_artifact_if_changed(
        state,
        "assembly_review_packet.json",
        payload,
        artifact_type=ASSEMBLY_REVIEW_PACKET,
    )
    report_ref = _write_derived_report_text(
        state,
        "assembly_review.md",
        _render_assembly_review_markdown(payload, title="Assembly Review Report"),
    )

    updated = {
        **ar_out,
        "assembly_review_report_ref": report_ref,
        "assembly_review_packet_ref": data_ref,
    }
    report_refs = {
        "assembly_review_report": report_ref,
        "review_packet": data_ref,
    }
    if ar_out.get("candidate_comparison_ref"):
        report_refs["candidate_comparison"] = str(ar_out["candidate_comparison_ref"])
    if ar_out.get("related_species_evidence_pack_ref"):
        report_refs["related_species_evidence_pack"] = str(ar_out["related_species_evidence_pack_ref"])

    comparison_context = _candidate_comparison_context(updated)
    return updated, report_refs, comparison_context


def _assembly_review_report_payload(
    state: TopLevelState,
    ar_out: dict[str, Any],
    *,
    report_kind: str,
) -> dict[str, Any]:
    candidates = ar_out.get("candidates", [])
    signals = ar_out.get("signals", [])
    assembly_out = (state.get("stage_outputs") or {}).get("assembly", {})
    eval_bundle = _read_eval_bundle(ar_out.get("eval_bundle_ref"))
    review_plan = ar_out.get("review_plan") or {}
    return {
        "artifact_type": ASSEMBLY_REVIEW_PACKET,
        "session_id": state["session_id"],
        "stage": "assembly_review",
        "report_kind": report_kind,
        "candidate_count": len(candidates),
        "candidates": [
            _candidate_report_row(candidate, signals, assembly_out)
            for candidate in candidates
        ],
        "route_evidence": {
            "planned_signals": list(review_plan.get("planned_signals") or []),
            "available_signals": list(review_plan.get("available_signals") or []),
            "missing_signals": list(review_plan.get("missing_signals") or []),
            "available_inputs": list(eval_bundle.get("available_inputs") or []),
            "missing_inputs": list(eval_bundle.get("missing_inputs") or []),
        },
        "raw_refs": {
            "eval_bundle_ref": ar_out.get("eval_bundle_ref"),
            "candidate_comparison_ref": ar_out.get("candidate_comparison_ref"),
            "related_species_evidence_pack_ref": ar_out.get("related_species_evidence_pack_ref"),
        },
        "related_species_sanity_check": dict(ar_out.get("related_species_evidence_pack") or {}),
        "notes": [
            "thresholds_not_applied_without_human_approval",
            "report_informs_human_decision_only",
        ],
    }


def _candidate_report_row(
    candidate: dict[str, Any],
    signals: list[dict[str, Any]],
    assembly_out: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = candidate.get("candidate_id", "unknown")
    candidate_signals = [
        signal for signal in signals
        if signal.get("candidate_id") == candidate_id
    ]
    return {
        "candidate_id": candidate_id,
        "method_name": candidate.get("method_name"),
        "source": candidate.get("source"),
        "status": candidate.get("status", "unknown"),
        "final_assembly_ref": candidate.get("final_assembly_ref") or candidate.get("assembly_ref"),
        "parameters": _candidate_parameters(candidate, assembly_out),
        "metrics": _candidate_metrics(candidate_id, signals),
        "signals": _signal_status_buckets(candidate_signals),
        "metric_refs_by_signal": {
            str(signal.get("signal_name")): [str(ref) for ref in signal.get("metric_refs", [])]
            for signal in candidate_signals
            if signal.get("metric_refs")
        },
        "signal_notes_by_signal": {
            str(signal.get("signal_name")): list(signal.get("notes") or [])
            for signal in candidate_signals
            if signal.get("notes")
        },
    }


def _signal_status_buckets(signals: list[dict[str, Any]]) -> dict[str, list[str]]:
    buckets = {"available": [], "missing": [], "failed": [], "pending": []}
    for signal in signals:
        status = signal.get("status", "pending")
        signal_name = signal.get("signal_name")
        if status in buckets and signal_name:
            buckets[status].append(str(signal_name))
    return {key: sorted(values) for key, values in buckets.items()}


def _candidate_comparison_context(ar_out: dict[str, Any]) -> dict[str, Any]:
    candidates = ar_out.get("candidates", [])
    context: dict[str, Any] = {
        "candidate_ids": [candidate.get("candidate_id") for candidate in candidates if candidate.get("candidate_id")],
        "candidate_final_assembly_refs": {
            str(candidate.get("candidate_id")): str(candidate.get("final_assembly_ref") or candidate.get("assembly_ref"))
            for candidate in candidates
            if candidate.get("candidate_id") and (candidate.get("final_assembly_ref") or candidate.get("assembly_ref"))
        },
    }
    for key in ("candidate_comparison_ref",):
        if ar_out.get(key):
            context[key] = ar_out[key]
    return context


def _render_assembly_review_markdown(payload: dict[str, Any], *, title: str) -> str:
    lines = [
        f"# {title}",
        "",
        "No automatic biological thresholds are applied. This report informs the human decision only.",
        "",
        "## Candidate Metrics",
        "",
        "| Candidate | Method | Source | Assembly ref | QUAST contigs | Total length | N50 | Largest contig | BUSCO C% | BUSCO S% | BUSCO D% | BUSCO F% | BUSCO M% | Available signals | Missing/failed/pending signals |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for candidate in payload.get("candidates", []):
        metrics = candidate.get("metrics") or {}
        quast = metrics.get("quast") or {}
        busco = metrics.get("busco") or {}
        signal_buckets = candidate.get("signals") or {}
        unavailable = []
        for status in ("missing", "failed", "pending"):
            for signal_name in signal_buckets.get(status, []):
                unavailable.append(f"{signal_name} ({status})")
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in [
                    candidate.get("candidate_id"),
                    candidate.get("method_name"),
                    candidate.get("source"),
                    candidate.get("final_assembly_ref"),
                    quast.get("contig_count"),
                    quast.get("total_length"),
                    quast.get("n50"),
                    quast.get("largest_contig"),
                    busco.get("complete_percent"),
                    busco.get("single_copy_percent"),
                    busco.get("duplicated_percent"),
                    busco.get("fragmented_percent"),
                    busco.get("missing_percent"),
                    ", ".join(signal_buckets.get("available", [])),
                    ", ".join(unavailable),
                ]
            )
            + " |"
        )

    lines.extend([
        "",
        "## Parameter Provenance",
        "",
        "| Candidate | Read profile | Estimated genome size | Threads | Rerun index |",
        "| --- | --- | --- | --- | --- |",
    ])
    for candidate in payload.get("candidates", []):
        params = candidate.get("parameters") or {}
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in [
                    candidate.get("candidate_id"),
                    params.get("read_profile"),
                    params.get("estimated_genome_size"),
                    params.get("threads"),
                    params.get("rerun_index"),
                ]
            )
            + " |"
        )

    _append_related_species_sanity_check(lines, payload.get("related_species_sanity_check") or {})

    lines.extend(["", "## Evidence Refs", ""])
    raw_refs = payload.get("raw_refs") or {}
    for key, value in raw_refs.items():
        if value:
            lines.append(f"- {key}: `{value}`")
    for candidate in payload.get("candidates", []):
        metric_refs = candidate.get("metric_refs_by_signal") or {}
        for signal_name, refs in metric_refs.items():
            for ref in refs:
                lines.append(f"- {candidate.get('candidate_id')} {signal_name}: `{ref}`")

    lines.extend(["", "## Missing Evidence", ""])
    for candidate in payload.get("candidates", []):
        signal_buckets = candidate.get("signals") or {}
        notes_by_signal = candidate.get("signal_notes_by_signal") or {}
        for status in ("missing", "failed", "pending"):
            for signal_name in signal_buckets.get(status, []):
                notes = ", ".join(notes_by_signal.get(signal_name, []))
                suffix = f" — {notes}" if notes else ""
                lines.append(f"- {candidate.get('candidate_id')} {signal_name}: {status}{suffix}")

    lines.extend(["", "## Report Notes", ""])
    for note in payload.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _append_related_species_sanity_check(lines: list[str], pack: dict[str, Any]) -> None:
    lines.extend(["", "## Related-species sanity check", ""])
    if not pack:
        lines.append("- Reference evidence was not generated.")
        return
    target = pack.get("target") or {}
    lines.extend([
        f"- Target: {_markdown_cell(target.get('canonical_scientific_name') or target.get('input_name'))}",
        f"- Matching mode: {_markdown_cell(pack.get('matching_mode'))}",
        "- This evidence is comparative only; it is not an assembly prior or automatic-failure threshold.",
        "",
    ])
    records = pack.get("selected_records") or []
    if records:
        lines.extend([
            "| Organism | Accession | Assembly | Total length | Chromosomes | Contig N50 | Scaffold N50 | GC | BUSCO lineage |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for record in records:
            busco = record.get("busco") or {}
            lines.append(
                "| " + " | ".join(
                    _markdown_cell(value) for value in [
                        record.get("organism_name"), record.get("accession"), record.get("assembly_name"),
                        record.get("total_sequence_length_bp"), record.get("chromosome_count"),
                        record.get("contig_n50_bp"), record.get("scaffold_n50_bp"), record.get("gc_percent"),
                        busco.get("lineage"),
                    ]
                ) + " |"
            )
    else:
        lines.append("- No useful related-species primary assemblies were found.")
    excluded = pack.get("excluded_alternate_haplotype_records") or []
    if excluded:
        lines.append(f"- Excluded alternate-haplotype records: {', '.join(str(record.get('accession')) for record in excluded)}")
    summary = pack.get("computed_summary") or {}
    if summary:
        lines.extend(["", "Computed summary:", "", "```json", json.dumps(summary, ensure_ascii=False, sort_keys=True), "```"])
    for note in pack.get("notes") or []:
        lines.append(f"- Reference note: {note}")


def _markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _write_report_json_artifact_if_changed(
    state: TopLevelState,
    filename: str,
    payload: dict[str, Any],
    *,
    artifact_type: str,
) -> str:
    return _write_json_artifact(
        state,
        filename,
        payload,
        artifact_type=artifact_type,
    )


def _write_derived_report_text(
    state: TopLevelState,
    filename: str,
    text: str,
) -> str:
    """Overwrite the optional Markdown projection without indexing it."""

    path = session_paths(state).report_path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = stage_work_dir(
        state,
        "assembly_review",
        "reports",
        f".{filename}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return str(path)



def _candidate_parameters(candidate: dict[str, Any], assembly_out: dict[str, Any]) -> dict[str, Any]:
    bundle_ref = candidate.get("bundle_ref")
    if bundle_ref and Path(str(bundle_ref)).exists():
        bundle = json.loads(Path(str(bundle_ref)).read_text(encoding="utf-8"))
        metadata = bundle.get("metadata") or {}
        params = {}
        for key in ("read_profile", "estimated_genome_size", "threads", "rerun_index"):
            if key in metadata:
                params[key] = metadata[key]
        if params:
            return params
    if candidate.get("source") == "assembly_stage":
        return {
            "read_profile": assembly_out.get("flye_parameters", {}).get("read_type"),
            "estimated_genome_size": assembly_out.get("flye_parameters", {}).get("estimated_genome_size"),
            "threads": assembly_out.get("flye_parameters", {}).get("threads"),
        }
    return {}


def _candidate_metrics(candidate_id: str, signals: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for signal in signals:
        if signal.get("candidate_id") != candidate_id:
            continue
        if signal.get("status") != "available":
            continue
        signal_name = signal.get("signal_name")
        signal_metrics = (signal.get("metadata") or {}).get("metrics")
        if signal_name and signal_metrics:
            metrics[signal_name] = signal_metrics
    return metrics


def fail_assembly_review(state: TopLevelState) -> dict:
    """Record assembly review failure and stop."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})
    status = ar_out.get("status", "failed")
    default_reason = {
        "blocked_env": "assembly_review blocked on missing conda environment",
        "missing_tool": "assembly_review blocked on missing tool",
    }.get(status, "assembly_review failed")
    return {
        "current_stage": "assembly_review",
        "supervision_state": "failed_terminal",
        "last_error": ar_out.get("reason", default_reason),
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


_EVAL_SIGNAL_NAMES = {"quast", "busco"}


def _assembly_review_auto_decidable(ar_out: dict[str, Any]) -> bool:
    candidates = ar_out.get("candidates") or []
    if not any(isinstance(c, dict) and c.get("candidate_id") for c in candidates):
        return False

    signals = [s for s in (ar_out.get("signals") or []) if isinstance(s, dict)]
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        has_quast = any(
            signal.get("signal_name") == "quast"
            and signal.get("candidate_id") == candidate_id
            and signal.get("status") == "available"
            and signal.get("metric_refs")
            for signal in signals
        )
        has_busco = any(
            signal.get("signal_name") == "busco"
            and signal.get("candidate_id") == candidate_id
            and signal.get("status") == "available"
            and signal.get("metric_refs")
            for signal in signals
        )
        if not (has_quast and has_busco):
            return False
    return True


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
    }[signal_name]
