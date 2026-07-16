"""Gene annotation stage nodes for planning structural annotation contracts."""

from __future__ import annotations

import json
import re
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.execution.artifacts import (
    append_artifact_records,
    publish_or_recover_work_artifact,
)
from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.graph.routing import next_stage_name
from app.review.annotation_intake import (
    DOWNSTREAM_STAGES,
    load_annotation_intake_plan,
    rewrite_requested_stages_after_assembly_review,
)
from app.review.base import ResumeReviewRequest, ReviewPayload, ReviewSignal
from app.review.persistence import (
    PersistedReviewRequest,
    persist_review_decision,
    persist_review_request,
)
from app.review.policy import evaluate
from app.schemas.artifacts import (
    ArtifactMeta,
    CandidateBundle,
    GeneAnnotationBundle,
    GeneAnnotationPlan,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.base import (
    absolute_data_dir,
    append_route_trace,
    stage_artifacts_dir,
    stage_output,
    stage_work_dir,
    update_stage_output,
)
from app.stages.gene_annotation.persistence import (
    load_gene_attempt,
    load_gene_execution,
    persist_gene_attempt,
    persist_gene_execution,
)

GENE_CANDIDATE_METHODS = [
    "gene_annotation.candidate.augustus",
    "gene_annotation.candidate.miniprot",
]
RNA_HELPER_METHODS: list[str] = []
LONG_RNA_HELPER_METHODS: list[str] = []
PROTEIN_HELPER_METHODS: list[str] = []
GENE_CONTRACT_METHOD = "gene_annotation.contract"


def _gene_output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "gene_annotation")


def _update_gene_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "gene_annotation", payload)


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_gene_output(state, {"status": "failed", "reason": reason})


def enter_gene_annotation(state: TopLevelState) -> dict:
    """Validate the annotation intake plan and mark gene annotation running."""
    try:
        load_annotation_intake_plan(state)
    except ValueError as exc:
        return _fail_update(state, str(exc))
    return {"supervision_state": "running_stage"}


def resolve_gene_annotation_inputs(state: TopLevelState) -> dict:
    """Resolve masked genome and optional annotation evidence."""
    if _gene_output(state).get("status", ""):
        return {}
    genome_ref = _softmasked_genome_ref(state)
    genome_source = "softmasked_genome"
    fallback_reason = None
    if not genome_ref:
        fallback_reason = _explicit_unmasked_fallback_reason(state)
        if not fallback_reason:
            return _fail_update(state, "missing softmasked_genome for gene_annotation")
        # Fallback to unmasked assembly
        from app.stages.base import resolve_current_assembly

        genome_ref, _ = resolve_current_assembly(
            state, prefer_stages=["t2t", "hic_scaffolding", "assembly_review"]
        )
        genome_source = "final_assembly_for_annotation"
        if not genome_ref:
            return _fail_update(
                state,
                "missing final_assembly_for_annotation for gene_annotation fallback",
            )

    rnaseq_refs = _asset_paths(state, {"clean_rnaseq_reads", "raw_rnaseq_reads"})
    long_rna_refs = _asset_paths(
        state,
        {"clean_isoseq_reads", "transcript_ccs_reads", "raw_isoseq_or_long_rna_reads"},
    )
    protein_refs = _asset_paths(state, {"reference_proteins"})
    repeat_ref = _repeat_annotation_ref(state)
    base_payload = {
        "genome_input_ref": genome_ref,
        "genome_input_source": genome_source,
        "unmasked_fallback_reason": fallback_reason,
        "repeat_annotation_ref": repeat_ref,
        "rnaseq_read_refs": rnaseq_refs,
        "long_rna_read_refs": long_rna_refs,
        "protein_evidence_refs": protein_refs,
        "lineage_or_species_hints": _lineage_or_species_hints(state),
    }
    provided = _provided_gene_annotation_refs(state)
    if any(provided.values()):
        missing = []
        if not provided.get("gene_models_ref"):
            missing.append("gene_models")
        if not provided.get("predicted_proteins_ref"):
            missing.append("predicted_proteins")
        if missing:
            return _fail_update(
                state,
                f"provided gene_annotation assets require {', '.join(missing)}",
            )
        review_triggers = _provided_annotation_review_triggers(
            {
                **base_payload,
                "transcripts_fasta_ref": provided.get("transcripts_fasta_ref"),
            }
        )
        return _update_gene_output(
            state,
            {
                **base_payload,
                "status": "provided_annotation_resolved",
                "gene_models_ref": provided["gene_models_ref"],
                "gene_models": provided["gene_models_ref"],
                "genes_gff3_ref": provided["gene_models_ref"],
                "predicted_proteins_ref": provided["predicted_proteins_ref"],
                "predicted_proteins": provided["predicted_proteins_ref"],
                "proteins_fasta_ref": provided["predicted_proteins_ref"],
                "transcripts_fasta_ref": provided.get("transcripts_fasta_ref"),
                "evidence_profile": "provided_annotation",
                "candidate_methods": [],
                "helper_methods": [],
                "method_selection_required": False,
                "review_required": bool(review_triggers),
                "review_triggers": review_triggers,
                "notes": [
                    "operator_provided_gene_annotation_adopted",
                    "gene_prediction_execution_not_requested",
                ],
            },
        )
    return _update_gene_output(state, {**base_payload, "status": "resolved"})


def select_evidence_profile(state: TopLevelState) -> dict:
    """Select the evidence profile from available annotation evidence."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "resolved":
        return {}

    available = []
    if gene_out.get("rnaseq_read_refs"):
        available.append("rnaseq")
    if gene_out.get("long_rna_read_refs"):
        available.append("long_rna")
    if gene_out.get("protein_evidence_refs"):
        available.append("protein")

    if not available:
        profile = "no_evidence"
    elif len(available) > 1:
        profile = "mixed_evidence"
    else:
        profile = available[0]

    return _update_gene_output(
        state,
        {
            "status": "evidence_profile_selected",
            "evidence_profile": profile,
            "available_evidence": available,
        },
    )


def plan_gene_methods(state: TopLevelState) -> dict:
    """List executable candidate gene annotation methods without selecting a default."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "evidence_profile_selected":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    profile = gene_out["evidence_profile"]
    candidate_methods = _candidate_methods(profile)
    helper_methods = _helper_methods(gene_out.get("available_evidence", []))
    required_methods = [*candidate_methods, *helper_methods]
    missing = [method for method in required_methods if method not in registry.methods]
    if missing:
        return _fail_update(state, f"gene_annotation descriptors missing: {missing}")

    work_dir = stage_work_dir(state, "gene_annotation")
    expected_outputs = {
        "genes_gff3": str(work_dir / "genes.gff3"),
        "transcripts_fasta": str(work_dir / "transcripts.fa"),
        "predicted_proteins": str(work_dir / "proteins.fa"),
        "evidence_report": str(work_dir / "evidence_report.json"),
    }
    notes = ["gene_annotation_methods_require_explicit_execution_selection"]
    if profile == "no_evidence":
        notes.append("ab_initio_planning_explicit_no_evidence")
    return _update_gene_output(
        state,
        {
            "status": "methods_planned",
            "candidate_methods": candidate_methods,
            "helper_methods": helper_methods,
            "method_selection_required": True,
            "expected_outputs": expected_outputs,
            "notes": notes,
        },
    )


def plan_evidence_merge_or_review(state: TopLevelState) -> dict:
    """Write a gene annotation plan and record review triggers (merge prep)."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "candidates_accepted":
        return {}

    review_triggers = _review_triggers(gene_out)
    review_required = bool(review_triggers)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = stage_artifacts_dir(state, "gene_annotation")
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = GeneAnnotationPlan(
        session_id=session_id,
        status="planned",
        genome_input_ref=gene_out["genome_input_ref"],
        genome_input_source=gene_out["genome_input_source"],
        unmasked_fallback_reason=gene_out.get("unmasked_fallback_reason"),
        repeat_annotation_ref=gene_out.get("repeat_annotation_ref"),
        rnaseq_read_refs=gene_out.get("rnaseq_read_refs", []),
        long_rna_read_refs=gene_out.get("long_rna_read_refs", []),
        protein_evidence_refs=gene_out.get("protein_evidence_refs", []),
        lineage_or_species_hints=gene_out.get("lineage_or_species_hints", {}),
        evidence_profile=gene_out["evidence_profile"],
        candidate_methods=gene_out.get("candidate_methods", []),
        helper_methods=gene_out.get("helper_methods", []),
        expected_outputs=gene_out.get("expected_outputs", {}),
        review_required=review_required,
        review_triggers=review_triggers,
        method_selection_required=True,
        notes=gene_out.get("notes", []),
        metadata={"execution_enabled": bool(gene_out.get("executed_methods"))},
    )
    plan_ref = out_dir / "gene_annotation_plan.json"
    plan_ref.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        plan_ref,
        stage="gene_annotation",
        method_name=GENE_CONTRACT_METHOD,
        artifact_type="gene_annotation.plan",
        metadata={
            "evidence_profile": gene_out["evidence_profile"],
            "genome_input_source": gene_out["genome_input_source"],
            "execution_enabled": bool(gene_out.get("executed_methods")),
            "review_required": review_required,
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return _update_gene_output(
        state,
        {
            "status": "merge_ready",
            "review_required": review_required,
            "review_triggers": review_triggers,
            "plan_artifact_ref": str(plan_ref),
            "artifact_refs": [
                *gene_out.get("artifact_refs", []),
                str(artifact.file_path),
            ],
        },
    )


def commit_gene_annotation(state: TopLevelState) -> dict:
    """Publish the gene annotation bundle and advance to function annotation."""
    gene_out = _gene_output(state)
    if gene_out.get("status") == "provided_annotation_resolved":
        return _commit_adopted_gene_annotation(state)
    return _commit_planned_gene_annotation(state)


def _resolve_final_gene_set(
    gene_out: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Resolve the materialized final gene set: (gff, proteins, cds, error).

    Prefers the merged/passthrough refs set during EVM collect; falls back to a
    single accepted candidate bundle. proteins.fa must exist on disk.
    """
    gff_ref = gene_out.get("final_gff_ref")
    proteins_ref = gene_out.get("final_proteins_ref")
    cds_ref = gene_out.get("final_cds_ref")
    if not proteins_ref:
        accepted = gene_out.get("accepted_candidate_refs") or {}
        bundles = gene_out.get("candidate_bundles") or {}
        method = next(iter(accepted), None)
        bundle = bundles.get(method) if method else None
        if bundle:
            gff_ref = gff_ref or bundle.get("annotation_gff_ref")
            proteins_ref = bundle.get("proteins_fasta_ref")
            cds_ref = bundle.get("cds_fasta_ref")
    if not proteins_ref:
        return (
            gff_ref,
            None,
            None,
            "no materialized proteins_fasta_ref for final gene set",
        )
    if not Path(str(proteins_ref)).exists():
        return (
            gff_ref,
            None,
            None,
            f"final proteins.fa does not exist on disk: {proteins_ref}",
        )
    return gff_ref, str(proteins_ref), (str(cds_ref) if cds_ref else None), None


def _commit_planned_gene_annotation(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = stage_artifacts_dir(state, "gene_annotation")
    out_dir.mkdir(parents=True, exist_ok=True)
    expected_outputs = gene_out["expected_outputs"]
    final_gff_ref, final_proteins_ref, final_cds_ref, final_error = (
        _resolve_final_gene_set(gene_out)
    )
    if final_error:
        return _fail_update(state, final_error)
    genes_gff3_ref = final_gff_ref or expected_outputs["genes_gff3"]
    transcripts_ref = (
        final_cds_ref
        or gene_out.get("transcripts_fasta_ref")
        or expected_outputs["transcripts_fasta"]
    )
    bundle = GeneAnnotationBundle(
        session_id=session_id,
        status="planned",
        genome_input_ref=gene_out["genome_input_ref"],
        genome_input_source=gene_out["genome_input_source"],
        genes_gff3_ref=genes_gff3_ref,
        transcripts_fasta_ref=transcripts_ref,
        predicted_proteins_ref=final_proteins_ref,
        evidence_report_ref=expected_outputs["evidence_report"],
        repeat_annotation_ref=gene_out.get("repeat_annotation_ref"),
        evidence_profile=gene_out["evidence_profile"],
        candidate_methods=gene_out.get("candidate_methods", []),
        helper_methods=gene_out.get("helper_methods", []),
        review_required=gene_out.get("review_required", False),
        review_triggers=gene_out.get("review_triggers", []),
        plan_artifact_ref=gene_out.get("plan_artifact_ref"),
        notes=gene_out.get("notes", []),
        metadata={"execution_enabled": False},
    )
    bundle_ref = out_dir / "gene_annotation_bundle.json"
    bundle_ref.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        bundle_ref,
        stage="gene_annotation",
        method_name=GENE_CONTRACT_METHOD,
        artifact_type="gene_annotation.bundle",
        metadata={
            "evidence_profile": gene_out["evidence_profile"],
            "genome_input_source": gene_out["genome_input_source"],
            "execution_enabled": False,
            "review_required": gene_out.get("review_required", False),
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])

    outputs = dict(state.get("stage_outputs") or {})
    outputs["gene_annotation"] = {
        **gene_out,
        "status": "completed",
        "gene_models_ref": genes_gff3_ref,
        "gene_models": genes_gff3_ref,
        "genes_gff3_ref": genes_gff3_ref,
        "transcripts_fasta_ref": transcripts_ref,
        "predicted_proteins_ref": final_proteins_ref,
        "predicted_proteins": final_proteins_ref,
        "proteins_fasta_ref": final_proteins_ref,
        "evidence_report_ref": expected_outputs["evidence_report"],
        "bundle_artifact_ref": str(bundle_ref),
        "artifact_refs": [*gene_out.get("artifact_refs", []), str(artifact.file_path)],
    }
    trace = append_route_trace(state, "gene_annotation_stage")
    return {
        "stage_outputs": outputs,
        "current_stage": next_stage_name("gene_annotation"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def _commit_adopted_gene_annotation(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    out_dir = stage_artifacts_dir(state, "gene_annotation")
    out_dir.mkdir(parents=True, exist_ok=True)
    review_triggers = list(gene_out.get("review_triggers") or [])
    bundle = GeneAnnotationBundle(
        session_id=session_id,
        status="adopted_existing_annotation",
        genome_input_ref=gene_out["genome_input_ref"],
        genome_input_source=gene_out["genome_input_source"],
        genes_gff3_ref=gene_out["genes_gff3_ref"],
        transcripts_fasta_ref=gene_out.get("transcripts_fasta_ref"),
        predicted_proteins_ref=gene_out["predicted_proteins_ref"],
        evidence_report_ref=None,
        repeat_annotation_ref=gene_out.get("repeat_annotation_ref"),
        evidence_profile="provided_annotation",
        candidate_methods=[],
        helper_methods=[],
        review_required=bool(review_triggers),
        review_triggers=review_triggers,
        plan_artifact_ref=None,
        notes=gene_out.get("notes", []),
        metadata={
            "execution_enabled": False,
            "adoption_mode": "provided_gene_annotation",
            "source": "manifest",
        },
    )
    bundle_ref = out_dir / "gene_annotation_bundle.json"
    bundle_ref.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Manifest-provided annotation files remain inventory-owned external
    # assets.  The artifact index only owns immutable files published inside
    # this run; indexing the external sources would falsely transfer
    # ownership and break portable run-relative references.
    artifacts: list[ArtifactMeta] = []
    bundle_artifact = ArtifactMeta.from_file(
        bundle_ref,
        stage="gene_annotation",
        method_name=GENE_CONTRACT_METHOD,
        artifact_type="gene_annotation.bundle",
        metadata={
            "evidence_profile": "provided_annotation",
            "genome_input_source": gene_out["genome_input_source"],
            "execution_enabled": False,
            "adoption_mode": "provided_gene_annotation",
            "review_required": bool(review_triggers),
        },
    )
    artifacts.append(bundle_artifact)
    append_artifact_records(data_dir, session_id, artifacts)

    outputs = dict(state.get("stage_outputs") or {})
    outputs["gene_annotation"] = {
        **gene_out,
        "status": "completed",
        "bundle_status": "adopted_existing_annotation",
        "method_selection_required": False,
        "evidence_report_ref": None,
        "bundle_artifact_ref": str(bundle_ref),
        "artifact_refs": [str(artifact.file_path) for artifact in artifacts],
    }
    trace = append_route_trace(state, "gene_annotation_stage")
    return {
        "stage_outputs": outputs,
        "current_stage": next_stage_name("gene_annotation"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def fail_gene_annotation(state: TopLevelState) -> dict:
    """Stop workflow on invalid gene annotation inputs or descriptors."""
    gene_out = _gene_output(state)
    return {
        "current_stage": "gene_annotation",
        "supervision_state": "failed_terminal",
        "last_error": gene_out.get("reason", "gene_annotation failed"),
    }


# --- Annotation review chain (Phase 7 task 03) ---------------------------------
#
# Each gate follows the single-gate architecture already used by mask_review and
# hic_scaffolding: ``block_<gate>`` writes a request artifact, evaluates policy,
# and either auto-decides (calling its applier inline) or blocks. The applier
# records the decision then continues the chain inline through the next node(s)
# until the next gate blocks or ``commit_gene_annotation`` advances the stage.

GENE_REVIEW_GATES = (
    "gene_method_planning_review",
    "candidate_evidence_review",
    "merge_review",
    "final_gene_set_review",
)


def _gene_review_policy(state: TopLevelState) -> dict[str, Any]:
    return _gene_output(state).get("review_policy") or {"mode": "auto"}


def _write_gene_gate_request(
    state: TopLevelState,
    gate_id: str,
    payload: ReviewPayload,
) -> PersistedReviewRequest:
    return persist_review_request(
        state,
        stage="gene_annotation",
        gate_id=gate_id,
        reason=payload.reason,
        payload=payload,
        method_name=GENE_CONTRACT_METHOD,
        artifact_type=f"gene_annotation.{gate_id}_request",
    )


def _run_gene_gate(
    state: TopLevelState,
    *,
    gate_id: str,
    reason: str,
    payload: ReviewPayload,
    applier: Callable[[TopLevelState, ResumeReviewRequest], dict],
    auto_decision: str,
) -> dict:
    """Write the request, evaluate policy, then auto-decide or block."""
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    persisted_request = _write_gene_gate_request(state, gate_id, payload)
    request_ref = persisted_request.request_ref

    review_policy = _gene_review_policy(state)
    outcome = evaluate("gene_annotation", payload, review_policy)
    pending = outcome.action == "BLOCK_PENDING_CONFIRM"

    Ledger(data_dir).record_review_requested(
        session_id,
        "gene_annotation",
        reason=reason,
        signals=[signal.model_dump(mode="json") for signal in payload.signals],
        candidates=payload.candidates,
        review_request_artifact_ref=request_ref.model_dump(mode="json"),
        auto_decidable=payload.auto_decidable,
        policy_mode=outcome.mode,
        pending_human_confirmation=pending,
    )

    if outcome.action == "AUTO_DECIDE_NOW":
        auto_request = ResumeReviewRequest(
            session_id=session_id,
            stage="gene_annotation",
            gate_id=gate_id,
            review_request_id=persisted_request.review_request_id,
            review_request_hash=persisted_request.request_hash,
            decision=auto_decision,
            decided_by="auto_rule",
            rationale_artifact_ref=request_ref.path_ref,
        )
        return applier(state, auto_request)

    gene_out = _gene_output(state)
    outputs = dict(state.get("stage_outputs") or {})
    outputs["gene_annotation"] = {
        **gene_out,
        "status": "blocked_human",
        "active_gate_id": gate_id,
        "review_request_artifact_ref": request_ref.model_dump(mode="json"),
        "review_policy_outcome": outcome.model_dump(mode="json"),
        "auto_decidable": payload.auto_decidable,
        "pending_human_confirmation": pending,
    }
    return {
        "current_stage": "gene_annotation",
        "supervision_state": "blocked_human",
        "stage_outputs": outputs,
        "active_interrupt": persisted_request.compact_interrupt(
            stage="gene_annotation",
            gate_id=gate_id,
            reason=reason,
        ),
    }


def _record_gene_gate_decision(
    state: TopLevelState,
    gate_id: str,
    request: ResumeReviewRequest,
    payload_extra: dict[str, Any],
) -> dict[str, Any]:
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    decision_payload = {
        "session_id": session_id,
        "stage": "gene_annotation",
        "gate_id": gate_id,
        "decision": request.decision,
        "notes": list(request.notes),
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
        "metadata": request.metadata,
        **payload_extra,
    }
    active_interrupt = state.get("active_interrupt") or {}
    review_request_id = request.review_request_id or active_interrupt.get(
        "review_request_id"
    )
    review_request_hash = request.review_request_hash or active_interrupt.get(
        "request_hash"
    )
    if not review_request_id or not review_request_hash:
        raise ResumeValidationError(
            f"{gate_id} decision is missing review request binding"
        )
    decision_ref = persist_review_decision(
        data_dir,
        session_id=session_id,
        review_request_id=review_request_id,
        stage="gene_annotation",
        gate_id=gate_id,
        decision=decision_payload,
        request_hash=review_request_hash,
        method_name=GENE_CONTRACT_METHOD,
        artifact_type=f"gene_annotation.{gate_id}_decision",
    )
    Ledger(data_dir).record_review_decision(
        session_id,
        "gene_annotation",
        decision=request.decision,
        notes=list(request.notes),
        artifact_ref=decision_ref.model_dump(mode="json"),
        metadata=request.metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
        gate_id=gate_id,
        review_request_id=review_request_id,
    )
    return decision_ref.model_dump(mode="json")


def _gene_gate_own_update(
    state: TopLevelState,
    gate_id: str,
    request: ResumeReviewRequest,
    gene_fields: dict[str, Any],
    decision_ref: dict[str, Any],
) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["gene_annotation"] = {
        **_gene_output(state),
        **gene_fields,
        "decision": request.decision,
        "decided_by": request.decided_by,
        f"{gate_id}_decision_ref": decision_ref,
        "active_gate_id": None,
    }
    return {
        "stage_outputs": outputs,
        "route_trace": append_route_trace(state, gate_id),
        "active_interrupt": None,
    }


def _gene_gate_stop(
    state: TopLevelState, gate_id: str, request: ResumeReviewRequest
) -> dict:
    decision_ref = _record_gene_gate_decision(state, gate_id, request, {})
    own = _gene_gate_own_update(
        state, gate_id, request, {"status": "stopped"}, decision_ref
    )
    return {
        **own,
        "current_stage": "gene_annotation",
        "supervision_state": "failed_terminal",
        "last_error": f"{gate_id} stopped by reviewer",
    }


def _require_gene_stage(request: ResumeReviewRequest, gate_id: str) -> None:
    if request.stage != "gene_annotation":
        raise ResumeValidationError(f"{gate_id} resume requires stage gene_annotation")


def _gene_method_readiness_issues(
    state: TopLevelState, methods: list[str]
) -> list[str]:
    issues: list[str] = []
    if AUGUSTUS_METHOD in methods and not _lineage_or_species_hints(state).get(
        "augustus_species_model"
    ):
        issues.append(
            "gene_annotation.candidate.augustus requires augustus_species_model before execution"
        )
    return issues


# --- gate 1: gene method planning ----------------------------------------------


def build_gene_method_planning_review_payload(state: TopLevelState) -> ReviewPayload:
    gene_out = _gene_output(state)
    candidate_methods = list(gene_out.get("candidate_methods") or [])
    triggers = _review_triggers(gene_out)
    conflict = "rna_protein_evidence_conflict_review" in triggers
    readiness_issues = _gene_method_readiness_issues(state, candidate_methods)
    candidates = [
        {
            "method_id": method,
            "evidence_profile": gene_out.get("evidence_profile"),
            "readiness_issues": _gene_method_readiness_issues(state, [method]),
        }
        for method in candidate_methods
    ]
    signals = [
        ReviewSignal(
            signal_name="evidence_profile",
            method_name=GENE_CONTRACT_METHOD,
            status="available" if gene_out.get("evidence_profile") else "missing",
            metadata={"evidence_profile": gene_out.get("evidence_profile")},
        )
    ]
    signals.extend(
        ReviewSignal(
            signal_name="method_readiness",
            method_name=method,
            status="missing",
            metadata={"issues": _gene_method_readiness_issues(state, [method])},
        )
        for method in candidate_methods
        if _gene_method_readiness_issues(state, [method])
    )
    return ReviewPayload(
        stage="gene_annotation",
        gate_id="gene_method_planning_review",
        reason="gene_method_planning_review",
        candidates=candidates,
        signals=signals,
        decisions_allowed=["select_methods", "accept", "stop"],
        auto_decidable=len(candidate_methods) == 1
        and not conflict
        and not readiness_issues,
        notes=[*gene_out.get("notes", []), *readiness_issues],
    )


def block_gene_method_planning_review(state: TopLevelState) -> dict:
    if _gene_output(state).get("status") != "methods_planned":
        return {}
    return _run_gene_gate(
        state,
        gate_id="gene_method_planning_review",
        reason="gene_method_planning_review",
        payload=build_gene_method_planning_review_payload(state),
        applier=apply_gene_method_planning_review_decision,
        auto_decision="accept",
    )


def apply_gene_method_planning_review_decision(
    state: TopLevelState, request: ResumeReviewRequest
) -> dict:
    _require_gene_stage(request, "gene_method_planning_review")
    if request.decision not in {"select_methods", "accept", "stop"}:
        raise ResumeValidationError(
            f"unsupported gene_method_planning_review decision: {request.decision!r}"
        )
    if request.decision == "stop":
        return _gene_gate_stop(state, "gene_method_planning_review", request)

    gene_out = _gene_output(state)
    candidate_methods = list(gene_out.get("candidate_methods") or [])
    if request.decision == "select_methods":
        confirmed = list(request.metadata.get("selected_methods") or [])
        if not confirmed:
            raise ResumeValidationError(
                "select_methods requires metadata.selected_methods"
            )
    else:
        confirmed = candidate_methods

    readiness_issues = _gene_method_readiness_issues(state, confirmed)
    if readiness_issues:
        raise ResumeValidationError("; ".join(readiness_issues))

    decision_ref = _record_gene_gate_decision(
        state,
        "gene_method_planning_review",
        request,
        {"confirmed_methods": confirmed},
    )
    own = _gene_gate_own_update(
        state,
        "gene_method_planning_review",
        request,
        {"status": "methods_confirmed", "confirmed_methods": confirmed},
        decision_ref,
    )
    # Only this gate may rewrite the downstream stage suffix.
    if request.decision == "select_methods" and state.get("requested_stages"):
        downstream = [
            stage
            for stage in state.get("requested_stages", [])
            if stage in DOWNSTREAM_STAGES
        ]
        own["requested_stages"] = rewrite_requested_stages_after_assembly_review(
            state.get("requested_stages"), downstream
        )
    return own


# --- executable method preparation ---------------------------------------------


def prepare_gene_method_execution(state: TopLevelState) -> dict:
    """Prepare output targets for confirmed executable methods."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "methods_confirmed":
        return {}
    methods = list(
        gene_out.get("confirmed_methods") or gene_out.get("candidate_methods") or []
    )
    unsupported = [
        method for method in methods if method not in GENE_EXECUTABLE_METHODS
    ]
    if unsupported:
        return _fail_update(
            state,
            "gene_annotation confirmed methods are not locally executable: "
            + ", ".join(sorted(unsupported)),
        )
    work_dir = stage_work_dir(state, "gene_annotation")
    candidate_refs = {m: str(work_dir / f"{m.split('.')[-1]}.gff3") for m in methods}
    return _update_gene_output(
        state,
        {
            "status": "pending_execution",
            "candidate_refs": candidate_refs,
            "candidate_gene_counts": {m: 0 for m in methods},
            "executed_methods": [],
        },
    )


# --- gate 2: candidate evidence ------------------------------------------------


def build_candidate_evidence_review_payload(state: TopLevelState) -> ReviewPayload:
    gene_out = _gene_output(state)
    candidate_refs = dict(gene_out.get("candidate_refs") or {})
    gene_counts = dict(gene_out.get("candidate_gene_counts") or {})
    bundles = dict(gene_out.get("candidate_bundles") or {})
    triggers = _review_triggers(gene_out)
    conflict = "rna_protein_evidence_conflict_review" in triggers
    candidates = [
        {
            "method_id": method,
            "candidate_ref": ref,
            "gene_count": gene_counts.get(method, 0),
            "proteins_fasta_ref": (bundles.get(method) or {}).get("proteins_fasta_ref"),
            "conflict_signals": ["rna_protein_evidence_conflict_review"]
            if conflict
            else [],
        }
        for method, ref in candidate_refs.items()
    ]
    # EVM is offered as a post-review merge action only when multiple candidates
    # exist; a single accepted candidate skips EVM via passthrough.
    decisions = ["accept_candidate", "proceed_merge", "request_rerun", "stop"]
    if len(candidate_refs) >= 2:
        decisions.insert(1, "merge_with_evm")
    return ReviewPayload(
        stage="gene_annotation",
        gate_id="candidate_evidence_review",
        reason="candidate_evidence_review",
        candidates=candidates,
        decisions_allowed=decisions,
        auto_decidable=not conflict,
    )


def block_candidate_evidence_review(state: TopLevelState) -> dict:
    if _gene_output(state).get("status") != "candidates_ready":
        return {}
    return _run_gene_gate(
        state,
        gate_id="candidate_evidence_review",
        reason="candidate_evidence_review",
        payload=build_candidate_evidence_review_payload(state),
        applier=apply_candidate_evidence_review_decision,
        auto_decision="proceed_merge",
    )


def apply_candidate_evidence_review_decision(
    state: TopLevelState, request: ResumeReviewRequest
) -> dict:
    _require_gene_stage(request, "candidate_evidence_review")
    if request.decision not in {
        "accept_candidate",
        "merge_with_evm",
        "proceed_merge",
        "request_rerun",
        "stop",
    }:
        raise ResumeValidationError(
            f"unsupported candidate_evidence_review decision: {request.decision!r}"
        )
    if request.decision == "stop":
        return _gene_gate_stop(state, "candidate_evidence_review", request)

    gene_out = _gene_output(state)
    candidate_refs = dict(gene_out.get("candidate_refs") or {})
    if request.decision == "request_rerun":
        decision_ref = _record_gene_gate_decision(
            state, "candidate_evidence_review", request, {}
        )
        own = _gene_gate_own_update(
            state,
            "candidate_evidence_review",
            request,
            {
                "status": "methods_confirmed",
                "candidate_refs": {},
                "accepted_candidate_refs": {},
            },
            decision_ref,
        )
        return own

    # accept_candidate keeps a single operator-chosen candidate (EVM passthrough);
    # merge_with_evm / proceed_merge keep all candidates for an EVM merge.
    if request.decision == "accept_candidate":
        chosen = request.method_name or request.candidate_id
        if not chosen or chosen not in candidate_refs:
            raise ResumeValidationError(
                f"accept_candidate requires method_name of an executed candidate; got {chosen!r}"
            )
        accepted = {chosen: candidate_refs[chosen]}
    else:
        accepted = candidate_refs

    decision_ref = _record_gene_gate_decision(
        state,
        "candidate_evidence_review",
        request,
        {"accepted_candidate_refs": accepted},
    )
    own = _gene_gate_own_update(
        state,
        "candidate_evidence_review",
        request,
        {"status": "candidates_accepted", "accepted_candidate_refs": accepted},
        decision_ref,
    )
    return own


# --- gate 3: merge / EVM weights -----------------------------------------------

_DEFAULT_MERGE_WEIGHTS = {"ab_initio": 1, "rnaseq": 5, "protein": 5}


def build_merge_review_payload(state: TopLevelState) -> ReviewPayload:
    gene_out = _gene_output(state)
    triggers = _review_triggers(gene_out)
    merge_policy = "evidence_merge_policy_required" in triggers
    available = set(gene_out.get("available_evidence", []))
    signals = [
        ReviewSignal(
            signal_name=f"evidence_class.{evidence_class}",
            method_name=GENE_CONTRACT_METHOD,
            status="available" if evidence_class in available else "missing",
        )
        for evidence_class in ("rnaseq", "long_rna", "protein")
    ]
    return ReviewPayload(
        stage="gene_annotation",
        gate_id="merge_review",
        reason="merge_review",
        signals=signals,
        decisions_allowed=["accept_weights", "adjust_weights", "stop"],
        auto_decidable=not merge_policy,
        candidate_comparison_context={"evm_weight_table": dict(_DEFAULT_MERGE_WEIGHTS)},
    )


def block_merge_review(state: TopLevelState) -> dict:
    if _gene_output(state).get("status") != "merge_ready":
        return {}
    return _run_gene_gate(
        state,
        gate_id="merge_review",
        reason="merge_review",
        payload=build_merge_review_payload(state),
        applier=apply_merge_review_decision,
        auto_decision="accept_weights",
    )


def apply_merge_review_decision(
    state: TopLevelState, request: ResumeReviewRequest
) -> dict:
    _require_gene_stage(request, "merge_review")
    if request.decision not in {"accept_weights", "adjust_weights", "stop"}:
        raise ResumeValidationError(
            f"unsupported merge_review decision: {request.decision!r}"
        )
    if request.decision == "stop":
        return _gene_gate_stop(state, "merge_review", request)

    if request.decision == "adjust_weights":
        merge_weights = dict(request.metadata.get("merge_weights") or {})
        if not merge_weights:
            raise ResumeValidationError(
                "adjust_weights requires metadata.merge_weights"
            )
    else:
        merge_weights = dict(_DEFAULT_MERGE_WEIGHTS)

    decision_ref = _record_gene_gate_decision(
        state,
        "merge_review",
        request,
        {"merge_weights": merge_weights},
    )
    own = _gene_gate_own_update(
        state,
        "merge_review",
        request,
        {"status": "merge_accepted", "merge_weights": merge_weights},
        decision_ref,
    )
    return own


# --- gate 4: final gene set + BUSCO --------------------------------------------


def build_final_gene_set_review_payload(state: TopLevelState) -> ReviewPayload:
    gene_out = _gene_output(state)
    busco_score = gene_out.get("busco_completeness")
    busco_status = gene_out.get("busco_completeness_status", "missing")
    busco = ReviewSignal(
        signal_name="busco_completeness",
        method_name=GENE_CONTRACT_METHOD,
        status=busco_status,
        metadata={"score": busco_score} if busco_score is not None else {},
    )
    auto_decidable = busco_status == "missing" or (
        isinstance(busco_score, (int, float)) and busco_score >= 80.0
    )
    return ReviewPayload(
        stage="gene_annotation",
        gate_id="final_gene_set_review",
        reason="final_gene_set_review",
        signals=[busco],
        decisions_allowed=["accept", "reject", "stop"],
        auto_decidable=auto_decidable,
    )


def block_final_gene_set_review(state: TopLevelState) -> dict:
    if _gene_output(state).get("status") != "merge_accepted":
        return {}
    return _run_gene_gate(
        state,
        gate_id="final_gene_set_review",
        reason="final_gene_set_review",
        payload=build_final_gene_set_review_payload(state),
        applier=apply_final_gene_set_review_decision,
        auto_decision="accept",
    )


def apply_final_gene_set_review_decision(
    state: TopLevelState, request: ResumeReviewRequest
) -> dict:
    _require_gene_stage(request, "final_gene_set_review")
    if request.decision not in {"accept", "reject", "stop"}:
        raise ResumeValidationError(
            f"unsupported final_gene_set_review decision: {request.decision!r}"
        )
    if request.decision == "stop":
        return _gene_gate_stop(state, "final_gene_set_review", request)

    if request.decision == "reject":
        decision_ref = _record_gene_gate_decision(
            state, "final_gene_set_review", request, {}
        )
        own = _gene_gate_own_update(
            state,
            "final_gene_set_review",
            request,
            {"status": "candidates_accepted", "merge_weights": {}},
            decision_ref,
        )
        return own

    decision_ref = _record_gene_gate_decision(
        state, "final_gene_set_review", request, {}
    )
    own = _gene_gate_own_update(
        state,
        "final_gene_set_review",
        request,
        {"status": "review_passed"},
        decision_ref,
    )
    return own


# --- method execution (Phase 7 task 04) ---------------------------------------

# Each entry maps a confirmed method to its execution statuses and naming
# conventions.  Adding a new executable method only requires a new entry here,
# the corresponding freeze / run / collect nodes, and the builder edge wiring.
GENE_EXECUTABLE_METHODS: dict[str, dict[str, str]] = {
    "gene_annotation.candidate.miniprot": {
        "suffix": "miniprot",
        "frozen_status": "miniprot_frozen",
        "ran_status": "miniprot_ran",
    },
    "gene_annotation.candidate.augustus": {
        "suffix": "augustus",
        "frozen_status": "augustus_frozen",
        "ran_status": "augustus_ran",
    },
}

CANDIDATE_GFF_TYPE = "gene_annotation.candidate_gff"
AUGUSTUS_METHOD = "gene_annotation.candidate.augustus"
GFFREAD_METHOD = "gene_annotation.materialize.gffread"
CANDIDATE_PROTEINS_TYPE = "gene_annotation.candidate_proteins"

# Per-candidate EVM evidence class, keyed by method. Used both for EVM weight
# selection and for the standardized candidate bundle. Methods absent here
# default to ABINITIO_PREDICTION.
_REGISTRY_PATH = Path("descriptors/registry.yaml")


@lru_cache(maxsize=1)
def _gene_registry() -> DescriptorRegistry:
    return DescriptorRegistry.load(_REGISTRY_PATH)


def _method_evm_class(method_name: str) -> str:
    """EVM evidence class for a candidate, sourced from its descriptor's evm block."""
    method = _gene_registry().methods.get(method_name)
    evm = method.evm if method else None
    return (evm or {}).get("evidence_class", "ABINITIO_PREDICTION")


# Weight bucket each evidence class draws from in merge_weights.
_EVM_CLASS_WEIGHT_KEY = {
    "ABINITIO_PREDICTION": "ab_initio",
    "PROTEIN": "protein",
    "TRANSCRIPT": "rnaseq",
    "OTHER_PREDICTION": "ab_initio",
}


def _materialize_candidate_proteins(
    state: TopLevelState,
    *,
    method_name: str,
    suffix: str,
    output_gff3: Path,
) -> tuple[Path | None, Path | None, str | None]:
    """Translate proteins.fa + cds.fa from a candidate GFF via gffread.

    Returns ``(proteins_fa, cds_fa, None)`` on success, or ``(None, None, error)``
    when gffread fails or yields empty output. Methods without native protein
    output must call this before a candidate bundle is recorded; nothing may
    publish a proteins path that is not materialized on disk.
    """
    gene_out = _gene_output(state)
    genome_ref = gene_out.get("genome_input_ref")
    if not genome_ref:
        return None, None, f"{suffix} materialization requires genome_input_ref"

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    working_dir = stage_work_dir(state, "gene_annotation", suffix)
    working_dir.mkdir(parents=True, exist_ok=True)
    proteins_fa = working_dir / "proteins.fa"
    cds_fa = working_dir / "cds.fa"

    env_spec, _packages, blocked = resolve_method_env(
        gene_out,
        stage="gene_annotation",
        method_name=GFFREAD_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return None, None, str(blocked.get("reason") or "gffread env preflight blocked")

    script = (
        "set -e\n"
        f"gffread {shlex.quote(str(output_gff3))} -g {shlex.quote(str(genome_ref))}"
        f" -x {shlex.quote(str(cds_fa))} -y {shlex.quote(str(proteins_fa))}\n"
        f"test -s {shlex.quote(str(proteins_fa))}\n"
    )
    script_path = working_dir / f"materialize_{suffix}.sh"
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:gene_annotation:{suffix}:materialize",
        session_id=session_id,
        stage="gene_annotation",
        method_name=GFFREAD_METHOD,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[proteins_fa],
        env=gene_out.get("env"),
        env_spec=env_spec,
        metadata={"source_gff3": str(output_gff3), "candidate_method": method_name},
    )
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=f"{suffix} materialization",
    )
    if not outcome.success:
        return None, None, outcome.error or f"{suffix} protein materialization failed"
    cds_ref = cds_fa if cds_fa.exists() else None
    return proteins_fa, cds_ref, None


def _write_candidate_bundle(
    state: TopLevelState,
    *,
    method_name: str,
    suffix: str,
    output_gff3: Path,
    proteins_fa: Path,
    cds_fa: Path | None,
    gene_count: int,
) -> dict[str, Any]:
    """Write minimal metrics/provenance and return a CandidateBundle dict."""
    work_dir = stage_work_dir(state, "gene_annotation", suffix)
    work_dir.mkdir(parents=True, exist_ok=True)

    metrics_path = work_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(
            {"method_name": method_name, "gene_count": gene_count},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    provenance_path = work_dir / "provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "method_name": method_name,
                "annotation_gff_ref": str(output_gff3),
                "materialized_with": "gffread",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    evm_class = _method_evm_class(method_name)
    bundle = CandidateBundle(
        method_name=method_name,
        category=_method_category(method_name),
        annotation_gff_ref=str(output_gff3),
        proteins_fasta_ref=str(proteins_fa),
        cds_fasta_ref=str(cds_fa) if cds_fa else None,
        metrics_ref=str(metrics_path),
        provenance_ref=str(provenance_path),
        evm_evidence_class=evm_class,
        evm_source_type=suffix,
    )
    return bundle.model_dump(mode="json")


def _method_category(method_name: str) -> str:
    method = _gene_registry().methods.get(method_name)
    return (method.category if method else None) or "candidate"


def _evm_source_type(gene_out: dict[str, Any], method_name: str) -> str:
    bundles = gene_out.get("candidate_bundles") or {}
    bundle = bundles.get(method_name) or {}
    source_type = bundle.get("evm_source_type")
    if source_type:
        return str(source_type)
    return method_name.split(".")[-1]


def _evm_argument_bucket(evm_class: str) -> str:
    if evm_class == "PROTEIN":
        return "protein_alignments"
    if evm_class == "TRANSCRIPT":
        return "transcript_alignments"
    return "gene_predictions"


def _freeze_gene_method_attempt(
    state: TopLevelState,
    *,
    method_name: str,
    cfg: dict[str, str],
    script_body: str,
    extra_metadata: dict[str, Any] | None = None,
) -> dict:
    """Shared freeze path: resolve env, write script, produce FrozenAttemptSpec."""
    gene_out = _gene_output(state)
    genome_ref = gene_out.get("genome_input_ref")
    if not genome_ref:
        return _fail_update(state, f"{cfg['suffix']} requires genome_input_ref")

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]

    env_spec, _packages, blocked = resolve_method_env(
        gene_out,
        stage="gene_annotation",
        method_name=method_name,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_gene_output(state, blocked)

    working_dir = stage_work_dir(state, "gene_annotation", cfg["suffix"])
    working_dir.mkdir(parents=True, exist_ok=True)

    candidate_refs = dict(gene_out.get("candidate_refs") or {})
    output_gff3 = Path(
        candidate_refs.get(method_name, str(working_dir / f"{cfg['suffix']}.gff3"))
    )

    script_path = working_dir / f"run_{cfg['suffix']}.sh"
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:gene_annotation:{cfg['suffix']}",
        session_id=session_id,
        stage="gene_annotation",
        method_name=method_name,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[output_gff3],
        env=gene_out.get("env"),
        env_spec=env_spec,
        metadata={
            "genome_input_ref": str(genome_ref),
            "output_gff3": str(output_gff3),
            **(extra_metadata or {}),
        },
    )
    attempt_ref = persist_gene_attempt(state, attempt)

    return _update_gene_output(
        state,
        {
            "status": cfg["frozen_status"],
            f"{cfg['suffix']}_attempt_ref": attempt_ref.model_dump(mode="json"),
        },
    )


def _run_gene_method_attempt(
    state: TopLevelState,
    *,
    method_name: str,
    cfg: dict[str, str],
) -> dict:
    """Shared run path: hydrate a verified FrozenAttemptSpec and execute it."""
    gene_out = _gene_output(state)
    attempt = load_gene_attempt(
        state,
        gene_out[f"{cfg['suffix']}_attempt_ref"],
        expected_method_name=method_name,
    )
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label=cfg["suffix"],
    )
    execution_ref = persist_gene_execution(
        state,
        method_name=attempt.method_name,
        result=outcome.result,
    )
    if not outcome.success:
        return _update_gene_output(
            state,
            {
                "status": "failed",
                "reason": outcome.error,
                f"{cfg['suffix']}_execution_ref": execution_ref.model_dump(mode="json"),
            },
        )
    return _update_gene_output(
        state,
        {
            "status": cfg["ran_status"],
            f"{cfg['suffix']}_execution_ref": execution_ref.model_dump(mode="json"),
        },
    )


def _collect_gene_method_artifacts(
    state: TopLevelState, *, method_name: str, cfg: dict[str, str]
) -> dict:
    """Shared collect path: register GFF3 artifact and advance execution state."""
    gene_out = _gene_output(state)
    attempt = load_gene_attempt(
        state,
        gene_out[f"{cfg['suffix']}_attempt_ref"],
        expected_method_name=method_name,
    )
    load_gene_execution(
        state,
        gene_out[f"{cfg['suffix']}_execution_ref"],
        expected_attempt_id=attempt.attempt_id,
        expected_method_name=method_name,
    )
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    output_gff3 = Path(str(attempt.metadata["output_gff3"]))

    try:
        published = publish_or_recover_work_artifact(
            data_dir,
            session_id,
            output_gff3,
            stage="gene_annotation",
            method_name=method_name,
            artifact_type=CANDIDATE_GFF_TYPE,
            metadata={"attempt_id": attempt.attempt_id},
        )
    except FileNotFoundError:
        reason = f"expected {cfg['suffix']} GFF3 missing: {output_gff3}"
        Ledger(data_dir).record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=0,
            error=reason,
        )
        return _update_gene_output(state, {"status": "failed", "reason": reason})
    published_gff3 = published.path

    Ledger(data_dir).record_attempt_succeeded(
        attempt.session_id,
        attempt,
        artifacts=[published.ref.model_dump(mode="json")],
    )

    candidate_refs = dict(gene_out.get("candidate_refs") or {})
    candidate_refs[method_name] = str(published_gff3)
    candidate_artifact_refs = dict(gene_out.get("candidate_artifact_refs") or {})
    candidate_artifact_refs[method_name] = published.ref.model_dump(mode="json")

    gene_count = 0
    try:
        for line in published_gff3.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) >= 3 and fields[2] == "gene":
                gene_count += 1
    except (OSError, UnicodeDecodeError):
        gene_count = 0

    candidate_gene_counts = dict(gene_out.get("candidate_gene_counts") or {})
    candidate_gene_counts[method_name] = gene_count

    # Materialize a real proteins.fa (+ cds.fa) from the candidate GFF. A
    # candidate is only valid with a structural GFF AND an on-disk proteins
    # FASTA; nothing may publish a fabricated protein path downstream.
    proteins_fa, cds_fa, mat_error = _materialize_candidate_proteins(
        state,
        method_name=method_name,
        suffix=cfg["suffix"],
        output_gff3=published_gff3,
    )
    if mat_error:
        Ledger(data_dir).record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=0,
            error=mat_error,
        )
        return _update_gene_output(
            state, {"status": "materialization_failed", "reason": mat_error}
        )

    bundle = _write_candidate_bundle(
        state,
        method_name=method_name,
        suffix=cfg["suffix"],
        output_gff3=published_gff3,
        proteins_fa=proteins_fa,
        cds_fa=cds_fa,
        gene_count=gene_count,
    )
    candidate_bundles = dict(gene_out.get("candidate_bundles") or {})
    candidate_bundles[method_name] = bundle

    executed = list(gene_out.get("executed_methods") or [])
    executed.append(method_name)
    confirmed = list(gene_out.get("confirmed_methods") or [])
    executable = [m for m in confirmed if m in GENE_EXECUTABLE_METHODS]
    pending = [m for m in executable if m not in executed]

    return _update_gene_output(
        state,
        {
            "status": "pending_execution" if pending else "candidates_ready",
            "candidate_refs": candidate_refs,
            "candidate_artifact_refs": candidate_artifact_refs,
            "candidate_bundles": candidate_bundles,
            "candidate_gene_counts": candidate_gene_counts,
            "candidate_gff_ref": str(published_gff3),
            f"{cfg['suffix']}_gene_count": gene_count,
            "executed_methods": executed,
        },
    )


# --- miniprot freeze / run / collect ------------------------------------------

MINIPROT_METHOD = "gene_annotation.candidate.miniprot"


def freeze_miniprot_attempt(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    if gene_out.get("status") != "pending_execution":
        return {}
    if MINIPROT_METHOD not in (gene_out.get("confirmed_methods") or []):
        return {}
    if MINIPROT_METHOD in (gene_out.get("executed_methods") or []):
        return {}
    protein_refs = gene_out.get("protein_evidence_refs") or []
    if not protein_refs:
        return _fail_update(state, "miniprot requires protein_evidence_refs")
    genome_arg = shlex.quote(str(gene_out["genome_input_ref"]))
    protein_args = " ".join(shlex.quote(str(ref)) for ref in protein_refs)
    output_arg = shlex.quote(
        str(
            Path(gene_out.get("candidate_refs", {}).get(MINIPROT_METHOD, ""))
            or "miniprot.gff3"
        )
    )
    script = (
        "set -e\n"
        f"miniprot --gff {genome_arg} {protein_args} > {output_arg}\n"
        f"test -f {output_arg}\n"
    )
    return _freeze_gene_method_attempt(
        state,
        method_name=MINIPROT_METHOD,
        cfg=GENE_EXECUTABLE_METHODS[MINIPROT_METHOD],
        script_body=script,
        extra_metadata={"protein_evidence_refs": [str(ref) for ref in protein_refs]},
    )


def run_miniprot_attempt(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    if gene_out.get("status") != "miniprot_frozen":
        return {}
    return _run_gene_method_attempt(
        state,
        method_name=MINIPROT_METHOD,
        cfg=GENE_EXECUTABLE_METHODS[MINIPROT_METHOD],
    )


def collect_miniprot_artifacts(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    if gene_out.get("status") != "miniprot_ran":
        return {}
    return _collect_gene_method_artifacts(
        state,
        method_name=MINIPROT_METHOD,
        cfg=GENE_EXECUTABLE_METHODS[MINIPROT_METHOD],
    )


# --- augustus freeze / run / collect ------------------------------------------

_AUGUSTUS_NO_MODEL_MESSAGE = (
    "augustus requires an explicit augustus_species_model; --species is a trained "
    "species model, not a generic label. Options: "
    "(1) train a model with BUSCO --long (future prepare action), "
    "(2) use a nearby installed species model, or (3) skip augustus."
)


def freeze_augustus_attempt(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    if gene_out.get("status") != "pending_execution":
        return {}
    if AUGUSTUS_METHOD not in (gene_out.get("confirmed_methods") or []):
        return {}
    if AUGUSTUS_METHOD in (gene_out.get("executed_methods") or []):
        return {}
    hints = gene_out.get("lineage_or_species_hints") or {}
    species = hints.get("augustus_species_model")
    if not species:
        return _fail_update(state, _AUGUSTUS_NO_MODEL_MESSAGE)
    warnings: list[str] = []
    if hints.get("species_model_is_distant"):
        warnings.append("augustus_species_model_is_distant")
    if not gene_out.get("repeat_annotation_ref"):
        warnings.append("augustus_repeat_masking_missing")
    genome_arg = shlex.quote(str(gene_out["genome_input_ref"]))
    species_arg = shlex.quote(str(species))
    output_arg = shlex.quote(
        str(
            Path(gene_out.get("candidate_refs", {}).get(AUGUSTUS_METHOD, ""))
            or "augustus.gff3"
        )
    )
    script = (
        "set -e\n"
        f"augustus --species={species_arg} --gff3=on {genome_arg} > {output_arg}\n"
        f"test -f {output_arg}\n"
    )
    return _freeze_gene_method_attempt(
        state,
        method_name=AUGUSTUS_METHOD,
        cfg=GENE_EXECUTABLE_METHODS[AUGUSTUS_METHOD],
        script_body=script,
        extra_metadata={"species": str(species), "readiness_warnings": warnings},
    )


def run_augustus_attempt(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    if gene_out.get("status") != "augustus_frozen":
        return {}
    return _run_gene_method_attempt(
        state,
        method_name=AUGUSTUS_METHOD,
        cfg=GENE_EXECUTABLE_METHODS[AUGUSTUS_METHOD],
    )


def collect_augustus_artifacts(state: TopLevelState) -> dict:
    gene_out = _gene_output(state)
    if gene_out.get("status") != "augustus_ran":
        return {}
    return _collect_gene_method_artifacts(
        state,
        method_name=AUGUSTUS_METHOD,
        cfg=GENE_EXECUTABLE_METHODS[AUGUSTUS_METHOD],
    )


# --- EVM merge + BUSCO execution (Phase 7 task 05) ----------------------------

EVM_METHOD = "gene_annotation.merge.evm"
GENE_BUSCO_METHOD = "gene_annotation.busco"
FINAL_GFF_TYPE = "gene_annotation.final_gff"
BUSCO_SUMMARY_TYPE = "gene_annotation.busco_summary"


def freeze_evm_attempt(state: TopLevelState) -> dict:
    """Freeze EVM merge or short-circuit single-candidate passthrough."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "merge_accepted":
        return {}

    candidate_refs = dict(
        gene_out.get("accepted_candidate_refs") or gene_out.get("candidate_refs") or {}
    )
    merge_weights = dict(gene_out.get("merge_weights") or _DEFAULT_MERGE_WEIGHTS)
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]

    # Single-candidate passthrough: no EVM script needed. Reuse the candidate's
    # already-materialized proteins so the final gene set has a real proteins.fa.
    if len(candidate_refs) <= 1:
        final_ref = list(candidate_refs.values())[0] if candidate_refs else None
        bundles = gene_out.get("candidate_bundles") or {}
        final_method = next(iter(candidate_refs), None) if candidate_refs else None
        bundle = bundles.get(final_method) if final_method else None
        update = {
            "status": "evm_collected",
            "merge_method": "single_candidate_passthrough",
            "final_gff_ref": final_ref,
            "merge_weights": merge_weights,
        }
        if bundle:
            update["final_proteins_ref"] = bundle.get("proteins_fasta_ref")
            update["final_cds_ref"] = bundle.get("cds_fasta_ref")
        return _update_gene_output(state, update)

    # Multi-candidate: write EVM weights file and script.
    genome_ref = gene_out.get("genome_input_ref")
    if not genome_ref:
        return _fail_update(state, "evm requires genome_input_ref")

    working_dir = stage_work_dir(state, "gene_annotation", "evm")
    working_dir.mkdir(parents=True, exist_ok=True)

    weights_path = working_dir / "weights.txt"
    weight_lines = []
    evm_inputs: dict[str, list[str]] = {
        "gene_predictions": [],
        "protein_alignments": [],
        "transcript_alignments": [],
    }
    # EVM weights format: EVIDENCE_CLASS<TAB>SOURCE<TAB>WEIGHT. Candidate files
    # are supplied through the evidence-class-specific command arguments below.
    for method_name, method_ref in sorted(candidate_refs.items()):
        evm_class = _method_evm_class(method_name)
        weight_key = _EVM_CLASS_WEIGHT_KEY.get(evm_class, "ab_initio")
        weight = merge_weights.get(
            weight_key, _DEFAULT_MERGE_WEIGHTS.get(weight_key, 1)
        )
        source_type = _evm_source_type(gene_out, method_name)
        weight_lines.append(f"{evm_class}\t{source_type}\t{weight}")
        evm_inputs[_evm_argument_bucket(evm_class)].append(method_ref)
    weights_path.write_text("\n".join(weight_lines) + "\n", encoding="utf-8")

    output_gff3 = working_dir / "evm_merged.gff3"
    script_path = working_dir / "run_evm.sh"
    evm_args = [
        f"--genome {shlex.quote(str(genome_ref))}",
        f"--weights {shlex.quote(str(weights_path))}",
    ]
    for arg_name in ("gene_predictions", "protein_alignments", "transcript_alignments"):
        refs = evm_inputs[arg_name]
        if refs:
            evm_args.append(f"--{arg_name} {shlex.quote(','.join(refs))}")
    script_path.write_text(
        "set -e\n"
        f"evidence_modeler.pl {' '.join(evm_args)} > {shlex.quote(str(output_gff3))}\n"
        f"test -f {shlex.quote(str(output_gff3))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    env_spec, _packages, blocked = resolve_method_env(
        gene_out,
        stage="gene_annotation",
        method_name=EVM_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_gene_output(state, blocked)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:gene_annotation:evm",
        session_id=session_id,
        stage="gene_annotation",
        method_name=EVM_METHOD,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[output_gff3],
        env=gene_out.get("env"),
        env_spec=env_spec,
        metadata={
            "genome_input_ref": str(genome_ref),
            "candidate_refs": candidate_refs,
            "evm_inputs": evm_inputs,
            "merge_weights": merge_weights,
            "output_gff3": str(output_gff3),
        },
    )
    attempt_ref = persist_gene_attempt(state, attempt)

    return _update_gene_output(
        state,
        {
            "status": "evm_frozen",
            "evm_attempt_ref": attempt_ref.model_dump(mode="json"),
        },
    )


def run_evm_attempt(state: TopLevelState) -> dict:
    """Run the frozen EVM attempt locally."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "evm_frozen":
        return {}

    attempt = load_gene_attempt(
        state,
        gene_out["evm_attempt_ref"],
        expected_method_name=EVM_METHOD,
    )
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label="evm",
    )
    execution_ref = persist_gene_execution(
        state,
        method_name=attempt.method_name,
        result=outcome.result,
    )
    if not outcome.success:
        return _update_gene_output(
            state,
            {
                "status": "failed",
                "reason": outcome.error,
                "evm_execution_ref": execution_ref.model_dump(mode="json"),
            },
        )

    return _update_gene_output(
        state,
        {
            "status": "evm_ran",
            "evm_execution_ref": execution_ref.model_dump(mode="json"),
        },
    )


def collect_evm_artifacts(state: TopLevelState) -> dict:
    """Collect EVM merged GFF3 and register as final_gff artifact."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "evm_ran":
        return {}

    attempt = load_gene_attempt(
        state,
        gene_out["evm_attempt_ref"],
        expected_method_name=EVM_METHOD,
    )
    load_gene_execution(
        state,
        gene_out["evm_execution_ref"],
        expected_attempt_id=attempt.attempt_id,
        expected_method_name=EVM_METHOD,
    )
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    output_gff3 = Path(str(attempt.metadata["output_gff3"]))

    try:
        published = publish_or_recover_work_artifact(
            data_dir,
            session_id,
            output_gff3,
            stage="gene_annotation",
            method_name=EVM_METHOD,
            artifact_type=FINAL_GFF_TYPE,
            metadata={"attempt_id": attempt.attempt_id},
        )
    except FileNotFoundError:
        reason = f"expected EVM merged GFF3 missing: {output_gff3}"
        Ledger(data_dir).record_attempt_failed(
            attempt.session_id, attempt, return_code=0, error=reason
        )
        return _update_gene_output(state, {"status": "failed", "reason": reason})
    published_gff3 = published.path
    Ledger(data_dir).record_attempt_succeeded(
        attempt.session_id,
        attempt,
        artifacts=[published.ref.model_dump(mode="json")],
    )

    # Materialize proteins from the merged GFF so the final gene set carries a
    # real proteins.fa before BUSCO and commit.
    proteins_fa, cds_fa, mat_error = _materialize_candidate_proteins(
        state,
        method_name=EVM_METHOD,
        suffix="evm",
        output_gff3=published_gff3,
    )
    if mat_error:
        Ledger(data_dir).record_attempt_failed(
            attempt.session_id, attempt, return_code=0, error=mat_error
        )
        return _update_gene_output(
            state, {"status": "materialization_failed", "reason": mat_error}
        )

    return _update_gene_output(
        state,
        {
            "status": "evm_collected",
            "merge_method": "evm",
            "final_gff_ref": str(published_gff3),
            "final_gff_artifact_ref": published.ref.model_dump(mode="json"),
            "final_proteins_ref": str(proteins_fa),
            "final_cds_ref": str(cds_fa) if cds_fa else None,
        },
    )


def freeze_gene_busco_attempt(state: TopLevelState) -> dict:
    """Freeze a BUSCO attempt on the final gene set (proteins preferred)."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "evm_collected":
        return {}

    final_gff_ref, final_proteins_ref, final_cds_ref, final_error = (
        _resolve_final_gene_set(gene_out)
    )
    if not final_gff_ref:
        return _update_gene_output(
            state,
            {
                "status": "merge_accepted",
                "busco_completeness_status": "missing",
                "busco_note": "no final_gff_ref for BUSCO",
            },
        )
    if final_error:
        return _update_gene_output(
            state,
            {
                "status": "merge_accepted",
                "busco_completeness_status": "missing",
                "busco_note": final_error,
            },
        )

    transcript_ref = final_cds_ref or gene_out.get("transcripts_fasta_ref")

    if final_proteins_ref and Path(str(final_proteins_ref)).exists():
        busco_input = str(final_proteins_ref)
        busco_mode = "proteins"
    elif transcript_ref and Path(str(transcript_ref)).exists():
        busco_input = str(transcript_ref)
        busco_mode = "transcriptome"
    else:
        return _update_gene_output(
            state,
            {
                "status": "merge_accepted",
                "busco_completeness_status": "missing",
                "busco_note": "no protein or transcript sequences for BUSCO",
            },
        )

    hints = gene_out.get("lineage_or_species_hints") or {}
    busco_lineage = hints.get("busco_lineage")
    if not busco_lineage:
        return _update_gene_output(
            state,
            {
                "status": "merge_accepted",
                "busco_completeness_status": "missing",
                "busco_note": "no busco_lineage in lineage_or_species_hints",
            },
        )

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]

    env_spec, _packages, blocked = resolve_method_env(
        gene_out,
        stage="gene_annotation",
        method_name=GENE_BUSCO_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_gene_output(state, blocked)

    working_dir = stage_work_dir(state, "gene_annotation", "busco")
    working_dir.mkdir(parents=True, exist_ok=True)

    out_name = "busco_gene"
    lineage_name = Path(busco_lineage).name
    short_summary = (
        working_dir / out_name / f"short_summary.specific.{lineage_name}.{out_name}.txt"
    )
    script_path = working_dir / "run_busco.sh"
    script_path.write_text(
        "set -e\n"
        f"busco -i {shlex.quote(busco_input)}"
        f" -o {shlex.quote(out_name)}"
        f" -m {shlex.quote(busco_mode)}"
        f" -l {shlex.quote(str(busco_lineage))}"
        f" --offline --cpu 4\n"
        f"test -f {shlex.quote(str(short_summary))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:gene_annotation:busco",
        session_id=session_id,
        stage="gene_annotation",
        method_name=GENE_BUSCO_METHOD,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[short_summary],
        env=gene_out.get("env"),
        env_spec=env_spec,
        metadata={
            "busco_input": busco_input,
            "busco_mode": busco_mode,
            "busco_lineage": str(busco_lineage),
            "busco_output_dir": str(working_dir / out_name),
            "short_summary_ref": str(short_summary),
        },
    )
    attempt_ref = persist_gene_attempt(state, attempt)

    return _update_gene_output(
        state,
        {
            "status": "busco_frozen",
            "busco_attempt_ref": attempt_ref.model_dump(mode="json"),
        },
    )


def run_gene_busco_attempt(state: TopLevelState) -> dict:
    """Run the frozen BUSCO attempt locally."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "busco_frozen":
        return {}

    attempt = load_gene_attempt(
        state,
        gene_out["busco_attempt_ref"],
        expected_method_name=GENE_BUSCO_METHOD,
    )
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label="busco",
        cleanup_outputs=False,
    )
    execution_ref = persist_gene_execution(
        state,
        method_name=attempt.method_name,
        result=outcome.result,
    )
    if not outcome.success:
        return _update_gene_output(
            state,
            {
                "status": "merge_accepted",
                "busco_completeness_status": "missing",
                "busco_note": f"busco failed: {outcome.error}",
                "busco_execution_ref": execution_ref.model_dump(mode="json"),
            },
        )

    return _update_gene_output(
        state,
        {
            "status": "busco_ran",
            "busco_execution_ref": execution_ref.model_dump(mode="json"),
        },
    )


def collect_gene_busco_artifacts(state: TopLevelState) -> dict:
    """Collect BUSCO short summary and write completeness signal into gene_output."""
    gene_out = _gene_output(state)
    if gene_out.get("status") != "busco_ran":
        return {}

    attempt = load_gene_attempt(
        state,
        gene_out["busco_attempt_ref"],
        expected_method_name=GENE_BUSCO_METHOD,
    )
    load_gene_execution(
        state,
        gene_out["busco_execution_ref"],
        expected_attempt_id=attempt.attempt_id,
        expected_method_name=GENE_BUSCO_METHOD,
    )
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    short_summary = Path(str(attempt.metadata["short_summary_ref"]))

    try:
        published = publish_or_recover_work_artifact(
            data_dir,
            session_id,
            short_summary,
            stage="gene_annotation",
            method_name=GENE_BUSCO_METHOD,
            artifact_type=BUSCO_SUMMARY_TYPE,
            metadata={"attempt_id": attempt.attempt_id},
        )
    except FileNotFoundError:
        Ledger(data_dir).record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=0,
            error=f"BUSCO short summary missing: {short_summary}",
        )
        return _update_gene_output(
            state,
            {
                "status": "merge_accepted",
                "busco_completeness_status": "missing",
                "busco_note": "busco short_summary.txt not found",
            },
        )

    metrics = _parse_busco_short_summary(published.path)
    busco_score = metrics.get("complete_percent")
    Ledger(data_dir).record_attempt_succeeded(
        attempt.session_id,
        attempt,
        artifacts=[published.ref.model_dump(mode="json")],
    )

    return _update_gene_output(
        state,
        {
            "status": "merge_accepted",
            "final_gff_ref": gene_out.get("final_gff_ref"),
            "busco_completeness": busco_score,
            "busco_completeness_status": "available"
            if busco_score is not None
            else "missing",
            "busco_metrics": metrics,
            "busco_summary_ref": str(published.path),
            "busco_summary_artifact_ref": published.ref.model_dump(mode="json"),
        },
    )


def _parse_busco_short_summary(path: Path) -> dict[str, Any]:
    """Parse a BUSCO short_summary.txt into a metrics dict."""
    metrics: dict[str, Any] = {}
    key_map = {
        "complete buscos (c)": "complete_buscos",
        "complete and single-copy buscos (s)": "single_copy_buscos",
        "complete and duplicated buscos (d)": "duplicated_buscos",
        "fragmented buscos (f)": "fragmented_buscos",
        "missing buscos (m)": "missing_buscos",
        "total busco groups searched": "total_busco_groups_searched",
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("C:"):
            metrics["summary_line"] = stripped
            for key, value in re.findall(r"([CSDFM]):([0-9.]+)%", stripped):
                metrics[
                    {
                        "C": "complete_percent",
                        "S": "single_copy_percent",
                        "D": "duplicated_percent",
                        "F": "fragmented_percent",
                        "M": "missing_percent",
                    }[key]
                ] = float(value)
            continue
        match = re.match(r"^(\d+)\s+(.+)$", stripped)
        if not match:
            continue
        out_key = key_map.get(match.group(2).strip().lower())
        if out_key:
            metrics[out_key] = int(match.group(1))
    return metrics


def _softmasked_genome_ref(state: TopLevelState) -> str | None:
    repeat_out = (state.get("stage_outputs") or {}).get("repeat_annotation") or {}
    for key in ["softmasked_genome_ref", "softmasked_genome"]:
        value = repeat_out.get(key)
        if value:
            return str(value)
    return None


def _repeat_annotation_ref(state: TopLevelState) -> str | None:
    repeat_out = (state.get("stage_outputs") or {}).get("repeat_annotation") or {}
    for key in ["repeat_annotation_gff3_ref", "repeat_annotation"]:
        value = repeat_out.get(key)
        if value:
            return str(value)
    return None


def _explicit_unmasked_fallback_reason(state: TopLevelState) -> str | None:
    gene_out = _gene_output(state)
    if gene_out.get("unmasked_fallback_reason"):
        return str(gene_out["unmasked_fallback_reason"])
    repeat_out = (state.get("stage_outputs") or {}).get("repeat_annotation") or {}
    if repeat_out.get("unmasked_fallback_reason"):
        return str(repeat_out["unmasked_fallback_reason"])
    skip_reason = (state.get("skip_reasons") or {}).get("repeat_annotation")
    return str(skip_reason) if skip_reason else None


def _registry_assets(
    state: TopLevelState, asset_types: set[str]
) -> list[dict[str, Any]]:
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
        if asset.get("asset_type") in asset_types and asset.get("path")
    ]


def _asset_paths(state: TopLevelState, asset_types: set[str]) -> list[str]:
    return [str(asset.get("path")) for asset in _registry_assets(state, asset_types)]


def _first_asset_path(state: TopLevelState, asset_type: str) -> str | None:
    assets = _registry_assets(state, {asset_type})
    if len(assets) > 1:
        raise ValueError(
            f"adoption asset {asset_type!r} has {len(assets)} registry entries; manifest validation should have rejected this"
        )
    return str(assets[0]["path"]) if assets else None


def _provided_gene_annotation_refs(state: TopLevelState) -> dict[str, str | None]:
    return {
        "gene_models_ref": _first_asset_path(state, "gene_models"),
        "predicted_proteins_ref": _first_asset_path(state, "predicted_proteins"),
        "transcripts_fasta_ref": _first_asset_path(state, "transcripts_fasta"),
    }


def _provided_annotation_review_triggers(gene_out: dict[str, Any]) -> list[str]:
    triggers: list[str] = []
    if gene_out.get("genome_input_source") == "final_assembly_for_annotation":
        triggers.append("unmasked_genome_fallback_requires_review")
    if (
        gene_out.get("rnaseq_read_refs")
        or gene_out.get("long_rna_read_refs")
        or gene_out.get("protein_evidence_refs")
    ):
        triggers.append("provided_annotation_with_additional_evidence_review")
    return triggers


def _lineage_or_species_hints(state: TopLevelState) -> dict[str, Any]:
    gene_out = _gene_output(state)
    hints = dict(gene_out.get("lineage_or_species_hints") or {})
    inventory_out = (state.get("stage_outputs") or {}).get("inventory") or {}
    for key in ["species", "lineage", "busco_lineage", "augustus_species_model"]:
        if inventory_out.get(key) and key not in hints:
            hints[key] = inventory_out[key]
    return hints


def _candidate_methods(profile: str) -> list[str]:
    if profile == "no_evidence":
        return ["gene_annotation.candidate.augustus"]
    if profile == "protein":
        return ["gene_annotation.candidate.miniprot"]
    if profile in {"rnaseq", "long_rna"}:
        return ["gene_annotation.candidate.augustus"]
    return GENE_CANDIDATE_METHODS


def _helper_methods(available_evidence: list[str]) -> list[str]:
    helpers: list[str] = []
    if "rnaseq" in available_evidence:
        helpers.extend(RNA_HELPER_METHODS)
    if "long_rna" in available_evidence:
        helpers.extend(LONG_RNA_HELPER_METHODS)
    if "protein" in available_evidence:
        helpers.extend(PROTEIN_HELPER_METHODS)
    return helpers


def _review_triggers(gene_out: dict[str, Any]) -> list[str]:
    triggers: list[str] = []
    if gene_out.get("evidence_profile") == "mixed_evidence":
        triggers.append("evidence_merge_policy_required")
    available = set(gene_out.get("available_evidence", []))
    if "protein" in available and ({"rnaseq", "long_rna"} & available):
        triggers.append("rna_protein_evidence_conflict_review")
    if gene_out.get("genome_input_source") == "final_assembly_for_annotation":
        triggers.append("unmasked_genome_fallback_requires_review")
    if gene_out.get("evidence_profile") == "no_evidence":
        triggers.append("species_or_training_policy_required")
    return triggers
