"""SeqKit long-read statistics method subgraph."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    READ_QC_LONG_READ_STATS,
    READ_QC_LONG_READ_SUMMARY,
    ArtifactMeta,
)
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_work_dir
from app.stages.read_qc.constants import METHOD_NAME
from app.stages.read_qc.method_application.support import (
    fail_update,
    parse_seqkit_summary,
    read_paths_from_output,
    read_qc_output,
    update_read_qc_output,
)
from app.storage.artifacts import publish_or_recover_work_artifact
from app.storage.ledger import Ledger

LIFECYCLE = MethodExecutionLifecycle(
    stage="read_qc",
    name="seqkit stats",
    method_name=METHOD_NAME,
    frozen_status="frozen",
    ran_status="ran",
)


def build_seqkit_stats_graph():
    return build_method_graph(
        LIFECYCLE,
        freeze_seqkit_attempt,
        collect_seqkit_artifacts,
    )


def freeze_seqkit_attempt(state: TopLevelState) -> dict:
    output = read_qc_output(state)
    if output.get("status") != "planned":
        return {}
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        output, stage="read_qc", method_name=METHOD_NAME, session_id=session_id, data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_read_qc_output(state, blocked)
    working_dir = stage_work_dir(state, "read_qc", "seqkit_stats")
    working_dir.mkdir(parents=True, exist_ok=True)
    input_paths = read_paths_from_output(output, "raw_long_reads_paths", "raw_long_reads_path")
    stats_path = working_dir / "seqkit_stats.tsv"
    script_path = working_dir / "run_seqkit_stats.sh"
    script_path.write_text(
        "set -e\n" + f"seqkit stats -a -T {' '.join(shlex.quote(str(path)) for path in input_paths)} > {shlex.quote(str(stats_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:read_qc:seqkit_stats", session_id=session_id, stage="read_qc",
        method_name=METHOD_NAME, script_path=script_path, working_dir=working_dir,
        expected_outputs=[stats_path], env=output.get("env"), env_spec=env_spec,
        metadata={"input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [],
                  "raw_long_reads_path": str(input_paths[0]), "raw_long_reads_paths": [str(path) for path in input_paths],
                  "stats_path": str(stats_path)},
    )
    return freeze_method_attempts(
        state,
        LIFECYCLE,
        [attempt],
        stage_updates={"stats_output_path": str(stats_path)},
    )


def collect_seqkit_artifacts(state: TopLevelState) -> dict:
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
    summary = parse_seqkit_summary(stats_path)
    summary_source = Path(attempt.working_dir) / "read_qc_summary.json"
    summary_source.write_text(json.dumps({"session_id": attempt.session_id, "method_name": METHOD_NAME,
                                        "input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [],
                                        "raw_long_reads_path": output.get("raw_long_reads_path"), "raw_long_reads_paths": output.get("raw_long_reads_paths") or [],
                                        "summary": summary}, indent=2), encoding="utf-8")
    summary_metadata = {
        "attempt_id": attempt.attempt_id,
        "input_asset_ref": output.get("input_asset_ref"),
        "input_asset_refs": output.get("input_asset_refs") or [],
        "format": "json",
        "tool": "seqkit",
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
    for artifact in payloads:
        ledger.record_artifact_registered(attempt.session_id, artifact, attempt_id=attempt.attempt_id)
    return update_read_qc_output(state, {
        "status": "completed", "stats_artifact_ref": str(stats_path), "summary_artifact_ref": str(summary_path),
        "raw_stats_artifact_ref": str(stats_path), "raw_summary_artifact_ref": str(summary_path), "final_read_set": "raw",
        "artifact_refs": [str(artifact.file_path) for artifact in artifacts], "summary": summary, "raw_summary": summary,
        "substeps": {**dict(output.get("substeps") or {}), "long_read_stats": {"name": "long_read_stats", "status": "executed",
                     "method_name": METHOD_NAME, "artifact_ref": str(stats_path), "summary_ref": str(summary_path)}},
    })
