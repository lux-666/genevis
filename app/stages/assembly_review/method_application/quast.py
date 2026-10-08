"""QUAST metric method subgraph and its implementation."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from app.control.workflow_plan import current_workflow_step_id
from app.execution.attempt_owners import DIRECT_ATTEMPTS_KEY
from app.execution.env_dispatch import resolve_method_env
from app.execution.input_integrity import (
    artifact_input_dependency,
    asset_input_dependencies,
)
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import MethodOutcome
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_REVIEW_QUAST_REPORT_TSV,
    ASSEMBLY_REVIEW_QUAST_REPORT_TXT,
    ASSEMBLY_REVIEW_QUAST_SUMMARY,
    ArtifactMeta,
    AssemblyEvalSignal,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly_review.method_application.support import (
    evaluator_method_outcome,
    evaluator_selected,
    local_execution_enabled,
    mark_evaluator_not_started,
    mark_missing_signals,
    planner_evaluator_outcome,
    review_methods_error,
    review_output,
    rewrite_eval_bundle,
    safe_candidate_id,
    update_signal,
    verified_review_candidate,
)
from app.stages.assembly_review.state import AssemblyReviewCursor
from app.stages.base import absolute_data_dir, ordered_unique, stage_work_dir
from app.stages.resource_resolution import resolve_stage_resource
from app.stages.resource_specs import QUAST_REFERENCE_SPEC
from app.storage.artifacts import (
    publish_or_recover_work_artifact,
    record_registered_artifacts,
)
from app.storage.ledger import Ledger

QUAST_METHOD = "assembly_review.eval.quast"
FROZEN_STATUS = "quast_frozen"
RAN_STATUS = "quast_ran"
LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly_review",
    name="QUAST",
    method_name=QUAST_METHOD,
    frozen_status=FROZEN_STATUS,
    ran_status=RAN_STATUS,
    cleanup_outputs=False,
    process_failure="collect",
    outcome_policy=lambda state: quast_method_outcome(state),
)


def build_quast_graph() -> CompiledStateGraph[
    TopLevelState, None, TopLevelState, TopLevelState
]:
    return build_method_graph(
        LIFECYCLE,
        freeze_quast_attempts,
        collect_quast_artifacts,
    )


def quast_planner_outcome(state: TopLevelState) -> MethodOutcome | None:
    return planner_evaluator_outcome(
        state,
        method_name=QUAST_METHOD,
    )


def quast_method_outcome(state: TopLevelState) -> MethodOutcome | None:
    return evaluator_method_outcome(
        state,
        method_name=QUAST_METHOD,
        signal_name="quast",
    )


def freeze_quast_attempts(state: TopLevelState) -> StateUpdate:
    """Freeze local QUAST attempts for materialized assembly candidates."""
    output = review_output(state)
    review_plan = output.get("review_plan") or {}
    if review_plan.get("status") != "evaluating" or not local_execution_enabled(output):
        return {}
    if not evaluator_selected(state, QUAST_METHOD):
        return {}
    descriptor_error = review_methods_error([QUAST_METHOD])
    if descriptor_error:
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_not_configured",
        )
        return rewrite_eval_bundle(
            state,
            {**output, "signals": signals},
            status="evaluating",
        )
    candidates = output.get("candidates", [])
    try:
        materialized = [
            (candidate, verified_review_candidate(state, candidate))
            for candidate in candidates
        ]
    except (OSError, TypeError, ValueError):
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_candidate_dependency_invalid",
        )
        return rewrite_eval_bundle(
            state,
            {**output, "signals": signals},
            status="evaluating",
        )
    signals = mark_missing_signals(
        output.get("signals", []),
        candidates,
        signal_name="quast",
        method_name=QUAST_METHOD,
        reason="quast_requires_materialized_draft_assembly",
        materialized_ids={
            candidate.get("candidate_id") for candidate, _verified in materialized
        },
    )
    if not materialized:
        return rewrite_eval_bundle(
            state, {**output, "signals": signals}, status="evaluating"
        )
    threads, error = quast_threads(output)
    if error:
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_not_configured",
        )
        return rewrite_eval_bundle(
            state,
            {**output, "signals": signals},
            status="evaluating",
        )
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        dict(output),
        stage="assembly_review",
        method_name=QUAST_METHOD,
        workflow_step_id=current_workflow_step_id(
            state, expected_stage="assembly_review"
        ),
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_environment_preflight_failed",
        )
        return rewrite_eval_bundle(
            state,
            {
                **output,
                "env_target": blocked["env_target"],
                "missing_tools": blocked["missing_tools"],
                "method_name": blocked["method_name"],
                "signals": signals,
            },
            status="evaluating",
        )
    attempts: list[FrozenAttemptSpec] = []
    try:
        reference = resolve_stage_resource(
            state,
            resource_spec=QUAST_REFERENCE_SPEC,
        )
    except (OSError, TypeError, ValueError):
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_reference_dependency_invalid",
        )
        return rewrite_eval_bundle(
            state,
            {**output, "signals": signals},
            status="evaluating",
        )
    if reference.found and len(reference.paths) != 1:
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_requires_single_reference_genome",
        )
        return rewrite_eval_bundle(
            state,
            {**output, "signals": signals},
            status="evaluating",
        )
    reference_path = (
        Path(reference.paths[0]).expanduser().resolve(strict=False)
        if reference.found
        else None
    )
    if reference_path is not None and len(reference.asset_records) != 1:
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_reference_must_be_registry_backed",
        )
        return rewrite_eval_bundle(
            state,
            {**output, "signals": signals},
            status="evaluating",
        )
    try:
        reference_dependencies = (
            asset_input_dependencies(
                state,
                [record.asset_id for record in reference.asset_records],
                [record.path for record in reference.asset_records],
            )
            if reference_path is not None
            else []
        )
    except (OSError, TypeError, ValueError):
        signals = mark_evaluator_not_started(
            output,
            method_name=QUAST_METHOD,
            signal_name="quast",
            reason="quast_reference_dependency_invalid",
        )
        return rewrite_eval_bundle(
            state,
            {**output, "signals": signals},
            status="evaluating",
        )
    reference_asset_id = (
        reference.asset_records[0].asset_id if reference.asset_records else None
    )
    for candidate, verified in materialized:
        candidate_id = candidate.get("candidate_id", "unknown")
        safe_id = safe_candidate_id(candidate_id)
        working_dir = stage_work_dir(state, "assembly_review", "quast", safe_id)
        working_dir.mkdir(parents=True, exist_ok=True)
        script_path = working_dir / "run_quast.sh"
        out_dir = working_dir / "quast_out"
        report_tsv = out_dir / "report.tsv"
        assembly_ref = verified.primary_output_path
        reference_arg = (
            f"-r {shlex.quote(str(reference_path))} " if reference_path else ""
        )
        script_path.write_text(
            "set -e\n"
            f"mkdir -p {shlex.quote(str(out_dir))}\n"
            f"quast.py {shlex.quote(str(assembly_ref))} "
            f"{reference_arg}-o {shlex.quote(str(out_dir))} --threads {threads}\n"
            f"test -f {shlex.quote(str(report_tsv))}\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        attempt = build_frozen_attempt(
            attempt_id=f"{session_id}:assembly_review:quast:{safe_id}",
            session_id=session_id,
            stage="assembly_review",
            method_name=QUAST_METHOD,
            script_path=script_path,
            working_dir=working_dir,
            expected_outputs=[report_tsv],
            env=output.get("env"),
            env_spec=env_spec,
            input_dependencies=[
                artifact_input_dependency(
                    state,
                    verified.primary_output_ref,
                    assembly_ref,
                ),
                *reference_dependencies,
            ],
            metadata={
                "candidate_id": candidate_id,
                "draft_assembly_ref": str(assembly_ref),
                "quast_output_dir": str(out_dir),
                "report_tsv": str(report_tsv),
                "threads": threads,
                "reference_mode": (
                    "reference_based"
                    if reference_path is not None
                    else "reference_free"
                ),
                "reference_genome_path": (
                    str(reference_path) if reference_path is not None else None
                ),
                "reference_genome_asset_id": reference_asset_id,
            },
        )
        attempts.append(attempt)
    return freeze_method_attempts(
        state,
        LIFECYCLE,
        attempts,
        stage_updates={
            "signals": [
                AssemblyEvalSignal.model_validate(signal).model_dump(mode="json")
                for signal in signals
            ]
        },
    )


def collect_quast_artifacts(state: TopLevelState) -> StateUpdate:
    """Collect QUAST artifacts and update review signals."""
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
        notes = ["thresholds_not_applied_without_human_approval"]
        metadata: dict[str, Any] = {
            "attempt_id": attempt.attempt_id,
            "draft_assembly_ref": attempt.metadata.get("draft_assembly_ref"),
            "quast_output_dir": attempt.metadata.get("quast_output_dir"),
            "reference_mode": attempt.metadata.get("reference_mode"),
            "reference_genome_path": attempt.metadata.get("reference_genome_path"),
            "reference_genome_asset_id": attempt.metadata.get(
                "reference_genome_asset_id"
            ),
        }
        if result.success:
            artifact_metadata = {
                "attempt_id": attempt.attempt_id,
                "candidate_id": candidate_id,
            }
            report_tsv = publish_or_recover_work_artifact(
                data_dir,
                session_id,
                Path(attempt.metadata["report_tsv"]),
                stage="assembly_review",
                method_name=QUAST_METHOD,
                artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TSV,
                metadata=artifact_metadata,
            ).path
            artifacts.append(
                ArtifactMeta.from_file(
                    report_tsv,
                    stage="assembly_review",
                    method_name=QUAST_METHOD,
                    artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TSV,
                    metadata=artifact_metadata,
                )
            )
            metric_refs.append(str(report_tsv))
            report_txt = Path(attempt.metadata["quast_output_dir"]) / "report.txt"
            try:
                report_txt = publish_or_recover_work_artifact(
                    data_dir,
                    session_id,
                    report_txt,
                    stage="assembly_review",
                    method_name=QUAST_METHOD,
                    artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TXT,
                    metadata=artifact_metadata,
                ).path
            except FileNotFoundError:
                pass
            else:
                artifacts.append(
                    ArtifactMeta.from_file(
                        report_txt,
                        stage="assembly_review",
                        method_name=QUAST_METHOD,
                        artifact_type=ASSEMBLY_REVIEW_QUAST_REPORT_TXT,
                        metadata=artifact_metadata,
                    )
                )
                metric_refs.append(str(report_txt))
            metrics = parse_quast_report_tsv(report_tsv)
            summary_source = Path(attempt.working_dir) / "quast_summary.json"
            summary_source.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "method_name": QUAST_METHOD,
                        "candidate_id": candidate_id,
                        "attempt_id": attempt.attempt_id,
                        "draft_assembly_ref": attempt.metadata.get(
                            "draft_assembly_ref"
                        ),
                        "reference_mode": attempt.metadata.get("reference_mode"),
                        "reference_genome_path": attempt.metadata.get(
                            "reference_genome_path"
                        ),
                        "reference_genome_asset_id": attempt.metadata.get(
                            "reference_genome_asset_id"
                        ),
                        "report_tsv_ref": str(report_tsv),
                        "metrics": metrics,
                        "notes": notes,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            summary_path = publish_or_recover_work_artifact(
                data_dir,
                session_id,
                summary_source,
                stage="assembly_review",
                method_name=QUAST_METHOD,
                artifact_type=ASSEMBLY_REVIEW_QUAST_SUMMARY,
                metadata=artifact_metadata,
            ).path
            artifacts.append(
                ArtifactMeta.from_file(
                    summary_path,
                    stage="assembly_review",
                    method_name=QUAST_METHOD,
                    artifact_type=ASSEMBLY_REVIEW_QUAST_SUMMARY,
                    metadata=artifact_metadata,
                )
            )
            metric_refs.append(str(summary_path))
            payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
            ledger.record_attempt_succeeded(session_id, attempt, artifacts=payloads)
            record_registered_artifacts(
                data_dir,
                session_id,
                payloads,
                attempt_id=attempt.attempt_id,
            )
            artifact_refs.extend(str(artifact.file_path) for artifact in artifacts)
            metadata.update(
                {"metrics": metrics, "quast_summary_ref": str(summary_path)}
            )
            signals = update_signal(
                signals,
                signal_name="quast",
                method_name=QUAST_METHOD,
                candidate_id=candidate_id,
                status="available",
                metric_refs=metric_refs,
                notes=[*notes, "quast_executed"],
                metadata=metadata,
            )
        else:
            metadata.update({"return_code": result.return_code, "error": result.error})
            signals = update_signal(
                signals,
                signal_name="quast",
                method_name=QUAST_METHOD,
                candidate_id=candidate_id,
                status="failed",
                metric_refs=[],
                notes=["quast_execution_failed", "candidate_not_rejected_from_metrics"],
                metadata=metadata,
            )
    update = rewrite_eval_bundle(
        state,
        {**output, "signals": signals, "artifact_refs": ordered_unique(artifact_refs)},
        status="evaluating",
    )
    outputs = update["stage_outputs"]
    outputs["assembly_review"] = {
        **outputs["assembly_review"],
        DIRECT_ATTEMPTS_KEY: [],
    }
    return {"stage_outputs": outputs}


def quast_threads(output: AssemblyReviewCursor) -> tuple[int, str | None]:
    threads = (output.get("quast_parameters") or {}).get("threads", 1)
    return (
        (threads, None)
        if threads > 0
        else (0, "assembly_review.quast_parameters.threads must be a positive integer")
    )


def parse_quast_report_tsv(path: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    key_map = {
        "# contigs": "contig_count",
        "largest contig": "largest_contig",
        "total length": "total_length",
        "n50": "n50",
        "l50": "l50",
        "gc (%)": "gc_percent",
        "# n's per 100 kbp": "n_per_100kbp",
        "# misassemblies": "misassemblies",
        "# mismatches per 100 kbp": "mismatches_per_100kbp",
        "# indels per 100 kbp": "indels_per_100kbp",
        "genome fraction (%)": "genome_fraction_percent",
        "duplication ratio": "duplication_ratio",
        "nga50": "nga50",
    }
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
