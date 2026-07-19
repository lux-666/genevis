"""SeqKit long-read statistics method subgraph."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.storage.ledger import Ledger
from app.storage.artifacts import publish_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.evidence import (
    load_execution_evidence,
    load_frozen_attempt,
    persist_frozen_attempt,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    READ_QC_LONG_READ_STATS,
    READ_QC_LONG_READ_SUMMARY,
    ArtifactMeta,
)
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.stages.read_qc.constants import METHOD_NAME
from app.stages.read_qc.method_application.lifecycle import (
    ReadQCMethodLifecycle,
    build_method_graph,
    fail_update,
    parse_seqkit_summary,
    read_paths_from_output,
    read_qc_output,
    update_read_qc_output,
)

LIFECYCLE = ReadQCMethodLifecycle(
    name="seqkit stats",
    frozen_status="frozen",
    ran_status="ran",
    frozen_attempt_key="frozen_attempt_ref",
    execution_result_key="execution_result",
)


def build_seqkit_stats_graph():
    return build_method_graph(LIFECYCLE, freeze_seqkit_attempt, collect_seqkit_artifacts)


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
    artifact_dir = stage_artifacts_dir(state, "read_qc")
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    input_paths = read_paths_from_output(output, "raw_long_reads_paths", "raw_long_reads_path")
    stats_path = artifact_dir / "seqkit_stats.tsv"
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
    attempt_ref = persist_frozen_attempt(state, attempt)
    return update_read_qc_output(
        state,
        {
            "status": LIFECYCLE.frozen_status,
            LIFECYCLE.frozen_attempt_key: attempt_ref.model_dump(mode="json"),
            "stats_output_path": str(stats_path),
        },
    )


def collect_seqkit_artifacts(state: TopLevelState) -> dict:
    output = read_qc_output(state)
    if output.get("status") != LIFECYCLE.ran_status:
        return {}
    attempt = load_frozen_attempt(
        state,
        output[LIFECYCLE.frozen_attempt_key],
        expected_stage="read_qc",
        expected_method_name=METHOD_NAME,
    )
    stats_path = Path(output["stats_output_path"])
    load_execution_evidence(
        state,
        output["execution_result"],
        expected_stage="read_qc",
        expected_attempt_id=attempt.attempt_id,
        expected_method_name=METHOD_NAME,
    )
    if not stats_path.exists():
        reason = f"expected SeqKit stats output missing: {stats_path}"
        Ledger(state.get("data_dir", "data")).record_attempt_failed(attempt.session_id, attempt, return_code=0, error=reason)
        return fail_update(state, reason)
    artifacts = [
        ArtifactMeta.from_file(stats_path, stage="read_qc", method_name=METHOD_NAME, artifact_type=READ_QC_LONG_READ_STATS,
                               metadata={"attempt_id": attempt.attempt_id, "input_asset_ref": output.get("input_asset_ref"),
                                         "input_asset_refs": output.get("input_asset_refs") or [], "format": "tsv", "tool": "seqkit"})
    ]
    summary = parse_seqkit_summary(stats_path)
    summary_path = stats_path.parent / "read_qc_summary.json"
    summary_path.write_text(json.dumps({"session_id": attempt.session_id, "method_name": METHOD_NAME,
                                        "input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [],
                                        "raw_long_reads_path": output.get("raw_long_reads_path"), "raw_long_reads_paths": output.get("raw_long_reads_paths") or [],
                                        "summary": summary}, indent=2), encoding="utf-8")
    artifacts.append(ArtifactMeta.from_file(summary_path, stage="read_qc", method_name=METHOD_NAME, artifact_type=READ_QC_LONG_READ_SUMMARY,
                                             metadata={"attempt_id": attempt.attempt_id, "input_asset_ref": output.get("input_asset_ref"),
                                                       "input_asset_refs": output.get("input_asset_refs") or [], "format": "json", "tool": "seqkit"}))
    publish_artifact_records(state.get("data_dir", "data"), attempt.session_id, artifacts)
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
