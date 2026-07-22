"""BUSCO metric method subgraph and its implementation."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from app.execution.attempt_owners import DIRECT_ATTEMPTS_KEY
from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_BUSCO_SHORT_SUMMARY,
    ASSEMBLY_REVIEW_BUSCO_SUMMARY,
    ArtifactMeta,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.assembly_review.method_application.support import (
    local_execution_enabled,
    mark_missing_signals,
    materialized_candidates,
    review_methods_error,
    review_output,
    rewrite_eval_bundle,
    safe_candidate_id,
    update_review_output,
    update_signal,
)
from app.stages.base import absolute_data_dir, ordered_unique, stage_work_dir
from app.storage.artifacts import publish_or_recover_work_artifact
from app.storage.ledger import Ledger

BUSCO_METHOD = "assembly_review.eval.busco"
FROZEN_STATUS = "busco_frozen"
RAN_STATUS = "busco_ran"
LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly_review",
    name="BUSCO",
    method_name=BUSCO_METHOD,
    frozen_status=FROZEN_STATUS,
    ran_status=RAN_STATUS,
    cleanup_outputs=False,
    process_failure="collect",
)


def build_busco_graph():
    return build_method_graph(
        LIFECYCLE,
        freeze_busco_attempts,
        collect_busco_artifacts,
    )


def freeze_busco_attempts(state: TopLevelState) -> dict:
    """Freeze BUSCO attempts when offline lineage parameters are supplied."""
    output = review_output(state)
    review_plan = output.get("review_plan") or {}
    if review_plan.get("status") != "evaluating" or not local_execution_enabled(output):
        return {}
    descriptor_error = review_methods_error([BUSCO_METHOD])
    if descriptor_error:
        return update_review_output(state, {"status": "failed", "reason": descriptor_error})
    params, error = busco_parameters(output)
    candidates = output.get("candidates", [])
    materialized = materialized_candidates(candidates)
    signals = output.get("signals", [])
    if error:
        signals = mark_missing_signals(signals, candidates, signal_name="busco", method_name=BUSCO_METHOD, reason=error)
        return rewrite_eval_bundle(state, {**output, "signals": signals}, status="evaluating")
    signals = mark_missing_signals(
        signals, candidates, signal_name="busco", method_name=BUSCO_METHOD,
        reason="busco_requires_materialized_draft_assembly",
        materialized_ids={candidate.get("candidate_id") for candidate in materialized},
    )
    if not materialized:
        return rewrite_eval_bundle(state, {**output, "signals": signals}, status="evaluating")
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        output, stage="assembly_review", method_name=BUSCO_METHOD, session_id=session_id, data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_review_output(state, blocked)
    attempts = []
    lineage_path = Path(params["lineage_path"])
    for candidate in materialized:
        candidate_id = candidate.get("candidate_id", "unknown")
        safe_id = safe_candidate_id(candidate_id)
        working_dir = stage_work_dir(state, "assembly_review", "busco", safe_id)
        working_dir.mkdir(parents=True, exist_ok=True)
        script_path = working_dir / "run_busco.sh"
        out_name = f"busco_{safe_id}"
        output_dir = working_dir / "output"
        short_summary = output_dir / out_name / f"short_summary.specific.{lineage_path.name}.{out_name}.txt"
        assembly_ref = Path(candidate["final_assembly_ref"])
        script_path.write_text(
            "set -e\n"
            f"mkdir -p {shlex.quote(str(output_dir))}\n"
            f"cd {shlex.quote(str(output_dir))}\n"
            f"busco -i {shlex.quote(str(assembly_ref))} -l {shlex.quote(str(lineage_path))} "
            f"-o {shlex.quote(out_name)} -m {shlex.quote(str(params['mode']))} "
            f"--offline --cpu {params['threads']}\n"
            f"test -f {shlex.quote(str(short_summary))}\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        attempt = build_frozen_attempt(
            attempt_id=f"{session_id}:assembly_review:busco:{safe_id}", session_id=session_id,
            stage="assembly_review", method_name=BUSCO_METHOD, script_path=script_path,
            working_dir=working_dir, expected_outputs=[short_summary], env=output.get("env"), env_spec=env_spec,
            metadata={"candidate_id": candidate_id, "draft_assembly_ref": str(assembly_ref),
                      "busco_output_dir": str(output_dir / out_name), "short_summary_ref": str(short_summary),
                      "lineage_path": str(lineage_path), "lineage_name": params["lineage_name"],
                      "lineage_version": params["lineage_version"], "lineage_source": params["lineage_source"],
                      "mode": params["mode"], "threads": params["threads"]},
        )
        attempts.append(attempt)
    return freeze_method_attempts(
        state,
        LIFECYCLE,
        attempts,
        stage_updates={"signals": signals},
    )


def collect_busco_artifacts(state: TopLevelState) -> dict:
    """Collect BUSCO artifacts and update review signals."""
    output = review_output(state)
    if output.get("status") != RAN_STATUS:
        return {}
    executions = load_method_executions(state, LIFECYCLE)
    signals = list(output.get("signals") or [])
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    artifact_refs = list(output.get("artifact_refs") or [])
    ledger = Ledger(data_dir)
    for execution in executions:
        attempt = execution.attempt
        result = execution.evidence
        candidate_id = str(attempt.metadata.get("candidate_id"))
        artifacts: list[ArtifactMeta] = []
        metric_refs: list[str] = []
        metadata: dict[str, Any] = {"attempt_id": attempt.attempt_id, "draft_assembly_ref": attempt.metadata.get("draft_assembly_ref"), "busco_output_dir": attempt.metadata.get("busco_output_dir"), "provenance": busco_provenance(attempt)}
        if result.success:
            artifact_metadata = {"attempt_id": attempt.attempt_id, "candidate_id": candidate_id}
            short_summary = publish_or_recover_work_artifact(
                data_dir,
                session_id,
                Path(attempt.metadata["short_summary_ref"]),
                stage="assembly_review",
                method_name=BUSCO_METHOD,
                artifact_type=ASSEMBLY_REVIEW_BUSCO_SHORT_SUMMARY,
                metadata=artifact_metadata,
            ).path
            artifacts.append(ArtifactMeta.from_file(short_summary, stage="assembly_review", method_name=BUSCO_METHOD, artifact_type=ASSEMBLY_REVIEW_BUSCO_SHORT_SUMMARY, metadata=artifact_metadata))
            metric_refs.append(str(short_summary))
            metrics = parse_busco_short_summary(short_summary)
            summary_source = Path(attempt.working_dir) / "busco_summary.json"
            summary_source.write_text(json.dumps({"session_id": session_id, "method_name": BUSCO_METHOD, "candidate_id": candidate_id, "attempt_id": attempt.attempt_id, "draft_assembly_ref": attempt.metadata.get("draft_assembly_ref"), "short_summary_ref": str(short_summary), "lineage": busco_provenance(attempt), "metrics": metrics, "notes": ["thresholds_not_applied_without_human_approval"]}, ensure_ascii=False, indent=2), encoding="utf-8")
            summary_path = publish_or_recover_work_artifact(
                data_dir,
                session_id,
                summary_source,
                stage="assembly_review",
                method_name=BUSCO_METHOD,
                artifact_type=ASSEMBLY_REVIEW_BUSCO_SUMMARY,
                metadata=artifact_metadata,
            ).path
            artifacts.append(ArtifactMeta.from_file(summary_path, stage="assembly_review", method_name=BUSCO_METHOD, artifact_type=ASSEMBLY_REVIEW_BUSCO_SUMMARY, metadata=artifact_metadata))
            metric_refs.append(str(summary_path))
            payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
            ledger.record_attempt_succeeded(session_id, attempt, artifacts=payloads)
            for artifact in payloads:
                ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)
            artifact_refs.extend(str(artifact.file_path) for artifact in artifacts)
            metadata.update({"metrics": metrics, "busco_summary_ref": str(summary_path)})
            signals = update_signal(signals, signal_name="busco", method_name=BUSCO_METHOD, candidate_id=candidate_id, status="available", metric_refs=metric_refs, notes=["thresholds_not_applied_without_human_approval", "busco_executed"], metadata=metadata)
        else:
            metadata.update({"return_code": result.return_code, "error": result.error})
            signals = update_signal(signals, signal_name="busco", method_name=BUSCO_METHOD, candidate_id=candidate_id, status="failed", metric_refs=[], notes=["busco_execution_failed", "candidate_not_rejected_from_metrics"], metadata=metadata)
    update = rewrite_eval_bundle(state, {**output, "signals": signals, "artifact_refs": ordered_unique(artifact_refs)}, status="evaluating")
    outputs = update["stage_outputs"]
    outputs["assembly_review"] = {
        **outputs["assembly_review"],
        DIRECT_ATTEMPTS_KEY: [],
    }
    return {"stage_outputs": outputs}


def busco_parameters(output: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    params = dict(output.get("busco_parameters") or {})
    if not params:
        return {}, "busco_lineage_not_supplied"
    for key in ["lineage_path", "lineage_name", "lineage_version", "lineage_source"]:
        if not str(params.get(key) or "").strip():
            return {}, f"assembly_review.busco_parameters.{key} is required"
    lineage_path = Path(str(params["lineage_path"])).expanduser()
    if not lineage_path.exists():
        return {}, "busco_lineage_path_missing"
    try:
        threads = int(params.get("threads", 1))
    except (TypeError, ValueError):
        return {}, "assembly_review.busco_parameters.threads must be a positive integer"
    if threads <= 0:
        return {}, "assembly_review.busco_parameters.threads must be a positive integer"
    return {"lineage_path": str(lineage_path.resolve(strict=False)), "lineage_name": str(params["lineage_name"]), "lineage_version": str(params["lineage_version"]), "lineage_source": str(params["lineage_source"]), "mode": str(params.get("mode") or "genome"), "threads": threads}, None


def parse_busco_short_summary(path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    key_map = {"complete buscos (c)": "complete_buscos", "complete and single-copy buscos (s)": "single_copy_buscos", "complete and duplicated buscos (d)": "duplicated_buscos", "fragmented buscos (f)": "fragmented_buscos", "missing buscos (m)": "missing_buscos", "total busco groups searched": "total_busco_groups_searched"}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("C:"):
            metrics["summary_line"] = stripped
            for key, value in re.findall(r"([CSDFM]):([0-9.]+)%", stripped):
                metrics[{"C": "complete_percent", "S": "single_copy_percent", "D": "duplicated_percent", "F": "fragmented_percent", "M": "missing_percent"}[key]] = float(value)
        elif match := re.match(r"^(\d+)\s+(.+)$", stripped):
            if out_key := key_map.get(match.group(2).strip().lower()):
                metrics[out_key] = int(match.group(1))
    return metrics


def busco_provenance(attempt: FrozenAttemptSpec) -> dict[str, Any]:
    return {key: attempt.metadata.get(key) for key in ["lineage_path", "lineage_name", "lineage_version", "lineage_source", "mode", "threads"]}
