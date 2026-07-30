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
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml

from app.control.lifecycle import ResumeValidationError
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
    ASSEMBLY_CANDIDATE_COMPARISON,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ASSEMBLY_REVIEW_ACTION,
    ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
    ASSEMBLY_REVIEW_EVAL_BUNDLE,
    ASSEMBLY_REVIEW_PACKET,
    ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
    ASSEMBLY_REVIEW_RELATED_SPECIES_STATISTICS_PACK,
    ArtifactMeta,
    ArtifactRef,
    AssemblyEvalBundle,
    AssemblyEvalSignal,
    AssemblyReviewActionArtifact,
    ReviewDecisionArtifact,
)
from app.schemas.plan import AssemblyReviewPlan
from app.schemas.state import (
    DOMAIN_STAGES,
    ActiveInterrupt,
    DomainStage,
    StateUpdate,
    TopLevelState,
    validate_stage_outputs,
)
from app.stages.assembly.method_application.support import (
    ASSEMBLY_EXECUTION_CONTRACT_KEY,
    AssemblyExecutionContract,
)
from app.stages.assembly_review.method_application.support import (
    read_eval_bundle as _read_eval_bundle,
)
from app.stages.assembly_review.method_application.support import (
    verified_review_candidate as _verified_review_candidate,
)
from app.stages.assembly_review.method_application.support import (
    write_json_artifact as _write_json_artifact,
)
from app.stages.assembly_review.notebook import (
    build_assembly_review_notebook,
    render_statistics_figures,
    write_notebook,
)
from app.stages.assembly_review.state import (
    ASSEMBLY_CANDIDATE_CURSOR_ADAPTER,
    ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER,
    ASSEMBLY_REVIEW_CURSOR_ADAPTER,
    ASSEMBLY_REVIEW_PLAN_CURSOR_ADAPTER,
    AssemblyCandidateCursor,
    AssemblyEvalSignalCursor,
    AssemblyReviewCursor,
    assembly_review_output,
    update_assembly_review_output,
)
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    resolve_run_ref,
    session_paths,
    stage_artifacts_dir,
    stage_work_dir,
)
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import ASSEMBLY_CANDIDATES_SPEC
from app.storage.artifacts import (
    PublishedArtifact,
    publish_artifact_record,
    register_artifact_records,
)
from app.storage.assembly_candidates import (
    VerifiedExecutedCandidate,
    load_executed_candidate_bundle,
    publish_derived_assembly_candidate,
)
from app.storage.ledger import Ledger
from app.workflow import is_terminal_failure

logger = logging.getLogger(__name__)

ASSEMBLY_REVIEW_CONTRACT_METHOD = "assembly_review.contract"


def enter_assembly_review(state: TopLevelState) -> StateUpdate:
    """Mark assembly_review as running.  No side effects."""
    return {"supervision_state": "running_stage"}


def resolve_candidates(state: TopLevelState) -> StateUpdate:
    """Resolve current assembly candidates from the central resource table."""
    candidates: list[AssemblyCandidateCursor] = []
    assembly = resolve_stage_resource(
        state,
        resource_spec=ASSEMBLY_CANDIDATES_SPEC,
    )
    current_ref = assembly.metadata.get("candidate_bundle_ref")
    if current_ref is None:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": "assembly resource has no executed candidate bundle ref",
                "candidate_count": 0,
                "candidates": [],
            },
        )

    candidate_inputs = [
        ("candidate_bundle_ref", current_ref, assembly.source),
    ]
    raw_ref = assembly.metadata.get("raw_candidate_bundle_ref")
    if raw_ref is not None:
        candidate_inputs.append(
            ("raw_candidate_bundle_ref", raw_ref, f"{assembly.source}:raw")
        )
    verified_by_id: dict[str, VerifiedExecutedCandidate] = {}
    try:
        for metadata_key, raw_candidate_ref, source in candidate_inputs:
            verified = load_executed_candidate_bundle(
                absolute_data_dir(state),
                state["session_id"],
                raw_candidate_ref,
            )
            if verified.ref.artifact_id in verified_by_id:
                continue
            verified_by_id[verified.ref.artifact_id] = verified
            candidates.append(
                ASSEMBLY_CANDIDATE_CURSOR_ADAPTER.validate_python(
                    {
                        "candidate_id": verified.bundle.candidate_id,
                        "method_name": verified.bundle.method_name,
                        "source": source,
                        "status": "executed",
                        "bundle_ref": verified.ref.model_dump(mode="json"),
                        "primary_output_ref": (
                            verified.primary_output_ref.model_dump(mode="json")
                        ),
                    }
                )
            )
            projected_primary_key = (
                "primary_output_ref"
                if metadata_key == "candidate_bundle_ref"
                else "raw_primary_output_ref"
            )
            projected_primary = assembly.metadata.get(projected_primary_key)
            if projected_primary is None:
                raise ValueError(
                    f"assembly resource has no {projected_primary_key}"
                )
            if (
                ArtifactRef.model_validate(projected_primary)
                != verified.primary_output_ref
            ):
                raise ValueError(
                    f"assembly resource {projected_primary_key} does not match "
                    f"{metadata_key}"
                )
    except (OSError, TypeError, ValueError) as exc:
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": f"assembly candidate handoff is invalid: {exc}",
                "candidate_count": 0,
                "candidates": [],
            },
        )

    current = verified_by_id[ArtifactRef.model_validate(current_ref).artifact_id]
    if [str(current.primary_output_path)] != list(assembly.paths):
        return update_assembly_review_output(
            state,
            {
                "status": "failed",
                "reason": (
                    "assembly resource paths do not match the current executed "
                    "candidate primary output"
                ),
                "candidate_count": 0,
                "candidates": [],
            },
        )

    # Build review plan
    review_plan = AssemblyReviewPlan(
        status="evaluating" if candidates else "failed",
        candidate_ids=[c["candidate_id"] for c in candidates],
        notes=[],
    )

    payload: dict[str, object] = {
        "status": "resolved",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "rejected_candidates": dict(assembly.metadata.get("rejected_candidates") or {}),
        "review_plan": review_plan.model_dump(mode="json"),
    }

    if not candidates:
        payload["status"] = "failed"
        payload["reason"] = "no_assembly_candidates_found"

    return update_assembly_review_output(state, payload)


def plan_review_metrics(state: TopLevelState) -> StateUpdate:
    """Plan evaluation signals based on available inputs.

    Plans signals only when inputs exist; does not execute tools.
    Thresholds are not stored here.
    """
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = assembly_review_output(state)

    if ar_out.get("status") == "failed":
        return {}

    # Determine available inputs from asset registry or prior stages
    _has_short_reads, _has_long_reads, available_inputs, missing_inputs = (
        _available_review_inputs(state)
    )

    read_qc_out = stage_outputs.get("read_qc", {})
    if read_qc_out:
        if read_qc_out.get("clean_long_reads") is not None:
            available_inputs.add("clean_long_reads")
        if read_qc_out.get("clean_short_dna_reads") is not None:
            available_inputs.add("clean_short_dna_reads")

    planned_signals: list[str] = ["quast", "busco"]

    review_plan = AssemblyReviewPlan.model_validate(
        {
            **ar_out.get("review_plan", {}),
            "planned_signals": planned_signals,
            "status": "evaluating",
        }
    )

    eval_signals: list[AssemblyEvalSignal] = []
    candidate_ids = [
        candidate.get("candidate_id", "unknown")
        for candidate in ar_out.get("candidates", [])
    ]
    for signal_name in planned_signals:
        method_name = _review_signal_method_name(signal_name)
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

    ar_out["review_plan"] = ASSEMBLY_REVIEW_PLAN_CURSOR_ADAPTER.validate_python(
        review_plan.model_dump(mode="json")
    )
    ar_out["planned_signals"] = planned_signals
    ar_out["signals"] = [
        ASSEMBLY_EVAL_SIGNAL_CURSOR_ADAPTER.validate_python(
            signal.model_dump(mode="json")
        )
        for signal in eval_signals
    ]
    ar_out["eval_bundle_ref"] = eval_bundle_ref

    return update_assembly_review_output(state, ar_out)


def evaluate_review_rules(state: TopLevelState) -> StateUpdate:
    """Evaluate structural rules and record candidate count for routing."""
    ar_out = assembly_review_output(state)

    if ar_out.get("status") == "failed":
        return {}

    candidates = ar_out.get("candidates", [])
    ar_out["candidate_count"] = len(candidates)

    return update_assembly_review_output(state, ar_out)


def parameter_quality_gate(state: TopLevelState) -> StateUpdate:
    """Route Phase10 assembly-review failures back to assembly parameter replan."""

    stage_outputs = dict(state.get("stage_outputs") or {})
    assembly_out = dict(stage_outputs.get("assembly") or {})
    if not assembly_out.get("parameter_attempts"):
        return {}

    ar_out = assembly_review_output(state)
    metrics = _phase10_review_metrics(ar_out, assembly_out)
    gate_state = state.copy()
    if metrics and not ar_out.get("metrics"):
        gate_state.update(update_assembly_review_output(state, {"metrics": metrics}))

    from app.stages.assembly.parameters.workflow import (
        parameter_quality_gate as assembly_parameter_quality_gate,
    )

    gate_update = assembly_parameter_quality_gate(gate_state)
    updated_outputs = dict(gate_update.get("stage_outputs") or stage_outputs)
    updated_assembly = dict(updated_outputs.get("assembly") or {})
    quality_gate = updated_assembly.get("quality_gate")
    updated_state = state.copy()
    updated_state["stage_outputs"] = validate_stage_outputs(updated_outputs)

    if updated_assembly.get("status") == "retry_parameters_after_review_failure":
        review_update = update_assembly_review_output(
            updated_state,
            {
                "status": "parameter_replan_requested",
                "parameter_quality_gate": quality_gate,
            },
        )
        return {
            "current_stage": "assembly",
            "supervision_state": "running_stage",
            "active_interrupt": None,
            **review_update,
        }

    if updated_assembly.get("status") == "review_parameter_retries_exhausted":
        return update_assembly_review_output(
            updated_state,
            {
                "parameter_quality_gate": quality_gate,
                "parameter_retries_exhausted": True,
                "parameter_quality_checked": True,
            },
        )

    return update_assembly_review_output(
        updated_state,
        {
            "parameter_quality_gate": quality_gate,
            "parameter_quality_checked": True,
        },
    )


def build_related_species_evidence_pack(state: TopLevelState) -> StateUpdate:
    """Build non-blocking local NCBI evidence for the review report and LLM."""
    ar_out = assembly_review_output(state)
    if is_terminal_failure(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out)
    if reference_dir:
        evidence_pack = _build_related_species_evidence_pack(
            species_name, reference_dir=reference_dir
        )
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
    return update_assembly_review_output(
        state,
        {
            "related_species_evidence_pack": evidence_pack,
            "related_species_evidence_pack_ref": evidence_ref,
        },
    )


def _related_species_reference_dir(
    ar_out: AssemblyReviewCursor,
) -> str | Path | None:
    """Return validated NCBI reference directory path from stage output or environment.

    Returns None if no valid path is configured, triggering default reference directory.
    """
    value = ar_out.get("related_species_reference_dir") or os.environ.get(
        "GENE_AGENT_NCBI_METAZOA_REFERENCE_DIR"
    )
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


def build_related_species_statistics_and_notebook(
    state: TopLevelState,
) -> StateUpdate:
    """Build statistics pack and comparative notebook for assembly review."""
    ar_out = assembly_review_output(state)
    if is_terminal_failure(ar_out.get("status")):
        return {}

    species_name = _target_species_name(state)
    reference_dir = _related_species_reference_dir(ar_out)

    target_metrics = _extract_target_metrics_from_signals(ar_out)

    if reference_dir:
        statistics_pack = _build_related_species_statistics_pack(
            species_name, reference_dir=reference_dir, target_metrics=target_metrics
        )
    else:
        statistics_pack = _build_related_species_statistics_pack(
            species_name, target_metrics=target_metrics
        )

    existing_stats_ref = ar_out.get("related_species_statistics_pack_ref")
    statistics_pack_unchanged = existing_stats_ref and _json_artifact_payload_matches(
        existing_stats_ref, statistics_pack
    )

    if statistics_pack_unchanged:
        existing_notebook_ref = ar_out.get("assembly_review_comparative_notebook_ref")
        if existing_notebook_ref and Path(existing_notebook_ref).exists():
            comparative_figures = ar_out.get("comparative_figures", {})
            all_figures_exist = all(
                Path(p).exists() for p in comparative_figures.values()
            )
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
                register_artifact_records(data_dir, session_id, [notebook_artifact])

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
                    register_artifact_records(data_dir, session_id, figure_artifacts)

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
    work_figures_dir = stage_work_dir(state, "assembly_review", "figures")
    figure_sources = render_statistics_figures(
        statistics_pack,
        work_figures_dir,
        target_metrics=target_metrics,
    )

    figure_paths: dict[str, str] = {}
    for logical_name, abs_path in figure_sources.items():
        source = Path(abs_path)
        artifact = ArtifactMeta.from_file(
            source,
            stage="assembly_review",
            method_name="assembly_review.contract",
            artifact_type=f"assembly_review.figure.{logical_name}",
            metadata={"figure_type": logical_name},
        )
        published = publish_artifact_record(
            data_dir,
            session_id,
            artifact,
            destination=figures_dir / source.name,
        )
        figure_paths[logical_name] = str(published.path)

    out_dir = stage_artifacts_dir(state, "assembly_review")
    statistics_pack_relpath = "related_species_statistics_pack.json"
    figures_relpaths = {
        name: f"figures/{Path(path).name}" for name, path in figure_paths.items()
    }
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
    notebook_source = stage_work_dir(
        state, "assembly_review", "assembly_review_report.ipynb"
    )
    write_notebook(notebook, notebook_source)

    notebook_artifact = ArtifactMeta.from_file(
        notebook_source,
        stage="assembly_review",
        method_name="assembly_review.contract",
        artifact_type=ASSEMBLY_REVIEW_COMPARATIVE_NOTEBOOK,
        metadata={"session_id": session_id, "figure_count": len(figure_paths)},
    )
    notebook_path = publish_artifact_record(
        data_dir,
        session_id,
        notebook_artifact,
        destination=notebook_path,
    ).path

    return update_assembly_review_output(
        state,
        {
            "related_species_statistics_pack": statistics_pack,
            "related_species_statistics_pack_ref": statistics_ref,
            "assembly_review_comparative_notebook_ref": str(notebook_path),
            "comparative_figures": figure_paths,
        },
    )


def _extract_target_metrics_from_signals(
    ar_out: AssemblyReviewCursor,
) -> dict[str, float] | None:
    """Extract target assembly metrics from available signals for percentile"""
    """computation.

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


def _phase10_review_metrics(
    ar_out: AssemblyReviewCursor, assembly_out: dict[str, Any]
) -> dict[str, Any]:
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
                metrics["assembly_size_ratio"] = float(quast["total_length"]) / float(
                    genome_size
                )
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


def human_review_gate(state: TopLevelState) -> StateUpdate:
    """Set an active interrupt for unresolved assembly review decisions."""
    ar_out = assembly_review_output(state)
    ar_out, report_refs, comparison_context = _ensure_assembly_review_reports(
        state, ar_out
    )
    candidates = ar_out.get("candidates", [])
    signals = ar_out.get("signals", [])
    candidate_payloads: list[dict[str, Any]] = [
        dict(candidate) for candidate in candidates
    ]

    payload = ReviewPayload(
        stage="assembly_review",
        gate_id="assembly_review",
        reason="assembly_review_human_decision_required",
        candidates=candidate_payloads,
        signals=[ReviewSignal.model_validate(s) for s in signals],
        decisions_allowed=[
            "accept",
            "stop_human_review",
        ],
        notes=[
            "Thresholds for BUSCO and QUAST require human approval before application.",
            (
                "Accept continues with the selected assembly to Hi-C scaffolding, "
                "then T2T, then annotation stages."
            ),
            (
                "Assembly parameter retries use the v3 recommendation, approval, "
                "and frozen-attempt workflow before this gate."
            ),
            "Stop leaves downstream stages unstarted.",
        ],
        auto_decidable=_assembly_review_auto_decidable(ar_out),
        report_refs=report_refs,
        candidate_comparison_context=comparison_context,
        related_species_evidence_pack=dict(
            ar_out.get("related_species_evidence_pack") or {}
        ),
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
        candidates=candidate_payloads,
        review_request_artifact_ref=request_ref.model_dump(mode="json"),
        auto_decidable=payload.auto_decidable,
        policy_mode=outcome.mode,
        pending_human_confirmation=pending_confirmation,
    )

    review_update = update_assembly_review_output(
        state,
        {
            **ar_out,
            "status": "blocked_human",
            "review_request_artifact_ref": request_ref.model_dump(mode="json"),
            "review_policy_outcome": outcome.model_dump(mode="json"),
            "auto_decidable": payload.auto_decidable,
            "pending_human_confirmation": pending_confirmation,
        },
    )
    blocked_update: StateUpdate = {
        "active_interrupt": cast(
            ActiveInterrupt,
            persisted_request.compact_interrupt(
                stage="assembly_review",
                gate_id="assembly_review",
                reason="assembly_review_human_decision_required",
            ),
        ),
        "supervision_state": "blocked_human",
        **review_update,
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

        blocked_state = state.copy()
        blocked_state.update(blocked_update)
        auto_update = resume_review_state(blocked_state, recommendation)
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


def commit_assembly_review(state: TopLevelState) -> StateUpdate:
    """Commit the assembly review decision and advance routing."""
    from app.workflow import next_stage_name, skipped_stages_until_next

    ar_out = assembly_review_output(state)

    decision = ar_out.get("decision", "stop_human_review")
    selected_candidate_id = ar_out.get("selected_candidate_id")
    rejected_candidates = ar_out.get("rejected_candidates", [])
    final_assembly_ref = ar_out.get("final_assembly_ref")
    action_type = ar_out.get("action_type", "pass")
    action_execution_mode = ar_out.get("action_execution_mode", "none")
    method_name = ar_out.get("action_method_name")
    evidence_refs = list(ar_out.get("evidence_refs") or [])
    evidence_notes = list(ar_out.get("evidence_notes") or [])

    selected_candidate = None
    selected_verified = None
    current_verified_candidate = None
    current_primary_output_ref = None
    if selected_candidate_id:
        selected_candidate = next(
            (
                candidate
                for candidate in ar_out.get("candidates") or []
                if candidate.get("candidate_id") == selected_candidate_id
            ),
            None,
        )
        if selected_candidate is None:
            raise ResumeValidationError(
                "assembly_review selected candidate is absent from its cursor"
            )
        selected_verified = _verified_review_candidate(state, selected_candidate)
        current_verified_candidate = selected_verified
        selected_path = str(selected_verified.primary_output_path)
        checkpoint_pre_action = ar_out.get("pre_action_assembly_ref")
        if checkpoint_pre_action and checkpoint_pre_action != selected_path:
            raise ResumeValidationError(
                "assembly_review pre-action path does not match selected candidate"
            )
        if not final_assembly_ref:
            raise ResumeValidationError(
                "assembly_review selected decision has no final assembly path"
            )
        if Path(final_assembly_ref).resolve(strict=False) == (
            selected_verified.primary_output_path.resolve(strict=False)
        ):
            output_assembly_ref = selected_path
            current_primary_output_ref = selected_verified.primary_output_ref
        else:
            published_action = _publish_review_action_output(
                state,
                selected_verified,
                final_assembly_ref,
                action_type=action_type,
                review_request_id=str(ar_out.get("review_request_id") or ""),
            )
            current_verified_candidate = publish_derived_assembly_candidate(
                absolute_data_dir(state),
                state["session_id"],
                source_candidate=selected_verified,
                primary_output_ref=published_action.ref,
                stage="assembly_review",
                method_name=f"assembly_review.{action_type}",
                action_identity=str(ar_out.get("review_request_id") or ""),
            )
            output_assembly_ref = str(current_verified_candidate.primary_output_path)
            final_assembly_ref = output_assembly_ref
            current_primary_output_ref = (
                current_verified_candidate.primary_output_ref
            )
        pre_action_ref = selected_path
    else:
        output_assembly_ref = final_assembly_ref
        pre_action_ref = ar_out.get("pre_action_assembly_ref") or final_assembly_ref
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
        if skipped_stage in DOMAIN_STAGES:
            skip_reasons.setdefault(cast(DomainStage, skipped_stage), "not_requested")
    decided_by = ar_out.get("decided_by", "human")
    rationale_artifact_ref = ar_out.get("rationale_artifact_ref")
    action_metadata = {
        "action_type": action_type,
        "action_execution_mode": action_execution_mode,
        "pre_action_assembly_ref": pre_action_ref,
        "post_action_assembly_ref": final_assembly_ref,
        "output_assembly_ref": output_assembly_ref,
        "method_name": method_name,
        "evidence_refs": evidence_refs,
        "evidence_notes": evidence_notes,
        "decided_by": decided_by,
        "rationale_artifact_ref": rationale_artifact_ref,
        "primary_output_ref": (
            current_primary_output_ref.model_dump(mode="json")
            if current_primary_output_ref is not None
            else None
        ),
    }

    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    rejected_candidate_ids = (
        rejected_candidates
        if isinstance(rejected_candidates, list)
        else list(rejected_candidates)
    )
    decision_artifact = ReviewDecisionArtifact(
        session_id=state["session_id"],
        stage="assembly_review",
        decision=decision,
        candidate_id=selected_candidate_id,
        rejected_candidates=rejected_candidate_ids,
        final_assembly_ref=final_assembly_ref,
        notes=ar_out.get("notes", []),
        metadata=action_metadata,
    )
    review_request_id = ar_out.get("review_request_id")
    review_request_hash = ar_out.get("review_request_hash")
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError(
            "assembly_review decision is missing review request binding"
        )
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
        rejected_candidates=rejected_candidate_ids,
        notes=ar_out.get("notes", []),
        artifact_ref=decision_ref.model_dump(mode="json"),
        metadata=action_metadata,
        decided_by=decided_by,
        rationale_artifact_ref=rationale_artifact_ref,
        review_request_id=review_request_id,
    )
    action_ref = None
    if (
        selected_candidate_id
        and pre_action_ref
        and final_assembly_ref
        and output_assembly_ref
        and decision in {"accept", "pass", "purge", "polish", "continue_low_confidence"}
    ):
        action_artifact = AssemblyReviewActionArtifact(
            session_id=state["session_id"],
            decision=decision,
            action_type=action_type,
            candidate_id=selected_candidate_id,
            pre_action_assembly_ref=pre_action_ref,
            post_action_assembly_ref=final_assembly_ref,
            output_assembly_ref=output_assembly_ref,
            action_execution_mode=action_execution_mode,
            method_name=method_name,
            evidence_refs=evidence_refs,
            evidence_notes=evidence_notes,
            notes=ar_out.get("notes", []),
            metadata={
                "review_decision_artifact_ref": decision_ref.model_dump(mode="json")
            },
        )
        action_ref = _write_json_artifact(
            state,
            "assembly_review_action.json",
            action_artifact.model_dump(mode="json"),
            artifact_type=ASSEMBLY_REVIEW_ACTION,
        )

    update: StateUpdate = {
        "current_stage": next_stage,
        "supervision_state": "completed" if next_stage == "done" else "running_stage",
        "route_trace": trace,
        "skip_reasons": skip_reasons,
    }

    if selected_candidate_id:
        committed_out: dict[str, object] = {
            **ar_out,
            "status": "completed",
            "decision": decision,
            "selected_candidate_id": selected_candidate_id,
            "rejected_candidates": rejected_candidates,
            "final_assembly_ref": final_assembly_ref,
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
        review_update = update_assembly_review_output(state, committed_out)
        update["stage_outputs"] = review_update["stage_outputs"]
        if output_assembly_ref:
            if (
                selected_candidate is None
                or selected_verified is None
                or current_verified_candidate is None
                or current_primary_output_ref is None
            ):
                raise ResumeValidationError(
                    "assembly_review output is missing typed candidate provenance"
                )
            update["resource_refs"] = update_resource_ref(
                state,
                "assembly",
                [str(output_assembly_ref)],
                "assembly_review",
                {
                    "selected_candidate_id": selected_candidate_id,
                    "decision": decision,
                    "action_type": action_type,
                    "candidate_bundle_ref": current_verified_candidate.ref.model_dump(
                        mode="json"
                    ),
                    "primary_output_ref": current_primary_output_ref.model_dump(
                        mode="json"
                    ),
                },
            )

    return update


def apply_assembly_review_resume_decision(
    state: TopLevelState, request: ResumeReviewRequest
) -> StateUpdate:
    """Apply a validated assembly review decision to a blocked state."""

    if request.stage != "assembly_review":
        raise ResumeValidationError(
            "assembly review resume requires stage assembly_review"
        )
    ar_out = assembly_review_output(state)
    active_interrupt = cast(ActiveInterrupt, state.get("active_interrupt") or {})
    review_request_id = request.review_request_id or active_interrupt.get(
        "review_request_id"
    )
    review_request_hash = request.review_request_hash or active_interrupt.get(
        "request_hash"
    )
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError(
            "assembly_review decision is missing review request binding"
        )
    candidates = ar_out.get("candidates") or []
    candidates_by_id = {
        candidate.get("candidate_id"): candidate for candidate in candidates
    }
    allowed = {
        "accept",
        "pass",
        "purge",
        "polish",
        "continue_low_confidence",
        "stop_human_review",
    }
    if request.decision not in allowed:
        raise ResumeValidationError(
            f"unsupported assembly_review decision: {request.decision}"
        )

    if request.decision == "stop_human_review":
        return _stop_assembly_review(state, request)

    if not request.candidate_id:
        raise ResumeValidationError("assembly_review resume requires candidate_id")
    if request.candidate_id not in candidates_by_id:
        raise ResumeValidationError(
            f"unknown assembly_review candidate_id: {request.candidate_id}"
        )

    candidate_final_ref = _candidate_primary_output_path(
        state,
        candidates_by_id[request.candidate_id],
    )
    action_payload = _assembly_review_action_payload(request, candidate_final_ref)

    review_payload: dict[str, object] = {
        **ar_out,
        "decision": request.decision,
        "selected_candidate_id": request.candidate_id,
        "rejected_candidates": request.rejected_candidates,
        "final_assembly_ref": action_payload["final_assembly_ref"],
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
        review_payload["purged_assembly"] = action_payload["final_assembly_ref"]
    if action_payload["action_type"] == "polish":
        review_payload["polished_assembly"] = action_payload["final_assembly_ref"]
    review_update = update_assembly_review_output(state, review_payload)
    updated_state = state.copy()
    updated_state.update(
        {
            "stage_outputs": review_update["stage_outputs"],
            "active_interrupt": None,
            "supervision_state": "running_stage",
        }
    )
    commit_update = commit_assembly_review(updated_state)
    return {**commit_update, "active_interrupt": None}


def _assembly_review_action_payload(
    request: ResumeReviewRequest, candidate_final_ref: str | None
) -> dict[str, Any]:
    if request.decision in {"accept", "pass", "continue_low_confidence"}:
        if not candidate_final_ref:
            raise ResumeValidationError(
                "assembly_review selected candidate has no verified primary output"
            )
        action_type = (
            "pass_low_confidence"
            if request.decision == "continue_low_confidence"
            else "pass"
        )
        return {
            "final_assembly_ref": candidate_final_ref,
            "pre_action_assembly_ref": candidate_final_ref,
            "action_type": action_type,
            "action_execution_mode": "none",
            "evidence_refs": list(request.metadata.get("evidence_refs") or []),
            "evidence_notes": list(request.metadata.get("evidence_notes") or []),
        }

    if not candidate_final_ref:
        raise ResumeValidationError(
            "assembly_review transform action requires selected candidate "
            "primary output"
        )
    metadata_output_ref = request.metadata.get("action_output_ref")
    action_output_ref = metadata_output_ref
    if not action_output_ref:
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires action_output_ref"
        )
    if action_output_ref == candidate_final_ref:
        raise ResumeValidationError(
            f"assembly_review {request.decision} output must differ from "
            "pre-action assembly"
        )
    if not Path(str(action_output_ref)).exists():
        raise ResumeValidationError(
            f"assembly_review {request.decision} action output ref must exist"
        )
    # Trust boundary: human_approved_action is user-asserted, not system-verified.
    # This documents that a human performed and approved an external action.
    # Additional validation (output exists, differs from input, has evidence)
    # reduces risk.
    if not (
        request.metadata.get("human_approved_action") is True
        or request.metadata.get("approval") == "human"
    ):
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires human-approved action "
            "metadata"
        )
    evidence_refs = list(request.metadata.get("evidence_refs") or [])
    evidence_notes = list(request.metadata.get("evidence_notes") or [])
    if not evidence_refs and not evidence_notes:
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires action evidence metadata"
        )
    method_name = request.method_name or request.metadata.get("method_name")
    if (
        not method_name
        and not request.metadata.get("provenance_ref")
        and not request.metadata.get("provenance")
    ):
        raise ResumeValidationError(
            f"assembly_review {request.decision} requires method or provenance metadata"
        )
    if request.decision == "polish" and method_name in {
        "nextpolish_shortread",
        "pilon_shortread",
    }:
        if not request.metadata.get(
            "short_read_evidence_refs"
        ) and not request.metadata.get("short_read_evidence_notes"):
            raise ResumeValidationError(
                "short-read polishing requires explicit short-read evidence"
            )
    return {
        "final_assembly_ref": str(action_output_ref),
        "pre_action_assembly_ref": candidate_final_ref,
        "action_type": request.decision,
        "action_execution_mode": "external",
        "action_method_name": method_name,
        "evidence_refs": evidence_refs,
        "evidence_notes": evidence_notes,
    }


def _stop_assembly_review(
    state: TopLevelState, request: ResumeReviewRequest
) -> StateUpdate:
    ar_out = assembly_review_output(state)
    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    decision_artifact = ReviewDecisionArtifact(
        session_id=state["session_id"],
        stage="assembly_review",
        decision="stop_human_review",
        candidate_id=request.candidate_id,
        rejected_candidates=request.rejected_candidates,
        final_assembly_ref=None,
        notes=request.notes,
        metadata=request.metadata,
    )
    active_interrupt = cast(ActiveInterrupt, state.get("active_interrupt") or {})
    review_request_id = request.review_request_id or active_interrupt.get(
        "review_request_id"
    )
    review_request_hash = request.review_request_hash or active_interrupt.get(
        "request_hash"
    )
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError(
            "assembly_review stop decision is missing review request binding"
        )
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
    review_update = update_assembly_review_output(
        state,
        {
            **ar_out,
            "status": "failed_terminal",
            "decision": "stop_human_review",
            "review_decision_artifact_ref": decision_ref.model_dump(mode="json"),
            "notes": request.notes,
            "decided_by": request.decided_by,
            "rationale_artifact_ref": request.rationale_artifact_ref,
        },
    )
    return {
        "current_stage": "assembly_review",
        "supervision_state": "failed_terminal",
        "active_interrupt": None,
        **review_update,
        "last_error": "assembly_review stopped by human review decision",
    }


def generate_candidate_comparison(state: TopLevelState) -> StateUpdate:
    """Generate a candidate comparison artifact when multiple candidates exist."""
    ar_out = assembly_review_output(state)
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
    if existing_ref and _json_artifact_payload_matches(
        existing_ref, comparison_payload
    ):
        return {}

    comparison_ref = _write_json_artifact(
        state,
        "candidate_comparison.json",
        comparison_payload,
        artifact_type=ASSEMBLY_CANDIDATE_COMPARISON,
    )

    ledger = Ledger(state.get("data_dir", "data"))
    ledger.record_event(
        state["session_id"],
        "candidate_comparison_generated",
        {
            "candidate_ids": [c["candidate_id"] for c in comparison_candidates],
            "candidate_count": len(comparison_candidates),
            "comparison_artifact_ref": comparison_ref,
        },
    )

    return update_assembly_review_output(
        state, {"candidate_comparison_ref": comparison_ref}
    )


def _json_artifact_payload_matches(artifact_ref: Any, payload: dict[str, Any]) -> bool:
    path = Path(str(artifact_ref))
    if not path.exists():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(existing == payload)


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
        raw = (
            json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        )
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    metadata = dict(raw.get("user_metadata") or {})
    for key in (
        "target_species",
        "species_name",
        "scientific_name",
        "organism",
        "species",
        "chinese_name",
    ):
        if key in raw:
            metadata.setdefault(key, raw[key])
    return species_name_from_metadata(metadata)


def _ensure_assembly_review_reports(
    state: TopLevelState,
    ar_out: AssemblyReviewCursor,
) -> tuple[AssemblyReviewCursor, dict[str, str], dict[str, Any]]:
    candidates = ar_out.get("candidates", [])
    report_kind = (
        "candidate_comparison_review"
        if len(candidates) >= 2
        else "single_candidate_review"
    )
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

    updated = ASSEMBLY_REVIEW_CURSOR_ADAPTER.validate_python(
        {
            **ar_out,
            "assembly_review_report_ref": report_ref,
            "assembly_review_packet_ref": data_ref,
        }
    )
    report_refs = {
        "assembly_review_report": report_ref,
        "review_packet": data_ref,
    }
    if ar_out.get("candidate_comparison_ref"):
        report_refs["candidate_comparison"] = str(ar_out["candidate_comparison_ref"])
    if ar_out.get("related_species_evidence_pack_ref"):
        report_refs["related_species_evidence_pack"] = str(
            ar_out["related_species_evidence_pack_ref"]
        )

    comparison_context = _candidate_comparison_context(state, updated)
    return updated, report_refs, comparison_context


def _assembly_review_report_payload(
    state: TopLevelState,
    ar_out: AssemblyReviewCursor,
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
            _candidate_report_row(state, candidate, signals, assembly_out)
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
            "related_species_evidence_pack_ref": ar_out.get(
                "related_species_evidence_pack_ref"
            ),
        },
        "related_species_sanity_check": dict(
            ar_out.get("related_species_evidence_pack") or {}
        ),
        "notes": [
            "thresholds_not_applied_without_human_approval",
            "report_informs_human_decision_only",
        ],
    }


def _candidate_report_row(
    state: TopLevelState,
    candidate: AssemblyCandidateCursor,
    signals: list[AssemblyEvalSignalCursor],
    assembly_out: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = candidate.get("candidate_id", "unknown")
    candidate_signals = [
        signal for signal in signals if signal.get("candidate_id") == candidate_id
    ]
    return {
        "candidate_id": candidate_id,
        "method_name": candidate.get("method_name"),
        "source": candidate.get("source"),
        "status": candidate.get("status", "unknown"),
        "final_assembly_ref": _candidate_primary_output_path(state, candidate),
        "parameters": _candidate_parameters(candidate, assembly_out),
        "metrics": _candidate_metrics(candidate_id, signals),
        "signals": _signal_status_buckets(candidate_signals),
        "metric_refs_by_signal": {
            str(signal.get("signal_name")): [
                str(ref) for ref in signal.get("metric_refs", [])
            ]
            for signal in candidate_signals
            if signal.get("metric_refs")
        },
        "signal_notes_by_signal": {
            str(signal.get("signal_name")): list(signal.get("notes") or [])
            for signal in candidate_signals
            if signal.get("notes")
        },
    }


def _signal_status_buckets(
    signals: list[AssemblyEvalSignalCursor],
) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "available": [],
        "missing": [],
        "failed": [],
        "pending": [],
    }
    for signal in signals:
        status = signal.get("status", "pending")
        signal_name = signal.get("signal_name")
        if status in buckets and signal_name:
            buckets[status].append(str(signal_name))
    return {key: sorted(values) for key, values in buckets.items()}


def _candidate_comparison_context(
    state: TopLevelState,
    ar_out: AssemblyReviewCursor,
) -> dict[str, Any]:
    candidates = ar_out.get("candidates", [])
    context: dict[str, Any] = {
        "candidate_ids": [
            candidate.get("candidate_id")
            for candidate in candidates
            if candidate.get("candidate_id")
        ],
        "candidate_primary_output_refs": {
            str(candidate.get("candidate_id")): dict(
                candidate["primary_output_ref"]
            )
            for candidate in candidates
            if candidate.get("candidate_id")
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
        (
            "No automatic biological thresholds are applied. This report informs "
            "the human decision only."
        ),
        "",
        "## Candidate Metrics",
        "",
        (
            "| Candidate | Method | Source | Assembly ref | QUAST contigs | "
            "Total length | N50 | Largest contig | BUSCO C% | BUSCO S% | "
            "BUSCO D% | BUSCO F% | BUSCO M% | Available signals | "
            "Missing/failed/pending signals |"
        ),
        (
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
            "--- | --- | --- | --- | --- |"
        ),
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

    lines.extend(
        [
            "",
            "## Parameter Provenance",
            "",
            (
                "| Candidate | Read profile | Estimated genome size | Threads | "
                "Rerun index |"
            ),
            "| --- | --- | --- | --- | --- |",
        ]
    )
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

    _append_related_species_sanity_check(
        lines, payload.get("related_species_sanity_check") or {}
    )

    lines.extend(["", "## Evidence Refs", ""])
    raw_refs = payload.get("raw_refs") or {}
    for key, value in raw_refs.items():
        if value:
            lines.append(f"- {key}: `{value}`")
    for candidate in payload.get("candidates", []):
        metric_refs = candidate.get("metric_refs_by_signal") or {}
        for signal_name, refs in metric_refs.items():
            for ref in refs:
                lines.append(
                    f"- {candidate.get('candidate_id')} {signal_name}: `{ref}`"
                )

    lines.extend(["", "## Missing Evidence", ""])
    for candidate in payload.get("candidates", []):
        signal_buckets = candidate.get("signals") or {}
        notes_by_signal = candidate.get("signal_notes_by_signal") or {}
        for status in ("missing", "failed", "pending"):
            for signal_name in signal_buckets.get(status, []):
                notes = ", ".join(notes_by_signal.get(signal_name, []))
                suffix = f" — {notes}" if notes else ""
                lines.append(
                    f"- {candidate.get('candidate_id')} {signal_name}: {status}{suffix}"
                )

    lines.extend(["", "## Report Notes", ""])
    for note in payload.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _append_related_species_sanity_check(
    lines: list[str], pack: dict[str, Any]
) -> None:
    lines.extend(["", "## Related-species sanity check", ""])
    if not pack:
        lines.append("- Reference evidence was not generated.")
        return
    target = pack.get("target") or {}
    target_name = target.get("canonical_scientific_name") or target.get("input_name")
    lines.extend(
        [
            f"- Target: {_markdown_cell(target_name)}",
            f"- Matching mode: {_markdown_cell(pack.get('matching_mode'))}",
            (
                "- This evidence is comparative only; it is not an assembly prior "
                "or automatic-failure threshold."
            ),
            "",
        ]
    )
    records = pack.get("selected_records") or []
    if records:
        lines.extend(
            [
                (
                    "| Organism | Accession | Assembly | Total length | "
                    "Chromosomes | Contig N50 | Scaffold N50 | GC | "
                    "BUSCO lineage |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for record in records:
            busco = record.get("busco") or {}
            lines.append(
                "| "
                + " | ".join(
                    _markdown_cell(value)
                    for value in [
                        record.get("organism_name"),
                        record.get("accession"),
                        record.get("assembly_name"),
                        record.get("total_sequence_length_bp"),
                        record.get("chromosome_count"),
                        record.get("contig_n50_bp"),
                        record.get("scaffold_n50_bp"),
                        record.get("gc_percent"),
                        busco.get("lineage"),
                    ]
                )
                + " |"
            )
    else:
        lines.append("- No useful related-species primary assemblies were found.")
    excluded = pack.get("excluded_alternate_haplotype_records") or []
    if excluded:
        excluded_accessions = ", ".join(
            str(record.get("accession")) for record in excluded
        )
        lines.append(f"- Excluded alternate-haplotype records: {excluded_accessions}")
    summary = pack.get("computed_summary") or {}
    if summary:
        lines.extend(
            [
                "",
                "Computed summary:",
                "",
                "```json",
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                "```",
            ]
        )
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


def _candidate_parameters(
    candidate: AssemblyCandidateCursor, assembly_out: dict[str, Any]
) -> dict[str, Any]:
    read_type_prior = assembly_out.get("read_type_prior") or {}
    execution_contract = assembly_out.get(ASSEMBLY_EXECUTION_CONTRACT_KEY)
    threads = None
    if execution_contract is not None:
        validated_contract = AssemblyExecutionContract.model_validate(
            execution_contract
        )
        if validated_contract.threads is not None:
            threads = validated_contract.threads.value
    method_key = str(candidate.get("method_name") or "").rsplit(".", 1)[-1]
    return {
        "read_profile": (
            read_type_prior.get(method_key)
            if isinstance(read_type_prior, dict)
            else None
        ),
        "estimated_genome_size": assembly_out.get("genome_size_prior"),
        "threads": threads,
    }


def _candidate_metrics(
    candidate_id: str, signals: list[AssemblyEvalSignalCursor]
) -> dict[str, Any]:
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


def fail_assembly_review(state: TopLevelState) -> StateUpdate:
    """Record assembly review failure and stop."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = assembly_review_output(state)
    assembly_out = stage_outputs.get("assembly", {})
    status = str(ar_out.get("status") or assembly_out.get("status") or "failed")
    default_reason = {
        "blocked_env": "assembly_review blocked on missing conda environment",
        "missing_tool": "assembly_review blocked on missing tool",
    }.get(status, "assembly_review failed")
    reason = ar_out.get("reason") or assembly_out.get("reason") or default_reason
    return {
        "current_stage": "assembly_review",
        "supervision_state": "failed_terminal",
        "last_error": str(reason),
    }


def _publish_review_action_output(
    state: TopLevelState,
    selected: VerifiedExecutedCandidate,
    source: str,
    *,
    action_type: str,
    review_request_id: str,
) -> PublishedArtifact:
    """Publish one externally approved transformed assembly before handoff."""

    source_path = Path(source).expanduser().resolve(strict=False)
    if not source_path.is_file():
        raise ResumeValidationError(
            "assembly_review action output is not a materialized file"
        )
    if source_path.stat().st_size <= 0:
        raise ResumeValidationError("assembly_review action output is empty")
    safe_request_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in review_request_id
    )
    if not safe_request_id:
        raise ResumeValidationError(
            "assembly_review action output has no review request identity"
        )
    filename = f"{safe_request_id}.{action_type}.fasta"
    destination = stage_artifacts_dir(
        state,
        "assembly_review",
        "actions",
    ) / filename
    temporary = stage_work_dir(
        state,
        "assembly_review",
        "actions",
        f".{filename}.{uuid4().hex}.tmp",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, temporary)
    artifact = ArtifactMeta.from_file(
        temporary,
        stage="assembly_review",
        method_name=f"assembly_review.{action_type}",
        artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
        metadata={
            "candidate_id": selected.bundle.candidate_id,
            "candidate_bundle_ref": selected.ref.model_dump(mode="json"),
            "dependency_refs": [
                selected.primary_output_ref.model_dump(mode="json")
            ],
            "review_request_id": review_request_id,
        },
    )
    try:
        return publish_artifact_record(
            absolute_data_dir(state),
            state["session_id"],
            artifact,
            destination=destination,
        )
    finally:
        temporary.unlink(missing_ok=True)


def _candidate_primary_output_path(
    state: TopLevelState,
    candidate: AssemblyCandidateCursor,
) -> str:
    return str(_verified_review_candidate(state, candidate).primary_output_path)


_EVAL_SIGNAL_NAMES = {"quast", "busco"}


def _assembly_review_auto_decidable(ar_out: AssemblyReviewCursor) -> bool:
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


def _available_review_inputs(
    state: TopLevelState,
) -> tuple[bool, bool, set[str], set[str]]:
    available_inputs: set[str] = set()
    missing_inputs: set[str] = set()
    if state.get("asset_registry_ref"):
        from app.storage.asset_registry import load_asset_records

        asset_types = {asset.asset_type for asset in load_asset_records(state)}
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
