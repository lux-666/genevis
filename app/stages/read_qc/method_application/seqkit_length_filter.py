"""SeqKit length-filter method subgraph."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import READ_QC_FILTERED_LONG_READS, READ_QC_LONG_READ_STATS, READ_QC_LONG_READ_SUMMARY, ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir
from app.stages.read_qc.constants import LENGTH_FILTER_DECISIONS, LENGTH_FILTER_METHOD_NAME, METHOD_NAME
from app.stages.read_qc.method_application.lifecycle import (
    ReadQCMethodLifecycle,
    build_method_graph,
    fail_update,
    filtered_long_reads_path,
    parse_seqkit_summary,
    read_paths_from_output,
    read_qc_output,
    resolve_read_qc_method_env,
    update_read_qc_output,
)

LIFECYCLE = ReadQCMethodLifecycle(
    name="seqkit seq",
    frozen_status="length_filter_frozen",
    ran_status="length_filter_ran",
    frozen_attempt_key="length_filter_frozen_attempt",
    execution_result_key="length_filter_execution_result",
)


def build_seqkit_length_filter_graph():
    return build_method_graph(LIFECYCLE, freeze_length_filter_attempt, collect_length_filter_artifacts)


def freeze_length_filter_attempt(state: TopLevelState) -> dict:
    output = read_qc_output(state)
    if output.get("status") != "length_filter_planned":
        return {}
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, blocked = resolve_read_qc_method_env(output, LENGTH_FILTER_METHOD_NAME, session_id=session_id, data_dir=data_dir, input_manifest_ref=state.get("input_manifest_ref", ""))
    if blocked is not None:
        return update_read_qc_output(state, blocked)
    min_len = int(output["substeps"]["length_filter"]["thresholds"]["min_len"])
    working_dir = data_dir / "work" / "read_qc" / session_id / "length_filter"
    artifact_dir = data_dir / "artifacts" / "read_qc" / session_id
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    input_paths = read_paths_from_output(output, "raw_long_reads_paths", "raw_long_reads_path")
    filtered_paths = [filtered_long_reads_path(artifact_dir, input_path, index=index, total=len(input_paths), fmt=output.get("format")) for index, input_path in enumerate(input_paths, start=1)]
    post_filter_stats_path = artifact_dir / "post_filter_seqkit_stats.tsv"
    script_path = working_dir / "run_length_filter.sh"
    commands = [f"seqkit seq -m {min_len} {shlex.quote(str(input_path))} > {shlex.quote(str(filtered_path))}" for input_path, filtered_path in zip(input_paths, filtered_paths)]
    script_path.write_text("set -e\n" + "\n".join(commands) + "\n" + f"seqkit stats -a -T {' '.join(shlex.quote(str(path)) for path in filtered_paths)} > {shlex.quote(str(post_filter_stats_path))}\n", encoding="utf-8")
    script_path.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:read_qc:length_filter", session_id=session_id, stage="read_qc", method_name=LENGTH_FILTER_METHOD_NAME,
        script_path=script_path, working_dir=working_dir, expected_outputs=[*filtered_paths, post_filter_stats_path], env=output.get("env"), env_spec=env_spec,
        metadata={"input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [],
                  "raw_long_reads_path": str(input_paths[0]), "raw_long_reads_paths": [str(path) for path in input_paths],
                  "filtered_long_reads_path": str(filtered_paths[0]), "filtered_long_reads_paths": [str(path) for path in filtered_paths],
                  "post_filter_stats_path": str(post_filter_stats_path), "thresholds": {"min_len": min_len}},
    )
    return update_read_qc_output(state, {"status": LIFECYCLE.frozen_status, LIFECYCLE.frozen_attempt_key: attempt.model_dump(mode="json")})


def collect_length_filter_artifacts(state: TopLevelState) -> dict:
    output = read_qc_output(state)
    if output.get("status") != LIFECYCLE.ran_status:
        return {}
    attempt = FrozenAttemptSpec.model_validate(output[LIFECYCLE.frozen_attempt_key])
    result = output[LIFECYCLE.execution_result_key]
    paths = [Path(path) for path in attempt.metadata["filtered_long_reads_paths"]]
    stats_path = Path(attempt.metadata["post_filter_stats_path"])
    missing = [str(path) for path in attempt.expected_outputs if not path.exists()]
    if missing:
        stdout = Path(result["stdout_path"]).read_text(encoding="utf-8") if Path(result["stdout_path"]).exists() else ""
        stderr = Path(result["stderr_path"]).read_text(encoding="utf-8") if Path(result["stderr_path"]).exists() else ""
        reason = f"seqkit seq expected outputs missing: {missing}"
        detail = "\n".join(part for part in [reason, stdout.strip(), stderr.strip()] if part)
        Ledger(state.get("data_dir", "data")).record_attempt_failed(attempt.session_id, attempt, return_code=0, error=detail)
        return fail_update(state, detail)
    min_len = attempt.metadata["thresholds"]["min_len"]
    artifacts = [ArtifactMeta.from_file(path, stage="read_qc", method_name=LENGTH_FILTER_METHOD_NAME, artifact_type=READ_QC_FILTERED_LONG_READS,
                                        metadata={"attempt_id": attempt.attempt_id, "input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [], "thresholds": {"min_len": min_len}, "tool": "seqkit"}) for path in paths]
    artifacts.append(ArtifactMeta.from_file(stats_path, stage="read_qc", method_name=METHOD_NAME, artifact_type=READ_QC_LONG_READ_STATS,
                                            metadata={"attempt_id": attempt.attempt_id, "input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [], "read_set": "post_filter", "format": "tsv", "thresholds": {"min_len": min_len}, "tool": "seqkit"}))
    summary = parse_seqkit_summary(stats_path)
    summary_path = stats_path.parent / "post_filter_read_qc_summary.json"
    summary_path.write_text(json.dumps({"session_id": attempt.session_id, "method_name": METHOD_NAME, "input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [], "raw_long_reads_path": output.get("raw_long_reads_path"), "raw_long_reads_paths": output.get("raw_long_reads_paths") or [], "filtered_long_reads_path": str(paths[0]), "filtered_long_reads_paths": [str(path) for path in paths], "thresholds": {"min_len": min_len}, "read_set": "post_filter", "summary": summary}, indent=2), encoding="utf-8")
    artifacts.append(ArtifactMeta.from_file(summary_path, stage="read_qc", method_name=METHOD_NAME, artifact_type=READ_QC_LONG_READ_SUMMARY,
                                            metadata={"attempt_id": attempt.attempt_id, "input_asset_ref": output.get("input_asset_ref"), "input_asset_refs": output.get("input_asset_refs") or [], "read_set": "post_filter", "format": "json", "thresholds": {"min_len": min_len}, "tool": "seqkit"}))
    for value, artifact_type in [(result["stdout_path"], "command.stdout"), (result["stderr_path"], "command.stderr")]:
        path = Path(value)
        if path.exists():
            artifacts.append(ArtifactMeta.from_file(path, stage="read_qc", method_name=LENGTH_FILTER_METHOD_NAME, artifact_type=artifact_type, metadata={"attempt_id": attempt.attempt_id}))
    data_dir = state.get("data_dir", "data")
    append_artifact_records(data_dir, attempt.session_id, artifacts)
    ledger = Ledger(data_dir)
    payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=payloads)
    for artifact in payloads:
        ledger.record_artifact_registered(attempt.session_id, artifact, attempt_id=attempt.attempt_id)
    substeps = dict(output.get("substeps") or {})
    substeps["length_filter"] = {**dict(substeps.get("length_filter") or {}), "name": "length_filter", "status": "executed", "method_name": LENGTH_FILTER_METHOD_NAME, "thresholds": {"min_len": min_len}, "artifact_ref": str(paths[0]), "artifact_refs": [str(path) for path in paths], "post_filter_stats_ref": str(stats_path), "post_filter_summary_ref": str(summary_path)}
    return update_read_qc_output(state, {"status": "length_filter_executed", "filtered_long_reads_ref": str(paths[0]), "filtered_long_reads_refs": [str(path) for path in paths], "post_filter_stats_artifact_ref": str(stats_path), "post_filter_summary_artifact_ref": str(summary_path), "post_filter_summary": summary, "summary": summary, "summary_artifact_ref": str(summary_path), "final_read_set": "post_filter", "artifact_refs": [*list(output.get("artifact_refs") or []), *[str(artifact.file_path) for artifact in artifacts]], "length_filter_method": LENGTH_FILTER_METHOD_NAME, "length_filter_decision": LENGTH_FILTER_DECISIONS[LENGTH_FILTER_METHOD_NAME], "substeps": substeps})
