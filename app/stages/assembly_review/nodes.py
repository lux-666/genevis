"""Assembly review stage nodes.

Assembly review is the main human gate.  It does not invent thresholds.
It collects or references evaluation signals and routes to:
- deterministic safe path (existing assembly input only)
- human review gate (multiple candidates or insufficient signals)
- failure (no assembly available)
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError, UnsupportedLifecycleOperationError
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import attach_conda_fields, resolve_method_env
from app.execution.local_runner import LocalRunner
from app.review.base import ResumeReviewRequest, ReviewPayload, ReviewSignal
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_ACTION,
    ASSEMBLY_REVIEW_EVAL_BUNDLE,
    ASSEMBLY_REVIEW_QUAST_REPORT_TSV,
    ASSEMBLY_REVIEW_QUAST_REPORT_TXT,
    ArtifactMeta,
    AssemblyEvalBundle,
    AssemblyEvalSignal,
    AssemblyReviewActionArtifact,
    ReviewDecisionArtifact,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
from app.schemas.plan import AssemblyReviewPlan
from app.schemas.state import TopLevelState

ASSEMBLY_REVIEW_CONTRACT_METHOD = "assembly_review.contract"
QUAST_METHOD = "assembly_review.eval.quast"


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


def freeze_quast_attempts(state: TopLevelState) -> dict:
    """Freeze local QUAST attempts for materialized assembly candidates."""
    stage_outputs = state.get("stage_outputs") or {}
    ar_out = stage_outputs.get("assembly_review", {})
    if ar_out.get("status") == "failed" or not _local_quast_enabled(ar_out):
        return {}

    descriptor_error = _validate_review_methods([QUAST_METHOD])
    if descriptor_error:
        return _update_assembly_review_output(state, {"status": "failed", "reason": descriptor_error})

    candidates = ar_out.get("candidates", [])
    materialized = _materialized_quast_candidates(candidates)
    signals = _mark_missing_quast_signals(
        ar_out.get("signals", []),
        candidates,
        {candidate.get("candidate_id") for candidate in materialized},
    )
    if not materialized:
        return _rewrite_eval_bundle(state, {**ar_out, "signals": signals}, status="quast_skipped")

    threads, error = _quast_threads(ar_out)
    if error:
        return _update_assembly_review_output(state, {"status": "failed", "reason": error})

    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    env_spec, blocked = resolve_method_env(
        ar_out,
        stage="assembly_review",
        method_name=QUAST_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_assembly_review_output(state, blocked)

    attempts = []
    for candidate in materialized:
        candidate_id = candidate.get("candidate_id", "unknown")
        safe_id = _safe_candidate_id(candidate_id)
        working_dir = data_dir / "work" / "assembly_review" / session_id / "quast" / safe_id
        artifact_dir = data_dir / "artifacts" / "assembly_review" / session_id / "quast" / safe_id
        working_dir.mkdir(parents=True, exist_ok=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        script_path = working_dir / "run_quast.sh"
        out_dir = artifact_dir / "quast_out"
        report_tsv = out_dir / "report.tsv"
        assembly_ref = Path(candidate["final_assembly_ref"])
        script_path.write_text(
            "set -e\n"
            f"mkdir -p {shlex.quote(str(out_dir))}\n"
            f"quast.py {shlex.quote(str(assembly_ref))} -o {shlex.quote(str(out_dir))} --threads {threads}\n"
            f"test -f {shlex.quote(str(report_tsv))}\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        attempt_kwargs: dict[str, Any] = {
            "attempt_id": f"{session_id}:assembly_review:quast:{safe_id}",
            "session_id": session_id,
            "stage": "assembly_review",
            "method_name": QUAST_METHOD,
            "script_path": script_path,
            "working_dir": working_dir,
            "interpreter": "/bin/bash",
            "expected_outputs": [report_tsv],
            "env": {str(k): str(v) for k, v in (ar_out.get("env") or {}).items()},
            "metadata": {
                "candidate_id": candidate_id,
                "draft_assembly_ref": str(assembly_ref),
                "quast_output_dir": str(out_dir),
                "report_tsv": str(report_tsv),
                "threads": threads,
            },
        }
        attach_conda_fields(env_spec, attempt_kwargs)
        attempt = FrozenAttemptSpec(**attempt_kwargs)
        attempts.append(attempt.model_dump(mode="json"))

    return _update_assembly_review_output(
        state,
        {
            "status": "quast_frozen",
            "signals": signals,
            "quast_frozen_attempts": attempts,
        },
    )


def run_quast_attempts(state: TopLevelState) -> dict:
    """Run frozen QUAST attempts locally."""
    ar_out = (state.get("stage_outputs") or {}).get("assembly_review", {})
    if ar_out.get("status") != "quast_frozen":
        return {}

    ledger = Ledger(state.get("data_dir", "data"))
    results = []
    for payload in ar_out.get("quast_frozen_attempts", []):
        attempt = FrozenAttemptSpec.model_validate(payload)
        ledger.create_session(attempt.session_id, state.get("input_manifest_ref", ""))
        ledger.record_attempt_started(attempt.session_id, attempt)
        result = LocalRunner().run(attempt)
        result_payload = result.model_dump(mode="json")
        if not result.success:
            stderr = result.stderr_path.read_text(encoding="utf-8") if result.stderr_path.exists() else ""
            error = stderr.strip() or f"QUAST failed with return code {result.return_code}"
            ledger.record_attempt_failed(
                attempt.session_id,
                attempt,
                return_code=result.return_code,
                error=error,
            )
            result_payload["error"] = error
        results.append(result_payload)

    return _update_assembly_review_output(state, {"status": "quast_ran", "quast_execution_results": results})


def collect_quast_artifacts(state: TopLevelState) -> dict:
    """Collect QUAST artifacts and update review signals before rule evaluation."""
    ar_out = (state.get("stage_outputs") or {}).get("assembly_review", {})
    if ar_out.get("status") != "quast_ran":
        return {}

    attempts = {
        payload["attempt_id"]: FrozenAttemptSpec.model_validate(payload)
        for payload in ar_out.get("quast_frozen_attempts", [])
    }
    signals = list(ar_out.get("signals") or [])
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    artifact_refs = list(ar_out.get("artifact_refs") or [])
    ledger = Ledger(data_dir)

    for result in ar_out.get("quast_execution_results", []):
        attempt = attempts[result["attempt_id"]]
        candidate_id = str(attempt.metadata.get("candidate_id"))
        artifacts: list[ArtifactMeta] = []
        metric_refs: list[str] = []
        notes = ["thresholds_not_applied_without_human_approval"]
        metadata = {
            "attempt_id": attempt.attempt_id,
            "draft_assembly_ref": attempt.metadata.get("draft_assembly_ref"),
            "quast_output_dir": attempt.metadata.get("quast_output_dir"),
        }
        if result.get("success"):
            report_tsv = Path(attempt.metadata["report_tsv"])
            artifacts.append(
                ArtifactMeta.from_file(
                    report_tsv,
                    stage="assembly_review",
                    method_name=QUAST_METHOD,
                    artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TSV,
                    metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id},
                )
            )
            metric_refs.append(str(report_tsv))
            report_txt = Path(attempt.metadata["quast_output_dir"]) / "report.txt"
            if report_txt.exists():
                artifacts.append(
                    ArtifactMeta.from_file(
                        report_txt,
                        stage="assembly_review",
                        method_name=QUAST_METHOD,
                        artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TXT,
                        metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id},
                    )
                )
                metric_refs.append(str(report_txt))
            for path, artifact_type in [
                (Path(result["stdout_path"]), "command.stdout"),
                (Path(result["stderr_path"]), "command.stderr"),
            ]:
                if path.exists():
                    artifacts.append(
                        ArtifactMeta.from_file(
                            path,
                            stage="assembly_review",
                            method_name=QUAST_METHOD,
                            artifact_type=artifact_type,
                            metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id},
                        )
                    )
            append_artifact_records(data_dir, session_id, artifacts)
            artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
            ledger.record_attempt_succeeded(session_id, attempt, artifacts=artifact_payloads)
            for artifact in artifact_payloads:
                ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)
            artifact_refs.extend(str(artifact.file_path) for artifact in artifacts)
            metadata["metrics"] = _parse_quast_report_tsv(report_tsv)
            notes.append("quast_executed_locally")
            signals = _update_quast_signal(signals, candidate_id, "available", metric_refs, notes, metadata)
        else:
            for path, artifact_type in [
                (Path(result["stdout_path"]), "command.stdout"),
                (Path(result["stderr_path"]), "command.stderr"),
            ]:
                if path.exists():
                    artifacts.append(
                        ArtifactMeta.from_file(
                            path,
                            stage="assembly_review",
                            method_name=QUAST_METHOD,
                            artifact_type=artifact_type,
                            metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id},
                        )
                    )
            if artifacts:
                append_artifact_records(data_dir, session_id, artifacts)
                artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
                for artifact in artifact_payloads:
                    ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)
                artifact_refs.extend(str(artifact.file_path) for artifact in artifacts)
            failed_refs = [str(artifact.file_path) for artifact in artifacts]
            metadata["return_code"] = result.get("return_code")
            metadata["error"] = result.get("error")
            signals = _update_quast_signal(
                signals,
                candidate_id,
                "failed",
                failed_refs,
                ["quast_local_execution_failed", "candidate_not_rejected_from_metrics"],
                metadata,
            )

    ar_out = {**ar_out, "signals": signals, "artifact_refs": _unique_refs(artifact_refs)}
    update = _rewrite_eval_bundle(state, ar_out, status="evaluating")
    outputs = update["stage_outputs"]
    outputs["assembly_review"] = {
        **outputs["assembly_review"],
        "quast_frozen_attempts": [],
        "quast_execution_results": [],
    }
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
            "pass",
            "accept",
            "purge",
            "polish",
            "continue_low_confidence",
            "stop_human_review",
            "rerun",
        ],
        notes=[
            "Thresholds for BUSCO, QUAST, Merqury, and purge_dups require human approval before application.",
            "Purge and polish actions require explicit action output refs, human approval, and provenance; pre-action assemblies are preserved.",
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
    interrupt = payload.model_dump(mode="json")
    interrupt["blocked_at"] = datetime.now(timezone.utc).isoformat()
    interrupt["review_request_artifact_ref"] = request_ref
    return {
        "active_interrupt": interrupt,
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

    deterministic_safe = ar_out.get("deterministic_safe", False)
    if deterministic_safe and candidates:
        decision = "accept"
        selected_candidate_id = candidates[0]["candidate_id"]
        rejected_candidates: list[str] = []
        final_assembly_ref = candidates[0].get("final_assembly_ref")
        pre_action_ref = final_assembly_ref
        action_type = "pass"
        action_execution_mode = "none"
        method_name = candidates[0].get("method_name")
        evidence_refs: list[str] = []
        evidence_notes: list[str] = []
    else:
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
    trace = [*state.get("route_trace", []), "assembly_review_stage"]
    next_stage = next_stage_name("assembly_review")
    action_metadata = {
        "action_type": action_type,
        "action_execution_mode": action_execution_mode,
        "pre_action_assembly_ref": pre_action_ref,
        "post_action_assembly_ref": final_assembly_ref,
        "final_assembly_for_scaffolding_or_annotation": final_for_downstream,
        "method_name": method_name,
        "evidence_refs": evidence_refs,
        "evidence_notes": evidence_notes,
    }

    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    ledger.record_review_decision(
        state["session_id"],
        "assembly_review",
        decision=decision,
        candidate_id=selected_candidate_id,
        rejected_candidates=rejected_candidates,
        notes=ar_out.get("notes", []),
        metadata=action_metadata,
    )

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
    decision_ref = _write_json_artifact(
        state,
        "assembly_review_decision.json",
        decision_artifact.model_dump(mode="json"),
        artifact_type="assembly_review.decision",
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
            metadata={"review_decision_artifact_ref": decision_ref},
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
            "review_decision_artifact_ref": decision_ref,
            "assembly_review_action_artifact_ref": action_ref,
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
    candidates = ar_out.get("candidates") or []
    candidates_by_id = {candidate.get("candidate_id"): candidate for candidate in candidates}
    allowed = {"accept", "pass", "purge", "polish", "continue_low_confidence", "stop_human_review"}
    if request.decision == "rerun":
        raise UnsupportedLifecycleOperationError("rerun is not implemented by review resume")
    if request.decision not in allowed:
        raise ResumeValidationError(f"unsupported assembly_review decision: {request.decision}")

    if request.decision == "stop_human_review":
        return _stop_assembly_review(state, request)

    if not request.candidate_id:
        raise ResumeValidationError("assembly_review resume requires candidate_id")
    if request.candidate_id not in candidates_by_id:
        raise ResumeValidationError(f"unknown assembly_review candidate_id: {request.candidate_id}")

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
    ledger = Ledger(state.get("data_dir", "data"))
    ledger.record_review_decision(
        state["session_id"],
        "assembly_review",
        decision="stop_human_review",
        candidate_id=request.candidate_id,
        rejected_candidates=request.rejected_candidates,
        notes=request.notes,
        metadata=request.metadata,
    )
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
    decision_ref = _write_json_artifact(
        state,
        "assembly_review_decision.json",
        decision_artifact.model_dump(mode="json"),
        artifact_type="assembly_review.decision",
    )
    stage_outputs["assembly_review"] = {
        **ar_out,
        "status": "failed_terminal",
        "decision": "stop_human_review",
        "review_decision_artifact_ref": decision_ref,
        "notes": request.notes,
    }
    return {
        "current_stage": "assembly_review",
        "supervision_state": "failed_terminal",
        "active_interrupt": None,
        "stage_outputs": stage_outputs,
        "last_error": "assembly_review stopped by human review decision",
    }


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


def _update_assembly_review_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["assembly_review"] = {**dict(outputs.get("assembly_review") or {}), **payload}
    return {"stage_outputs": outputs}


def _local_quast_enabled(ar_out: dict[str, Any]) -> bool:
    return ar_out.get("execution_mode") == "local"


def _validate_review_methods(methods: list[str]) -> str | None:
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    missing = [method for method in methods if method not in registry.methods]
    if missing:
        return f"assembly_review descriptors missing: {missing}"
    return None


def _materialized_quast_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    materialized = []
    for candidate in candidates:
        final_ref = candidate.get("final_assembly_ref")
        if final_ref and Path(final_ref).exists():
            materialized.append(candidate)
    return materialized


def _safe_candidate_id(candidate_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id).strip("_") or "candidate"


def _quast_threads(ar_out: dict[str, Any]) -> tuple[int, str | None]:
    params = dict(ar_out.get("quast_parameters") or {})
    raw_threads = params.get("threads", 1)
    try:
        threads = int(raw_threads)
    except (TypeError, ValueError):
        return 0, "assembly_review.quast_parameters.threads must be a positive integer"
    if threads <= 0:
        return 0, "assembly_review.quast_parameters.threads must be a positive integer"
    return threads, None


def _mark_missing_quast_signals(
    signals: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    materialized_ids: set[Any],
) -> list[dict[str, Any]]:
    updated = list(signals)
    for candidate in candidates:
        candidate_id = candidate.get("candidate_id")
        if candidate_id in materialized_ids:
            continue
        updated = _update_quast_signal(
            updated,
            str(candidate_id),
            "missing",
            [],
            ["quast_requires_materialized_draft_assembly", "execution_skipped"],
            {"draft_assembly_ref": candidate.get("final_assembly_ref")},
        )
    return updated


def _update_quast_signal(
    signals: list[dict[str, Any]],
    candidate_id: str,
    status: str,
    metric_refs: list[str],
    notes: list[str],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    updated = []
    matched = False
    for signal in signals:
        if signal.get("signal_name") == "quast" and signal.get("candidate_id") in {None, candidate_id}:
            payload = {**signal, "candidate_id": candidate_id, "status": status, "metric_refs": metric_refs, "notes": notes, "metadata": metadata}
            updated.append(payload)
            matched = True
        else:
            updated.append(signal)
    if not matched:
        updated.append(
            ReviewSignal(
                signal_name="quast",
                method_name=QUAST_METHOD,
                candidate_id=candidate_id,
                status=status,  # type: ignore[arg-type]
                metric_refs=metric_refs,
                notes=notes,
                metadata=metadata,
            ).model_dump(mode="json")
        )
    return updated


def _rewrite_eval_bundle(state: TopLevelState, ar_out: dict[str, Any], *, status: str) -> dict:
    previous_bundle = _read_eval_bundle(ar_out.get("eval_bundle_ref"))
    signal_payloads = _candidate_eval_signals(previous_bundle, ar_out.get("signals", []))
    eval_signals = [AssemblyEvalSignal.model_validate(signal) for signal in signal_payloads]
    review_plan_data = dict(ar_out.get("review_plan") or {})
    review_plan_data["status"] = status
    review_plan_data["available_signals"] = sorted(
        {signal.signal_name for signal in eval_signals if signal.status == "available"}
    )
    review_plan_data["missing_signals"] = sorted(
        {signal.signal_name for signal in eval_signals if signal.status in {"missing", "failed"}}
    )
    eval_bundle = AssemblyEvalBundle(
        session_id=state["session_id"],
        candidate_ids=[candidate.get("candidate_id", "unknown") for candidate in ar_out.get("candidates", [])],
        signals=eval_signals,
        available_inputs=previous_bundle.get("available_inputs", []),
        missing_inputs=previous_bundle.get("missing_inputs", []),
        notes=["thresholds_not_applied_without_human_approval"],
    )
    filename = f"assembly_eval_bundle.{status}.json"
    eval_bundle_ref = _write_json_artifact(
        state,
        filename,
        eval_bundle.model_dump(mode="json"),
        artifact_type=ASSEMBLY_REVIEW_EVAL_BUNDLE,
    )
    return _update_assembly_review_output(
        state,
        {
            **ar_out,
            "status": status,
            "signals": [signal.model_dump(mode="json") for signal in eval_signals],
            "review_plan": review_plan_data,
            "eval_bundle_ref": eval_bundle_ref,
        },
    )


def _read_eval_bundle(ref: Any) -> dict[str, Any]:
    if ref and Path(str(ref)).exists():
        return json.loads(Path(str(ref)).read_text(encoding="utf-8"))
    return {}


def _candidate_eval_signals(previous_bundle: dict[str, Any], review_signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    updated_by_key = {
        (signal.get("signal_name"), signal.get("candidate_id")): signal
        for signal in review_signals
        if signal.get("candidate_id") is not None
    }
    result = []
    for signal in previous_bundle.get("signals", []):
        key = (signal.get("signal_name"), signal.get("candidate_id"))
        result.append(updated_by_key.pop(key, signal))
    result.extend(updated_by_key.values())
    return result


def _parse_quast_report_tsv(path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    key_map = {
        "# contigs": "contig_count",
        "largest contig": "largest_contig",
        "total length": "total_length",
        "n50": "n50",
        "l50": "l50",
        "gc (%)": "gc_percent",
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        key = parts[0].strip().lower()
        out_key = key_map.get(key)
        if not out_key:
            continue
        raw = parts[1].strip().replace(",", "")
        try:
            metrics[out_key] = float(raw) if "." in raw else int(raw)
        except ValueError:
            metrics[f"{out_key}_raw"] = parts[1].strip()
    return metrics


def _unique_refs(refs: list[str]) -> list[str]:
    unique = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique


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
