"""QUAST metric method subgraph and its implementation."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_QUAST_REPORT_TSV,
    ASSEMBLY_REVIEW_QUAST_REPORT_TXT,
    ASSEMBLY_REVIEW_QUAST_SUMMARY,
    ArtifactMeta,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.assembly_review.method_application.lifecycle import (
    absolute_review_data_dir,
    build_method_graph,
    local_execution_enabled,
    mark_missing_signals,
    materialized_candidates,
    resolve_review_method_env,
    review_methods_error,
    review_output,
    rewrite_eval_bundle,
    run_attempts,
    safe_candidate_id,
    unique_refs,
    update_review_output,
    update_signal,
)

QUAST_METHOD = "assembly_review.eval.quast"
FROZEN_STATUS = "quast_frozen"
RAN_STATUS = "quast_ran"
ATTEMPTS_KEY = "quast_frozen_attempts"
RESULTS_KEY = "quast_execution_results"


def build_quast_graph():
    return build_method_graph(freeze_quast_attempts, run_quast_attempts, collect_quast_artifacts, frozen_status=FROZEN_STATUS, ran_status=RAN_STATUS)


def freeze_quast_attempts(state: TopLevelState) -> dict:
    """Freeze local QUAST attempts for materialized assembly candidates."""
    output = review_output(state)
    if output.get("status") == "failed" or not local_execution_enabled(output):
        return {}
    descriptor_error = review_methods_error([QUAST_METHOD])
    if descriptor_error:
        return update_review_output(state, {"status": "failed", "reason": descriptor_error})
    candidates = output.get("candidates", [])
    materialized = materialized_candidates(candidates)
    signals = mark_missing_signals(
        output.get("signals", []), candidates, signal_name="quast", method_name=QUAST_METHOD,
        reason="quast_requires_materialized_draft_assembly",
        materialized_ids={candidate.get("candidate_id") for candidate in materialized},
    )
    if not materialized:
        return rewrite_eval_bundle(state, {**output, "signals": signals}, status="quast_skipped")
    threads, error = quast_threads(output)
    if error:
        return update_review_output(state, {"status": "failed", "reason": error})
    session_id = state["session_id"]
    data_dir = absolute_review_data_dir(state)
    env_spec, blocked = resolve_review_method_env(
        output, QUAST_METHOD, session_id=session_id, data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_review_output(state, blocked)
    attempts = []
    for candidate in materialized:
        candidate_id = candidate.get("candidate_id", "unknown")
        safe_id = safe_candidate_id(candidate_id)
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
        attempt = build_frozen_attempt(
            attempt_id=f"{session_id}:assembly_review:quast:{safe_id}", session_id=session_id,
            stage="assembly_review", method_name=QUAST_METHOD, script_path=script_path,
            working_dir=working_dir, expected_outputs=[report_tsv], env=output.get("env"), env_spec=env_spec,
            metadata={"candidate_id": candidate_id, "draft_assembly_ref": str(assembly_ref),
                      "quast_output_dir": str(out_dir), "report_tsv": str(report_tsv), "threads": threads},
        )
        attempts.append(attempt.model_dump(mode="json"))
    return update_review_output(state, {"status": FROZEN_STATUS, "signals": signals, ATTEMPTS_KEY: attempts})


def run_quast_attempts(state: TopLevelState) -> dict:
    return run_attempts(state, frozen_status=FROZEN_STATUS, attempts_key=ATTEMPTS_KEY,
                        ran_status=RAN_STATUS, results_key=RESULTS_KEY, fail_label="QUAST")


def collect_quast_artifacts(state: TopLevelState) -> dict:
    """Collect QUAST artifacts and update review signals."""
    output = review_output(state)
    if output.get("status") != RAN_STATUS:
        return {}
    attempts = {payload["attempt_id"]: FrozenAttemptSpec.model_validate(payload) for payload in output.get(ATTEMPTS_KEY, [])}
    signals = list(output.get("signals") or [])
    data_dir = Path(state.get("data_dir", "data"))
    session_id = state["session_id"]
    artifact_refs = list(output.get("artifact_refs") or [])
    ledger = Ledger(data_dir)
    for result in output.get(RESULTS_KEY, []):
        attempt = attempts[result["attempt_id"]]
        candidate_id = str(attempt.metadata.get("candidate_id"))
        artifacts: list[ArtifactMeta] = []
        metric_refs: list[str] = []
        notes = ["thresholds_not_applied_without_human_approval"]
        metadata: dict[str, Any] = {"attempt_id": attempt.attempt_id, "draft_assembly_ref": attempt.metadata.get("draft_assembly_ref"), "quast_output_dir": attempt.metadata.get("quast_output_dir")}
        if result.get("success"):
            report_tsv = Path(attempt.metadata["report_tsv"])
            artifacts.append(ArtifactMeta.from_file(report_tsv, stage="assembly_review", method_name=QUAST_METHOD, artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TSV, metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id}))
            metric_refs.append(str(report_tsv))
            report_txt = Path(attempt.metadata["quast_output_dir"]) / "report.txt"
            if report_txt.exists():
                artifacts.append(ArtifactMeta.from_file(report_txt, stage="assembly_review", method_name=QUAST_METHOD, artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TXT, metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id}))
                metric_refs.append(str(report_txt))
            metrics = parse_quast_report_tsv(report_tsv)
            summary_path = Path(attempt.metadata["quast_output_dir"]).parent / "quast_summary.json"
            summary_path.write_text(json.dumps({"session_id": session_id, "method_name": QUAST_METHOD, "candidate_id": candidate_id, "attempt_id": attempt.attempt_id, "draft_assembly_ref": attempt.metadata.get("draft_assembly_ref"), "report_tsv_ref": str(report_tsv), "metrics": metrics, "notes": notes}, ensure_ascii=False, indent=2), encoding="utf-8")
            artifacts.append(ArtifactMeta.from_file(summary_path, stage="assembly_review", method_name=QUAST_METHOD, artifact_type=ASSEMBLY_REVIEW_QUAST_SUMMARY, metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id}))
            metric_refs.append(str(summary_path))
            register_command_logs(artifacts, result, attempt, candidate_id)
            append_artifact_records(data_dir, session_id, artifacts)
            payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
            ledger.record_attempt_succeeded(session_id, attempt, artifacts=payloads)
            for artifact in payloads:
                ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)
            artifact_refs.extend(str(artifact.file_path) for artifact in artifacts)
            metadata.update({"metrics": metrics, "quast_summary_ref": str(summary_path)})
            signals = update_signal(signals, signal_name="quast", method_name=QUAST_METHOD, candidate_id=candidate_id, status="available", metric_refs=metric_refs, notes=[*notes, "quast_executed"], metadata=metadata)
        else:
            register_command_logs(artifacts, result, attempt, candidate_id)
            if artifacts:
                append_artifact_records(data_dir, session_id, artifacts)
                payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
                for artifact in payloads:
                    ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)
                artifact_refs.extend(str(artifact.file_path) for artifact in artifacts)
            metadata.update({"return_code": result.get("return_code"), "error": result.get("error")})
            signals = update_signal(signals, signal_name="quast", method_name=QUAST_METHOD, candidate_id=candidate_id, status="failed", metric_refs=[str(artifact.file_path) for artifact in artifacts], notes=["quast_execution_failed", "candidate_not_rejected_from_metrics"], metadata=metadata)
    update = rewrite_eval_bundle(state, {**output, "signals": signals, "artifact_refs": unique_refs(artifact_refs)}, status="evaluating")
    outputs = update["stage_outputs"]
    outputs["assembly_review"] = {**outputs["assembly_review"], ATTEMPTS_KEY: [], RESULTS_KEY: []}
    return {"stage_outputs": outputs}


def quast_threads(output: dict[str, Any]) -> tuple[int, str | None]:
    try:
        threads = int(dict(output.get("quast_parameters") or {}).get("threads", 1))
    except (TypeError, ValueError):
        return 0, "assembly_review.quast_parameters.threads must be a positive integer"
    return (threads, None) if threads > 0 else (0, "assembly_review.quast_parameters.threads must be a positive integer")


def register_command_logs(artifacts: list[ArtifactMeta], result: dict[str, Any], attempt: FrozenAttemptSpec, candidate_id: str) -> None:
    for value, artifact_type in [(result["stdout_path"], "command.stdout"), (result["stderr_path"], "command.stderr")]:
        path = Path(value)
        if path.exists():
            artifacts.append(ArtifactMeta.from_file(path, stage="assembly_review", method_name=QUAST_METHOD, artifact_type=artifact_type, metadata={"attempt_id": attempt.attempt_id, "candidate_id": candidate_id}))


def parse_quast_report_tsv(path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    key_map = {"# contigs": "contig_count", "largest contig": "largest_contig", "total length": "total_length", "n50": "n50", "l50": "l50", "gc (%)": "gc_percent"}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or (out_key := key_map.get(parts[0].strip().lower())) is None:
            continue
        raw = parts[1].strip().replace(",", "")
        try:
            metrics[out_key] = float(raw) if "." in raw else int(raw)
        except ValueError:
            metrics[f"{out_key}_raw"] = parts[1].strip()
    return metrics
