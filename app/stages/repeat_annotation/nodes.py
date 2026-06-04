"""Repeat annotation stage nodes for planning repeat library and masking contracts."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.control.lifecycle import ResumeValidationError
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.graph.routing import next_stage_name
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.policy import evaluate
from app.stages.base import absolute_data_dir
from app.schemas.artifacts import (
    REPEAT_ANNOTATION_BUNDLE,
    REPEAT_ANNOTATION_GFF3,
    REPEAT_ANNOTATION_PLAN,
    REPEAT_HARDMASKED_GENOME,
    REPEAT_SOFTMASKED_GENOME,
    ArtifactMeta,
    RepeatAnnotationBundle,
    RepeatAnnotationPlan,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
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
    existing = _repeat_output(state)
    return _update_repeat_output(
        state,
        {
            **existing,
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

    data_dir = absolute_data_dir(state)
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

    data_dir = absolute_data_dir(state)
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
    execution_enabled = _local_repeatmasker_enabled(repeat_out)
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
        metadata={"execution_enabled": execution_enabled},
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
        artifact_type=REPEAT_ANNOTATION_PLAN,
        metadata={
            "strategy": repeat_out["strategy"],
            "repeat_library_source": repeat_out["repeat_library_source"],
            "confidence": repeat_out.get("confidence", "high"),
            "execution_enabled": execution_enabled,
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
            "execution_enabled": execution_enabled,
        },
    )


def freeze_repeatmasker_attempt(state: TopLevelState) -> dict:
    """Freeze an explicit local RepeatMasker attempt."""
    repeat_out = _repeat_output(state)
    if repeat_out.get("status") != "masking_planned" or not _local_repeatmasker_enabled(repeat_out):
        return {}

    descriptor_error = _validate_repeat_methods([REPEAT_MASKING_METHOD])
    if descriptor_error:
        return _fail_update(state, descriptor_error)

    assembly_ref = Path(str(repeat_out.get("final_assembly_for_annotation", "")))
    library_ref = Path(str(repeat_out.get("repeat_library_ref", "")))
    if not assembly_ref.exists():
        return _fail_update(state, f"RepeatMasker local execution assembly not found: {assembly_ref}")
    if not library_ref.exists():
        return _fail_update(state, f"RepeatMasker local execution repeat library not found: {library_ref}")

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, blocked = resolve_method_env(
        repeat_out,
        stage="repeat_annotation",
        method_name=REPEAT_MASKING_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_repeat_output(state, blocked)

    safe_id = _safe_path_id(session_id)
    working_dir = data_dir / "work" / "repeat_annotation" / session_id / "repeatmasker" / safe_id
    artifact_dir = data_dir / "artifacts" / "repeat_annotation" / session_id / "repeatmasker" / safe_id
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    work_assembly = working_dir / "assembly.fasta"
    softmasked_genome = artifact_dir / "softmasked_genome.fasta"
    repeat_gff3 = artifact_dir / "repeat_annotation.gff3"
    hardmasked_genome = artifact_dir / "hardmasked_genome.fasta"
    script_path = working_dir / "run_repeatmasker.sh"
    script_path.write_text(
        "set -e\n"
        f"cp {shlex.quote(str(assembly_ref))} {shlex.quote(str(work_assembly))}\n"
        f"RepeatMasker -lib {shlex.quote(str(library_ref))} -gff -xsmall {shlex.quote(str(work_assembly))}\n"
        f"test -f {shlex.quote(str(work_assembly) + '.masked')}\n"
        f"cp {shlex.quote(str(work_assembly) + '.masked')} {shlex.quote(str(softmasked_genome))}\n"
        f"if test -f {shlex.quote(str(work_assembly) + '.out.gff')}; then cp {shlex.quote(str(work_assembly) + '.out.gff')} {shlex.quote(str(repeat_gff3))}; "
        f"elif test -f {shlex.quote(str(work_assembly) + '.gff')}; then cp {shlex.quote(str(work_assembly) + '.gff')} {shlex.quote(str(repeat_gff3))}; "
        "else exit 7; fi\n"
        f"if test -f {shlex.quote(str(work_assembly) + '.hardmasked')}; then cp {shlex.quote(str(work_assembly) + '.hardmasked')} {shlex.quote(str(hardmasked_genome))}; fi\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:repeat_annotation:repeatmasker",
        session_id=session_id,
        stage="repeat_annotation",
        method_name=REPEAT_MASKING_METHOD,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[softmasked_genome, repeat_gff3],
        env=repeat_out.get("env"),
        env_spec=env_spec,
        metadata={
            "final_assembly_for_annotation": str(assembly_ref),
            "repeat_library_ref": str(library_ref),
            "softmasked_genome": str(softmasked_genome),
            "repeat_annotation_gff3": str(repeat_gff3),
            "hardmasked_genome": str(hardmasked_genome),
        },
    )
    return _update_repeat_output(
        state,
        {
            "status": "repeatmasker_frozen",
            "repeatmasker_frozen_attempt": attempt.model_dump(mode="json"),
        },
    )


def run_repeatmasker_attempt(state: TopLevelState) -> dict:
    """Run the frozen RepeatMasker attempt locally."""
    repeat_out = _repeat_output(state)
    if repeat_out.get("status") != "repeatmasker_frozen":
        return {}

    attempt = FrozenAttemptSpec.model_validate(repeat_out["repeatmasker_frozen_attempt"])
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label="RepeatMasker",
        cleanup_outputs=False,
    )
    result_payload = outcome.result.model_dump(mode="json")
    if not outcome.success:
        _register_repeatmasker_log_artifacts(state, attempt, result_payload)
        result_payload["error"] = outcome.error
        return _update_repeat_output(
            state,
            {
                "status": "failed",
                "reason": outcome.error,
                "repeatmasker_execution_result": result_payload,
            },
        )
    return _update_repeat_output(state, {"status": "repeatmasker_ran", "repeatmasker_execution_result": result_payload})


def collect_repeatmasker_artifacts(state: TopLevelState) -> dict:
    """Collect RepeatMasker artifacts before committing repeat annotation."""
    repeat_out = _repeat_output(state)
    if repeat_out.get("status") != "repeatmasker_ran":
        return {}

    attempt = FrozenAttemptSpec.model_validate(repeat_out["repeatmasker_frozen_attempt"])
    result = dict(repeat_out.get("repeatmasker_execution_result") or {})
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    artifacts: list[ArtifactMeta] = []
    for path, artifact_type in [
        (Path(attempt.metadata["softmasked_genome"]), REPEAT_SOFTMASKED_GENOME),
        (Path(attempt.metadata["repeat_annotation_gff3"]), REPEAT_ANNOTATION_GFF3),
    ]:
        artifacts.append(
            ArtifactMeta.from_file(
                path,
                stage="repeat_annotation",
                method_name=REPEAT_MASKING_METHOD,
                artifact_type=artifact_type,
                metadata={"attempt_id": attempt.attempt_id},
            )
        )
    hardmasked = Path(attempt.metadata["hardmasked_genome"])
    if hardmasked.exists():
        artifacts.append(
            ArtifactMeta.from_file(
                hardmasked,
                stage="repeat_annotation",
                method_name=REPEAT_MASKING_METHOD,
                artifact_type=REPEAT_HARDMASKED_GENOME,
                metadata={"attempt_id": attempt.attempt_id},
            )
        )
    for path, artifact_type in [
        (Path(result["stdout_path"]), "command.stdout"),
        (Path(result["stderr_path"]), "command.stderr"),
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="repeat_annotation",
                    method_name=REPEAT_MASKING_METHOD,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    append_artifact_records(data_dir, session_id, artifacts)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger = Ledger(data_dir)
    ledger.record_attempt_succeeded(session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)

    tbl_path = Path(attempt.working_dir) / "assembly.fasta.tbl"
    repeat_percentage = _parse_tbl_repeat_percentage(tbl_path)
    artifact_refs = _unique_refs([*repeat_out.get("artifact_refs", []), *[str(artifact.file_path) for artifact in artifacts]])
    notes = _unique_refs([*repeat_out.get("notes", []), "repeatmasker_executed"])
    return _update_repeat_output(
        state,
        {
            "status": "masking_executed",
            "repeat_annotation_gff3_ref": str(attempt.metadata["repeat_annotation_gff3"]),
            "softmasked_genome_ref": str(attempt.metadata["softmasked_genome"]),
            "hardmasked_genome_ref": str(attempt.metadata["hardmasked_genome"]) if hardmasked.exists() else None,
            "repeat_percentage": repeat_percentage,
            "artifact_refs": artifact_refs,
            "notes": notes,
            "execution_enabled": True,
            "repeatmasker_attempt_id": attempt.attempt_id,
            "repeatmasker_frozen_attempt": None,
            "repeatmasker_execution_result": None,
        },
    )


def commit_repeat_annotation(state: TopLevelState) -> dict:
    """Publish the repeat annotation bundle and advance to gene annotation."""
    repeat_out = _repeat_output(state)
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "repeat_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)

    bundle = RepeatAnnotationBundle(
        session_id=session_id,
        status="executed" if repeat_out.get("execution_enabled") else "planned",
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
        metadata={
            "execution_enabled": bool(repeat_out.get("execution_enabled")),
            "attempt_id": repeat_out.get("repeatmasker_attempt_id"),
        },
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
        artifact_type=REPEAT_ANNOTATION_BUNDLE,
        metadata={
            "strategy": repeat_out["strategy"],
            "repeat_library_source": repeat_out["repeat_library_source"],
            "confidence": repeat_out.get("confidence", "high"),
            "execution_enabled": bool(repeat_out.get("execution_enabled")),
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


def block_mask_review(state: TopLevelState) -> dict:
    """Gate after RepeatMasker execution for mask-completion traceability."""
    repeat_out = _repeat_output(state)
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]

    payload = ReviewPayload(
        stage="repeat_annotation",
        gate_id="mask_review",
        reason="mask_review_traceability",
        candidates=[{
            "softmasked_genome_ref": repeat_out.get("softmasked_genome_ref"),
            "repeat_percentage": repeat_out.get("repeat_percentage"),
            "library_source": repeat_out.get("repeat_library_source"),
            "steps_run": ["repeatmasker"],
            "steps_skipped": ["trf", "de_novo_library"],
        }],
        decisions_allowed=["accept", "skip"],
        notes=repeat_out.get("notes", []),
        auto_decidable=True,
    )
    out_dir = data_dir / "artifacts" / "repeat_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    request_ref = out_dir / "mask_review_request.json"
    request_ref.write_text(
        json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        request_ref,
        stage="repeat_annotation",
        method_name=REPEAT_CONTRACT_METHOD,
        artifact_type="repeat_annotation.mask_review_request",
        metadata={"auto_decidable": True},
    )
    append_artifact_records(data_dir, session_id, [artifact])

    review_policy = repeat_out.get("review_policy") or {"mode": "auto"}
    outcome = evaluate("repeat_annotation", payload, review_policy)
    pending_confirmation = outcome.action == "BLOCK_PENDING_CONFIRM"

    Ledger(data_dir).record_review_requested(
        session_id,
        "repeat_annotation",
        reason="mask_review_traceability",
        candidates=payload.candidates,
        review_request_artifact_ref=str(request_ref),
        auto_decidable=True,
        policy_mode=outcome.mode,
        pending_human_confirmation=pending_confirmation,
    )
    if outcome.action == "BLOCK_WITH_TIMEOUT":
        Ledger(data_dir).record_event(
            session_id,
            "blocked_with_timeout",
            {
                "stage": "repeat_annotation",
                "gate_id": "mask_review",
                "max_blocked_seconds": outcome.max_blocked_seconds,
                "review_request_artifact_ref": str(request_ref),
                "policy_mode": outcome.mode,
            },
        )

    if outcome.action == "AUTO_DECIDE_NOW":
        auto_request = ResumeReviewRequest(
            session_id=session_id,
            stage="repeat_annotation",
            gate_id="mask_review",
            decision="accept",
            decided_by="auto_rule",
            rationale_artifact_ref=str(request_ref),
        )
        return apply_mask_review_decision(state, auto_request)

    outputs = dict(state.get("stage_outputs") or {})
    outputs["repeat_annotation"] = {
        **repeat_out,
        "status": "blocked_human",
        "review_request_artifact_ref": str(request_ref),
        "review_policy_outcome": outcome.model_dump(mode="json"),
        "auto_decidable": True,
        "pending_human_confirmation": pending_confirmation,
    }
    return {
        "current_stage": "repeat_annotation",
        "supervision_state": "blocked_human",
        "stage_outputs": outputs,
        "active_interrupt": {
            "stage": "repeat_annotation",
            "gate_id": "mask_review",
            "reason": "mask_review_traceability",
            "decisions_allowed": payload.decisions_allowed,
            "auto_decidable": True,
            "pending_human_confirmation": pending_confirmation,
            "review_policy": review_policy,
            "review_request_artifact_ref": str(request_ref),
            "softmasked_genome_ref": repeat_out.get("softmasked_genome_ref"),
            "repeat_percentage": repeat_out.get("repeat_percentage"),
            "repeat_library_source": repeat_out.get("repeat_library_source"),
            "final_assembly_for_annotation": repeat_out.get("final_assembly_for_annotation"),
            "notes": repeat_out.get("notes", []),
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def apply_mask_review_decision(state: TopLevelState, request: ResumeReviewRequest) -> dict:
    """Apply a validated mask-review decision to advance repeat annotation."""
    if request.stage != "repeat_annotation":
        raise ResumeValidationError("mask_review resume requires stage repeat_annotation")
    repeat_out = _repeat_output(state)
    if request.decision not in {"accept", "skip"}:
        raise ResumeValidationError(f"unsupported mask_review decision: {request.decision!r}")

    softmasked_genome_ref = repeat_out.get("softmasked_genome_ref")
    if not softmasked_genome_ref:
        raise ResumeValidationError("mask_review resume requires softmasked_genome_ref in repeat_annotation output")

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    out_dir = data_dir / "artifacts" / "repeat_annotation" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)

    notes = list(repeat_out.get("notes") or [])
    if request.decision == "skip":
        softmasked_genome_ref = repeat_out.get("final_assembly_for_annotation") or softmasked_genome_ref
        notes = [*notes, "mask_review_skipped"]

    decision_payload = {
        "session_id": session_id,
        "stage": "repeat_annotation",
        "gate_id": "mask_review",
        "decision": request.decision,
        "softmasked_genome_ref": softmasked_genome_ref,
        "repeat_percentage": repeat_out.get("repeat_percentage"),
        "repeat_library_source": repeat_out.get("repeat_library_source"),
        "notes": list(request.notes),
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
        "metadata": request.metadata,
    }
    decision_ref = out_dir / "mask_review_decision.json"
    decision_ref.write_text(
        json.dumps(decision_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        decision_ref,
        stage="repeat_annotation",
        method_name=REPEAT_CONTRACT_METHOD,
        artifact_type="repeat_annotation.mask_review_decision",
        metadata={"decision": request.decision},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    Ledger(data_dir).record_review_decision(
        session_id,
        "repeat_annotation",
        decision=request.decision,
        notes=list(request.notes),
        artifact_ref=str(artifact.file_path),
        metadata=request.metadata,
        decided_by=request.decided_by,
        rationale_artifact_ref=request.rationale_artifact_ref,
    )

    updated_repeat_out = {**repeat_out, "softmasked_genome_ref": softmasked_genome_ref, "notes": notes}
    outputs = dict(state.get("stage_outputs") or {})
    outputs["repeat_annotation"] = updated_repeat_out
    updated_state: TopLevelState = {**state, "stage_outputs": outputs}  # type: ignore[return-value]
    commit_result = commit_repeat_annotation(updated_state)

    stage_outputs = dict(commit_result.get("stage_outputs") or {})
    stage_outputs["repeat_annotation"] = {
        **stage_outputs.get("repeat_annotation", {}),
        "decision": request.decision,
        "mask_review_decision_ref": str(artifact.file_path),
        "decided_by": request.decided_by,
        "rationale_artifact_ref": request.rationale_artifact_ref,
    }
    original_trace = list(state.get("route_trace") or [])
    return {
        **commit_result,
        "active_interrupt": None,
        "stage_outputs": stage_outputs,
        "route_trace": [*original_trace, "mask_review", "repeat_annotation_stage"],
    }


def fail_repeat_annotation(state: TopLevelState) -> dict:
    """Stop on invalid repeat annotation inputs, descriptors, or preflight block."""
    repeat_out = _repeat_output(state)
    status = repeat_out.get("status", "failed")
    default_reason = {
        "blocked_env": "repeat_annotation blocked on missing conda environment",
        "missing_tool": "repeat_annotation blocked on missing tool",
    }.get(status, "repeat_annotation failed")
    return {
        "current_stage": "repeat_annotation",
        "supervision_state": "failed_terminal",
        "last_error": repeat_out.get("reason", default_reason),
    }


def _register_repeatmasker_log_artifacts(
    state: TopLevelState,
    attempt: FrozenAttemptSpec,
    result: dict[str, Any],
) -> list[str]:
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    artifacts: list[ArtifactMeta] = []
    for path, artifact_type in [
        (Path(result["stdout_path"]), "command.stdout"),
        (Path(result["stderr_path"]), "command.stderr"),
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="repeat_annotation",
                    method_name=REPEAT_MASKING_METHOD,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )
    if not artifacts:
        return []
    append_artifact_records(data_dir, session_id, artifacts)
    ledger = Ledger(data_dir)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)
    return [str(artifact.file_path) for artifact in artifacts]


def _local_repeatmasker_enabled(repeat_out: dict[str, Any]) -> bool:
    return (
        repeat_out.get("execution_mode") == "local"
        and repeat_out.get("strategy") == "provided"
        and repeat_out.get("primary_method") == REPEAT_MASKING_METHOD
    )


def _validate_repeat_methods(methods: list[str]) -> str | None:
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    missing = [method for method in methods if method not in registry.methods]
    if missing:
        return f"repeat_annotation descriptors missing: {missing}"
    return None


def _safe_path_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or "repeatmasker"


def _unique_refs(refs: list[str]) -> list[str]:
    unique = []
    for ref in refs:
        if ref and ref not in unique:
            unique.append(ref)
    return unique


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


def _parse_tbl_repeat_percentage(tbl_path: Path) -> float | None:
    if not tbl_path.exists():
        return None
    try:
        text = tbl_path.read_text(encoding="utf-8")
        match = re.search(r"Bases masked:\s+[\d,]+ bp \(\s*([\d.]+) %\)", text)
        return float(match.group(1)) if match else None
    except Exception:
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
