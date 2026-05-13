"""Hi-C scaffolding stage nodes for scaffold planning or explicit skip."""

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
from app.execution.env_dispatch import attach_conda_fields, resolve_method_env
from app.execution.local_runner import LocalRunner
from app.graph.routing import next_stage_name
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import (
    HIC_QC_REPORT,
    HIC_REVIEW_BUNDLE,
    HIC_SCAFFOLD_AGP,
    HIC_SCAFFOLD_PLAN,
    HIC_SCAFFOLDED_ASSEMBLY,
    ArtifactMeta,
    HiCScaffoldDecisionArtifact,
    HiCScaffoldPlan,
    HiCSkipRecord,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
from app.schemas.state import TopLevelState

HIC_CANDIDATE_METHODS = [
    "hic_scaffolding.candidate.yahs",
    "hic_scaffolding.candidate.juicer_3ddna_juicebox",
]
HIC_CONTRACT_METHOD = "hic_scaffolding.contract"
HIC_YAHS_METHOD = "hic_scaffolding.candidate.yahs"


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
    existing = _hic_output(state)
    return _update_hic_output(
        state,
        {
            **existing,
            "status": "resolved",
            "pre_hic_assembly_ref": final_assembly_ref,
            "final_assembly_for_annotation": final_assembly_ref,
            "hic_read_refs": [asset.get("asset_id", "") for asset in hic_assets if asset.get("asset_id")],
            "hic_read_paths": [str(asset.get("path")) for asset in hic_assets if asset.get("path")],
            "hic_alignment_refs": [asset.get("asset_id", "") for asset in _hic_alignment_assets(state) if asset.get("asset_id")],
            "hic_alignment_paths": [str(asset.get("path")) for asset in _hic_alignment_assets(state) if asset.get("path")],
            "target_assembly_level": target_level,
        },
    )


def select_hic_mode(state: TopLevelState) -> dict:
    """Select scaffold or explicit skip mode."""
    hic_out = _hic_output(state)
    if hic_out.get("status") != "resolved":
        return {}
    if not hic_out.get("hic_read_refs") and not hic_out.get("hic_alignment_refs"):
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
        hic_alignment_refs=hic_out.get("hic_alignment_refs", []),
        candidate_methods=HIC_CANDIDATE_METHODS,
        expected_outputs={
            "hic_qc_report": str(work_dir / "hic_qc_report.json"),
            "chromosome_scaffolded_assembly": str(work_dir / "chromosome_scaffolded_assembly.fasta"),
            "juicebox_review_bundle": str(work_dir / "juicebox_review_bundle"),
        },
        notes=_hic_plan_notes(hic_out),
        metadata={"execution_mode": hic_out.get("execution_mode", "planned_only")},
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
        metadata={"mode": "scaffold", "execution_enabled": _local_yahs_enabled(hic_out)},
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


def freeze_yahs_attempt(state: TopLevelState) -> dict:
    """Freeze an explicit local YaHS scaffolding attempt."""
    hic_out = _hic_output(state)
    if hic_out.get("mode") != "scaffold" or not _local_yahs_enabled(hic_out):
        return {}

    descriptor_error = _validate_hic_methods([HIC_YAHS_METHOD])
    if descriptor_error:
        return _fail_update(state, descriptor_error)

    assembly_ref = Path(str(hic_out.get("pre_hic_assembly_ref", "")))
    if not assembly_ref.exists():
        return _update_hic_output(
            state,
            {
                "status": "blocked_human",
                "notes": _unique_refs([*(hic_out.get("notes") or []), "pre_hic_assembly_not_materialized_for_yahs"]),
            },
        )
    alignment_path, alignment_error = _hic_alignment_path(hic_out)
    if alignment_error:
        return _update_hic_output(
            state,
            {
                "status": "blocked_human",
                "notes": _unique_refs([*(hic_out.get("notes") or []), alignment_error]),
            },
        )

    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    env_spec, blocked = resolve_method_env(
        hic_out,
        stage="hic_scaffolding",
        method_name=HIC_YAHS_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_hic_output(state, blocked)

    safe_id = _safe_path_id(session_id)
    working_dir = data_dir / "work" / "hic_scaffolding" / session_id / "yahs" / safe_id
    artifact_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id / "yahs" / safe_id
    yahs_out_dir = working_dir / "yahs_out"
    out_prefix = yahs_out_dir / "yahs"
    scaffold_fasta = artifact_dir / "chromosome_scaffolded_assembly.fasta"
    scaffold_agp = artifact_dir / "chromosome_scaffolded_assembly.agp"
    yahs_fasta = Path(str(out_prefix) + "_scaffolds_final.fa")
    yahs_agp = Path(str(out_prefix) + "_scaffolds_final.agp")
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    script_path = working_dir / "run_yahs.sh"
    script_path.write_text(
        "set -e\n"
        f"mkdir -p {shlex.quote(str(yahs_out_dir))} {shlex.quote(str(artifact_dir))}\n"
        f"yahs -o {shlex.quote(str(out_prefix))} {shlex.quote(str(assembly_ref))} {shlex.quote(str(alignment_path))}\n"
        f"test -f {shlex.quote(str(yahs_fasta))}\n"
        f"cp {shlex.quote(str(yahs_fasta))} {shlex.quote(str(scaffold_fasta))}\n"
        f"if test -f {shlex.quote(str(yahs_agp))}; then cp {shlex.quote(str(yahs_agp))} {shlex.quote(str(scaffold_agp))}; fi\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    attempt_kwargs: dict[str, Any] = {
        "attempt_id": f"{session_id}:hic_scaffolding:yahs",
        "session_id": session_id,
        "stage": "hic_scaffolding",
        "method_name": HIC_YAHS_METHOD,
        "script_path": script_path,
        "working_dir": working_dir,
        "interpreter": "/bin/bash",
        "expected_outputs": [scaffold_fasta],
        "env": {str(k): str(v) for k, v in (hic_out.get("env") or {}).items()},
        "metadata": {
            "pre_hic_assembly_ref": str(assembly_ref),
            "hic_alignment_ref": str(alignment_path),
            "scaffold_fasta": str(scaffold_fasta),
            "scaffold_agp": str(scaffold_agp),
            "out_prefix": str(out_prefix),
        },
    }
    attach_conda_fields(env_spec, attempt_kwargs)
    attempt = FrozenAttemptSpec(**attempt_kwargs)
    return _update_hic_output(
        state,
        {
            "status": "yahs_frozen",
            "yahs_frozen_attempt": attempt.model_dump(mode="json"),
        },
    )


def run_yahs_attempt(state: TopLevelState) -> dict:
    """Run the frozen YaHS attempt locally."""
    hic_out = _hic_output(state)
    if hic_out.get("status") != "yahs_frozen":
        return {}

    attempt = FrozenAttemptSpec.model_validate(hic_out["yahs_frozen_attempt"])
    ledger = Ledger(state.get("data_dir", "data"))
    ledger.create_session(attempt.session_id, state.get("input_manifest_ref", ""))
    ledger.record_attempt_started(attempt.session_id, attempt)
    result = LocalRunner().run(attempt)
    result_payload = result.model_dump(mode="json")
    if not result.success:
        stderr = result.stderr_path.read_text(encoding="utf-8") if result.stderr_path.exists() else ""
        error = stderr.strip() or f"YaHS failed with return code {result.return_code}"
        ledger.record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=result.return_code,
            error=error,
        )
        result_payload["error"] = error
    return _update_hic_output(state, {"status": "yahs_ran", "yahs_execution_result": result_payload})


def collect_yahs_artifacts(state: TopLevelState) -> dict:
    """Collect YaHS candidate artifacts while preserving the pre-Hi-C fallback."""
    hic_out = _hic_output(state)
    if hic_out.get("status") != "yahs_ran":
        return {}

    attempt = FrozenAttemptSpec.model_validate(hic_out["yahs_frozen_attempt"])
    result = dict(hic_out.get("yahs_execution_result") or {})
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    artifact_refs = list(hic_out.get("artifact_refs") or [])
    artifacts: list[ArtifactMeta] = []
    ledger = Ledger(data_dir)
    notes = list(hic_out.get("notes") or [])

    if result.get("success"):
        scaffold_fasta = Path(str(attempt.metadata["scaffold_fasta"]))
        artifacts.append(
            ArtifactMeta.from_file(
                scaffold_fasta,
                stage="hic_scaffolding",
                method_name=HIC_YAHS_METHOD,
                artifact_type=HIC_SCAFFOLDED_ASSEMBLY,
                metadata={"attempt_id": attempt.attempt_id, "review_required": True},
            )
        )
        scaffold_agp = Path(str(attempt.metadata["scaffold_agp"]))
        if scaffold_agp.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    scaffold_agp,
                    stage="hic_scaffolding",
                    method_name=HIC_YAHS_METHOD,
                    artifact_type=HIC_SCAFFOLD_AGP,
                    metadata={"attempt_id": attempt.attempt_id, "review_required": True},
                )
            )
        review_bundle_ref = _write_yahs_review_bundle(state, hic_out, attempt, result)
        artifacts.append(
            ArtifactMeta.from_file(
                Path(review_bundle_ref),
                stage="hic_scaffolding",
                method_name=HIC_YAHS_METHOD,
                artifact_type=HIC_REVIEW_BUNDLE,
                metadata={"attempt_id": attempt.attempt_id, "review_required": True},
            )
        )
        qc_report_ref = _write_yahs_qc_report(state, hic_out, attempt, result)
        artifacts.append(
            ArtifactMeta.from_file(
                Path(qc_report_ref),
                stage="hic_scaffolding",
                method_name=HIC_YAHS_METHOD,
                artifact_type=HIC_QC_REPORT,
                metadata={"attempt_id": attempt.attempt_id, "review_required": True},
            )
        )
        notes = _unique_refs([*notes, "yahs_executed_locally", "contact_map_policy_required_before_promotion"])
        update_payload = {
            "status": "blocked_human",
            "chromosome_scaffolded_assembly_candidate_ref": str(scaffold_fasta),
            "hic_review_bundle_ref": review_bundle_ref,
            "hic_qc_report_ref": qc_report_ref,
            "selected_method_name": HIC_YAHS_METHOD,
            "notes": notes,
        }
    else:
        notes = _unique_refs([*notes, "yahs_local_execution_failed", "annotation_continues_on_pre_hic_assembly_fallback"])
        update_payload = {
            "status": "blocked_human",
            "selected_method_name": HIC_YAHS_METHOD,
            "notes": notes,
            "yahs_error": result.get("error"),
        }

    for path, artifact_type in [
        (Path(result["stdout_path"]), "command.stdout"),
        (Path(result["stderr_path"]), "command.stderr"),
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="hic_scaffolding",
                    method_name=HIC_YAHS_METHOD,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    if artifacts:
        append_artifact_records(data_dir, session_id, artifacts)
        artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
        if result.get("success"):
            ledger.record_attempt_succeeded(session_id, attempt, artifacts=artifact_payloads)
        for artifact in artifact_payloads:
            ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)
        artifact_refs.extend(str(artifact.file_path) for artifact in artifacts)

    return _update_hic_output(
        state,
        {
            **update_payload,
            "artifact_refs": _unique_refs(artifact_refs),
            "yahs_frozen_attempt": None,
            "yahs_execution_result": None,
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
            "chromosome_scaffolded_assembly_candidate_ref": hic_out.get("chromosome_scaffolded_assembly_candidate_ref"),
            "hic_review_bundle_ref": hic_out.get("hic_review_bundle_ref"),
            "notes": hic_out.get("notes", []),
            "blocked_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def apply_hic_scaffolding_resume_decision(state: TopLevelState, request: ResumeReviewRequest) -> dict:
    """Apply a validated Hi-C scaffolding decision to a blocked state."""

    if request.stage != "hic_scaffolding":
        raise ResumeValidationError("Hi-C resume requires stage hic_scaffolding")
    hic_out = _hic_output(state)
    allowed = {"select_method", "skip", "stop_human_review"}
    if request.decision not in allowed:
        raise ResumeValidationError(f"unsupported hic_scaffolding decision: {request.decision}")
    if request.decision == "select_method":
        if not request.method_name:
            raise ResumeValidationError("hic_scaffolding select_method requires method_name")
        if request.method_name not in set(hic_out.get("candidate_methods") or []):
            raise ResumeValidationError(f"unknown hic_scaffolding method_name: {request.method_name}")
        if not request.notes and not request.metadata.get("contact_map_policy"):
            raise ResumeValidationError("hic_scaffolding select_method requires contact-map policy notes or metadata")
    elif request.decision == "skip" and request.method_name:
        raise ResumeValidationError("hic_scaffolding skip decision must not include method_name")

    pre_hic_assembly_ref = hic_out.get("pre_hic_assembly_ref")
    final_assembly_for_annotation = hic_out.get("final_assembly_for_annotation") or pre_hic_assembly_ref
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
    )

    outputs = dict(state.get("stage_outputs") or {})
    outputs["hic_scaffolding"] = {
        **hic_out,
        "status": "failed_terminal" if request.decision == "stop_human_review" else "completed",
        "decision": request.decision,
        "selected_method_name": request.method_name,
        "pre_hic_assembly_ref": pre_hic_assembly_ref,
        "final_assembly_for_annotation": final_assembly_for_annotation,
        "review_decision_artifact_ref": str(artifact.file_path),
        "notes": notes,
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


def _write_yahs_review_bundle(
    state: TopLevelState,
    hic_out: dict[str, Any],
    attempt: FrozenAttemptSpec,
    result: dict[str, Any],
) -> str:
    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    out_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id / "yahs"
    out_dir.mkdir(parents=True, exist_ok=True)
    review_bundle_ref = out_dir / "hic_review_bundle.json"
    payload = {
        "session_id": session_id,
        "method_name": HIC_YAHS_METHOD,
        "status": "yahs_executed_review_required",
        "pre_hic_assembly_ref": hic_out.get("pre_hic_assembly_ref"),
        "final_assembly_for_annotation": hic_out.get("final_assembly_for_annotation"),
        "chromosome_scaffolded_assembly_candidate_ref": attempt.metadata.get("scaffold_fasta"),
        "hic_alignment_ref": attempt.metadata.get("hic_alignment_ref"),
        "stdout_path": result.get("stdout_path"),
        "stderr_path": result.get("stderr_path"),
        "review_required": True,
        "notes": ["contact_map_policy_required_before_promotion", "pre_hic_assembly_preserved_as_annotation_fallback"],
    }
    review_bundle_ref.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(review_bundle_ref)


def _write_yahs_qc_report(
    state: TopLevelState,
    hic_out: dict[str, Any],
    attempt: FrozenAttemptSpec,
    result: dict[str, Any],
) -> str:
    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    out_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id / "yahs"
    out_dir.mkdir(parents=True, exist_ok=True)
    qc_report_ref = out_dir / "hic_qc_report.json"
    payload = {
        "session_id": session_id,
        "method_name": HIC_YAHS_METHOD,
        "status": "available",
        "pre_hic_assembly_ref": hic_out.get("pre_hic_assembly_ref"),
        "hic_alignment_ref": attempt.metadata.get("hic_alignment_ref"),
        "chromosome_scaffolded_assembly_candidate_ref": attempt.metadata.get("scaffold_fasta"),
        "return_code": result.get("return_code"),
        "notes": ["qc_thresholds_not_applied", "contact_map_policy_required_before_promotion"],
    }
    qc_report_ref.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(qc_report_ref)


def _validate_hic_methods(methods: list[str]) -> str | None:
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    missing = [method for method in methods if method not in registry.methods]
    if missing:
        return f"hic_scaffolding descriptors missing: {missing}"
    return None


def _local_yahs_enabled(hic_out: dict[str, Any]) -> bool:
    method = hic_out.get("primary_method") or hic_out.get("selected_method_name")
    return hic_out.get("execution_mode") == "local" and method == HIC_YAHS_METHOD


def _hic_plan_notes(hic_out: dict[str, Any]) -> list[str]:
    notes = [
        "contact_map_policy_required",
        "pre_hic_assembly_preserved_as_annotation_fallback",
    ]
    if _local_yahs_enabled(hic_out):
        if _hic_alignment_path(hic_out)[1]:
            notes.append("alignment_required_before_yahs")
        else:
            notes.append("yahs_local_execution_requested")
    else:
        notes.append("execution_deferred_to_method_specific_task")
    return notes


def _hic_alignment_path(hic_out: dict[str, Any]) -> tuple[Path | None, str | None]:
    for key in ["hic_alignment_bam_ref", "hic_alignment_ref"]:
        value = hic_out.get(key)
        if value:
            path = Path(str(value))
            if path.exists():
                return path, None
            return None, "alignment_required_before_yahs"
    for value in hic_out.get("hic_alignment_paths") or []:
        path = Path(str(value))
        if path.exists():
            return path, None
    return None, "alignment_required_before_yahs"


def _hic_alignment_assets(state: TopLevelState) -> list[dict[str, Any]]:
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
        if str(asset.get("asset_type", "")) in {"hic_alignment_bam", "hic_alignments_bam", "hic_alignment"}
    ]


def _safe_path_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or "yahs"


def _unique_refs(refs: list[str]) -> list[str]:
    unique = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique


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
