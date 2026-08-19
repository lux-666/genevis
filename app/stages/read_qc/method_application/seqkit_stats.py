"""SeqKit long-read statistics method subgraph."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path

from app.control.workflow_plan import current_workflow_step, current_workflow_step_id
from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    CompiledMethodGraph,
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import MethodOutcome
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    READ_QC_LONG_READ_AGGREGATE_STATS,
    READ_QC_LONG_READ_STATS,
    READ_QC_LONG_READ_SUMMARY,
    ArtifactMeta,
)
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, stage_work_dir
from app.stages.read_qc.constants import METHOD_NAME
from app.stages.read_qc.method_application.support import (
    direct_read_set_dependency_refs,
    direct_read_set_provenance,
    fail_update,
    parse_seqkit_summary,
    read_input_dependencies,
    read_paths_from_output,
    read_qc_output,
    seqkit_aggregate_length_commands,
    update_read_qc_output,
)
from app.storage.artifacts import (
    publish_or_recover_work_artifact,
    record_registered_artifacts,
)
from app.storage.ledger import Ledger
from app.workflow import is_terminal_failure


def seqkit_stats_outcome(state: TopLevelState) -> MethodOutcome | None:
    """Return SeqKit stats' terminal disposition for this fixed stage method."""

    if not _is_selected(state):
        return None
    output = read_qc_output(state)
    substep = (output.get("substeps") or {}).get("long_read_stats") or {}
    if substep.get("status") == "executed":
        return MethodOutcome(METHOD_NAME, "completed")
    if substep.get("status") == "skipped":
        return MethodOutcome(
            METHOD_NAME,
            "skipped",
            reason=str(substep.get("skip_reason") or "method_not_run"),
        )
    if is_terminal_failure(output.get("status"), extra_statuses={"blocked_input"}):
        if not _has_execution_result(output, METHOD_NAME):
            return MethodOutcome(METHOD_NAME, "not_started")
        return MethodOutcome(
            METHOD_NAME,
            "failed",
            reason=str(output.get("reason") or "seqkit_stats_failed"),
        )
    return None


def _is_selected(state: TopLevelState) -> bool:
    step = current_workflow_step(state)
    roster = step.get("selected_method_names") if step is not None else None
    return isinstance(roster, list) and METHOD_NAME in roster


def _has_execution_result(output: Mapping[str, object], method_name: str) -> bool:
    attempts = output.get("execution_attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        attempt.get("method_id") == method_name
        and attempt.get("status") in {"executed", "execution_failed"}
        for attempt in attempts
        if isinstance(attempt, Mapping)
    )


LIFECYCLE = MethodExecutionLifecycle(
    stage="read_qc",
    name="seqkit stats",
    method_name=METHOD_NAME,
    frozen_status="frozen",
    ran_status="ran",
    outcome_policy=seqkit_stats_outcome,
)


def build_seqkit_stats_graph() -> CompiledMethodGraph:
    return build_method_graph(
        LIFECYCLE,
        freeze_seqkit_attempt,
        collect_seqkit_artifacts,
    )


def freeze_seqkit_attempt(state: TopLevelState) -> StateUpdate:
    output = read_qc_output(state)
    if output.get("status") != "planned":
        return {}
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        output,
        stage="read_qc",
        method_name=METHOD_NAME,
        workflow_step_id=current_workflow_step_id(state, expected_stage="read_qc"),
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_read_qc_output(state, blocked)
    working_dir = stage_work_dir(state, "read_qc", "seqkit_stats")
    working_dir.mkdir(parents=True, exist_ok=True)
    input_paths = read_paths_from_output(output)
    try:
        input_dependencies = read_input_dependencies(state, output, input_paths)
    except ValueError as exc:
        return fail_update(state, f"SeqKit stats input dependency error: {exc}")
    stats_path = working_dir / "seqkit_stats.tsv"
    aggregate_stats_path = working_dir / "aggregate_seqkit_stats.tsv"
    script_path = working_dir / "run_seqkit_stats.sh"
    input_args = " ".join(shlex.quote(str(path)) for path in input_paths)
    aggregate_command = seqkit_aggregate_length_commands(
        input_args,
        aggregate_stats_path=aggregate_stats_path,
    )
    script_path.write_text(
        "set -euo pipefail\n"
        + f"seqkit stats -a -T {input_args} "
        + f"> {shlex.quote(str(stats_path))}\n"
        + aggregate_command,
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:read_qc:seqkit_stats",
        session_id=session_id,
        stage="read_qc",
        method_name=METHOD_NAME,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[
            stats_path,
            aggregate_stats_path,
        ],
        env=output.get("env"),
        env_spec=env_spec,
        input_dependencies=input_dependencies,
        metadata={
            "input_asset_ref": output.get("input_asset_ref"),
            "input_asset_refs": output.get("input_asset_refs") or [],
            "raw_long_reads_path": str(input_paths[0]),
            "raw_long_reads_paths": [str(path) for path in input_paths],
            "stats_path": str(stats_path),
            "aggregate_stats_path": str(aggregate_stats_path),
        },
    )
    return freeze_method_attempts(
        state,
        LIFECYCLE,
        [attempt],
        stage_updates={"stats_output_path": str(stats_path)},
    )


def collect_seqkit_artifacts(state: TopLevelState) -> StateUpdate:
    output = read_qc_output(state)
    if output.get("status") != LIFECYCLE.ran_status:
        return {}
    executions = load_method_executions(state, LIFECYCLE)
    if len(executions) != 1:
        return fail_update(state, "SeqKit stats requires exactly one execution attempt")
    attempt = executions[0].attempt
    stats_path = Path(output["stats_output_path"])
    stats_metadata = {
        "attempt_id": attempt.attempt_id,
        "input_asset_ref": output.get("input_asset_ref"),
        "input_asset_refs": output.get("input_asset_refs") or [],
        "format": "tsv",
        "tool": "seqkit",
    }
    published_stats = publish_or_recover_work_artifact(
        state.get("data_dir", "data"),
        attempt.session_id,
        stats_path,
        stage="read_qc",
        method_name=METHOD_NAME,
        artifact_type=READ_QC_LONG_READ_STATS,
        metadata=stats_metadata,
    )
    stats_path = published_stats.path
    aggregate_stats = attempt.metadata.get("aggregate_stats_path")
    if not isinstance(aggregate_stats, str) or not aggregate_stats:
        return fail_update(
            state,
            "SeqKit stats requires a direct whole-read-set aggregate",
        )
    aggregate_metadata = {
        **stats_metadata,
        "aggregation": "whole_read_set_direct_input_stream",
        "dependency_refs": direct_read_set_dependency_refs(
            attempt.input_dependencies,
            asset_registry_ref=state.get("asset_registry_ref"),
        ),
        "provenance": direct_read_set_provenance(
            attempt.input_dependencies,
            read_set="raw",
        ),
    }
    published_aggregate = publish_or_recover_work_artifact(
        state.get("data_dir", "data"),
        attempt.session_id,
        Path(aggregate_stats),
        stage="read_qc",
        method_name=METHOD_NAME,
        artifact_type=READ_QC_LONG_READ_AGGREGATE_STATS,
        metadata=aggregate_metadata,
    )
    summary = parse_seqkit_summary(
        stats_path,
        aggregate_stats_path=published_aggregate.path,
    )
    summary_source = Path(attempt.working_dir) / "read_qc_summary.json"
    summary_source.write_text(
        json.dumps(
            {
                "session_id": attempt.session_id,
                "method_name": METHOD_NAME,
                "input_asset_ref": output.get("input_asset_ref"),
                "input_asset_refs": output.get("input_asset_refs") or [],
                "raw_long_reads_path": output.get("raw_long_reads_path"),
                "raw_long_reads_paths": output.get("raw_long_reads_paths") or [],
                "aggregate_stats_artifact_ref": published_aggregate.ref.model_dump(
                    mode="json"
                ),
                "summary": summary,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    summary_metadata = {
        "attempt_id": attempt.attempt_id,
        "input_asset_ref": output.get("input_asset_ref"),
        "input_asset_refs": output.get("input_asset_refs") or [],
        "format": "json",
        "tool": "seqkit",
        "aggregate_stats_ref": published_aggregate.ref.model_dump(mode="json"),
        "dependency_refs": [published_aggregate.ref.model_dump(mode="json")],
    }
    published_summary = publish_or_recover_work_artifact(
        state.get("data_dir", "data"),
        attempt.session_id,
        summary_source,
        stage="read_qc",
        method_name=METHOD_NAME,
        artifact_type=READ_QC_LONG_READ_SUMMARY,
        metadata=summary_metadata,
    )
    summary_path = published_summary.path
    artifacts = [
        ArtifactMeta.from_file(
            stats_path,
            stage="read_qc",
            method_name=METHOD_NAME,
            artifact_type=READ_QC_LONG_READ_STATS,
            metadata=stats_metadata,
        ),
        ArtifactMeta.from_file(
            published_aggregate.path,
            stage="read_qc",
            method_name=METHOD_NAME,
            artifact_type=READ_QC_LONG_READ_AGGREGATE_STATS,
            metadata=aggregate_metadata,
        ),
        ArtifactMeta.from_file(
            summary_path,
            stage="read_qc",
            method_name=METHOD_NAME,
            artifact_type=READ_QC_LONG_READ_SUMMARY,
            metadata=summary_metadata,
        ),
    ]
    ledger = Ledger(state.get("data_dir", "data"))
    payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=payloads)
    record_registered_artifacts(
        state.get("data_dir", "data"),
        attempt.session_id,
        payloads,
        attempt_id=attempt.attempt_id,
    )
    return update_read_qc_output(
        state,
        {
            "status": "completed",
            "stats_artifact_ref": str(stats_path),
            "summary_artifact_ref": str(summary_path),
            "raw_stats_artifact_ref": str(stats_path),
            "raw_summary_artifact_ref": str(summary_path),
            "final_read_set": "raw",
            "artifact_refs": [str(artifact.file_path) for artifact in artifacts],
            "summary": summary,
            "raw_summary": summary,
            "substeps": {
                **dict(output.get("substeps") or {}),
                "long_read_stats": {
                    "name": "long_read_stats",
                    "status": "executed",
                    "method_name": METHOD_NAME,
                    "artifact_ref": str(stats_path),
                    "summary_ref": str(summary_path),
                },
            },
        },
    )
