"""SeqKit length-filter method subgraph."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.control.workflow_plan import current_workflow_step_id
from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    CompiledMethodGraph,
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    READ_QC_FILTERED_LONG_READS,
    READ_QC_LONG_READ_STATS,
    READ_QC_LONG_READ_SUMMARY,
    READ_QC_READ_SET_MANIFEST,
    ArtifactMeta,
    ArtifactRef,
)
from app.schemas.assets import ReadSetManifest, ReadSetMember
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, stage_work_dir
from app.stages.read_qc.constants import (
    LENGTH_FILTER_DECISIONS,
    LENGTH_FILTER_METHOD_NAME,
    METHOD_NAME,
)
from app.stages.read_qc.method_application.support import (
    fail_update,
    filtered_long_reads_path,
    parse_seqkit_summary,
    read_input_dependencies,
    read_paths_from_output,
    read_qc_output,
    update_read_qc_output,
)
from app.stages.read_qc.state import ReadQCSubstepJson
from app.storage.artifacts import (
    publish_or_recover_work_artifact,
    record_registered_artifacts,
)
from app.storage.ledger import Ledger

LIFECYCLE = MethodExecutionLifecycle(
    stage="read_qc",
    name="seqkit seq",
    method_name=LENGTH_FILTER_METHOD_NAME,
    frozen_status="length_filter_frozen",
    ran_status="length_filter_ran",
)


def build_seqkit_length_filter_graph() -> CompiledMethodGraph:
    return build_method_graph(
        LIFECYCLE,
        freeze_length_filter_attempt,
        collect_length_filter_artifacts,
    )


def freeze_length_filter_attempt(state: TopLevelState) -> StateUpdate:
    output = read_qc_output(state)
    if output.get("status") != "length_filter_planned":
        return {}
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        output,
        stage="read_qc",
        method_name=LENGTH_FILTER_METHOD_NAME,
        workflow_step_id=current_workflow_step_id(state, expected_stage="read_qc"),
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_read_qc_output(state, blocked)
    min_len = int(output["substeps"]["length_filter"]["thresholds"]["min_len"])
    working_dir = stage_work_dir(state, "read_qc", "length_filter")
    working_dir.mkdir(parents=True, exist_ok=True)
    input_paths = read_paths_from_output(output)
    filtered_paths = [
        filtered_long_reads_path(
            working_dir,
            input_path,
            index=index,
            total=len(input_paths),
            fmt=output.get("format"),
        )
        for index, input_path in enumerate(input_paths, start=1)
    ]
    post_filter_stats_path = working_dir / "post_filter_seqkit_stats.tsv"
    script_path = working_dir / "run_length_filter.sh"
    commands = [
        f"seqkit seq -m {min_len} {shlex.quote(str(input_path))} "
        f"> {shlex.quote(str(filtered_path))}"
        for input_path, filtered_path in zip(input_paths, filtered_paths)
    ]
    filtered_args = " ".join(shlex.quote(str(path)) for path in filtered_paths)
    script_path.write_text(
        "set -e\n"
        + "\n".join(commands)
        + "\n"
        + f"seqkit stats -a -T {filtered_args} "
        + f"> {shlex.quote(str(post_filter_stats_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    try:
        input_dependencies = read_input_dependencies(state, output, input_paths)
    except ValueError as exc:
        return fail_update(state, f"SeqKit filter input dependency error: {exc}")
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:read_qc:length_filter",
        session_id=session_id,
        stage="read_qc",
        method_name=LENGTH_FILTER_METHOD_NAME,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[*filtered_paths, post_filter_stats_path],
        env=output.get("env"),
        env_spec=env_spec,
        input_dependencies=input_dependencies,
        metadata={
            "input_asset_ref": output.get("input_asset_ref"),
            "input_asset_refs": list(output.get("input_asset_refs") or []),
            "input_member_platforms": list(output.get("input_member_platforms") or []),
            "input_member_formats": list(output.get("input_member_formats") or []),
            "raw_long_reads_path": str(input_paths[0]),
            "raw_long_reads_paths": [str(path) for path in input_paths],
            "filtered_long_reads_path": str(filtered_paths[0]),
            "filtered_long_reads_paths": [str(path) for path in filtered_paths],
            "post_filter_stats_path": str(post_filter_stats_path),
            "thresholds": {"min_len": min_len},
        },
    )
    return freeze_method_attempts(state, LIFECYCLE, [attempt])


def collect_length_filter_artifacts(state: TopLevelState) -> StateUpdate:
    output = read_qc_output(state)
    if output.get("status") != LIFECYCLE.ran_status:
        return {}
    executions = load_method_executions(state, LIFECYCLE)
    if len(executions) != 1:
        return fail_update(
            state, "SeqKit filter requires exactly one execution attempt"
        )
    attempt = executions[0].attempt
    paths = [Path(path) for path in attempt.metadata["filtered_long_reads_paths"]]
    stats_path = Path(attempt.metadata["post_filter_stats_path"])
    min_len = attempt.metadata["thresholds"]["min_len"]
    filtered_metadata = {
        "attempt_id": attempt.attempt_id,
        "input_asset_ref": output.get("input_asset_ref"),
        "input_asset_refs": output.get("input_asset_refs") or [],
        "thresholds": {"min_len": min_len},
        "tool": "seqkit",
    }
    published_reads = [
        publish_or_recover_work_artifact(
            state.get("data_dir", "data"),
            attempt.session_id,
            path,
            stage="read_qc",
            method_name=LENGTH_FILTER_METHOD_NAME,
            artifact_type=READ_QC_FILTERED_LONG_READS,
            metadata=filtered_metadata,
        )
        for path in paths
    ]
    paths = [published.path for published in published_reads]
    input_asset_ids = list(attempt.metadata.get("input_asset_refs") or [])
    input_platforms = list(attempt.metadata.get("input_member_platforms") or [])
    input_formats = list(attempt.metadata.get("input_member_formats") or [])
    expected_cardinality = len(paths)
    if not (
        len(input_asset_ids)
        == len(input_platforms)
        == len(input_formats)
        == expected_cardinality
    ):
        return fail_update(
            state,
            "SeqKit filter input/output provenance cardinality mismatch",
        )
    registry_ref = state.get("asset_registry_ref")
    if not isinstance(registry_ref, dict):
        return fail_update(
            state,
            "SeqKit filter cannot publish a read-set without asset_registry_ref",
        )
    read_set = ReadSetManifest(
        session_id=attempt.session_id,
        source_registry_ref=ArtifactRef.model_validate(registry_ref),
        members=[
            ReadSetMember(
                output_ref=published.ref,
                source_asset_id=input_asset_ids[index],
                platform=input_platforms[index],
                format=input_formats[index],
                transformation_method=LENGTH_FILTER_METHOD_NAME,
                attempt_id=attempt.attempt_id,
            )
            for index, published in enumerate(published_reads)
        ],
    )
    read_set_metadata = {
        "attempt_id": attempt.attempt_id,
        "member_count": len(paths),
        "dependency_refs": [
            registry_ref,
            *[published.ref.model_dump(mode="json") for published in published_reads],
        ],
    }
    read_set_source = (
        Path(attempt.working_dir) / "filtered_long_reads.read_set_manifest.json"
    )
    read_set_source.write_text(
        json.dumps(
            read_set.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    published_read_set = publish_or_recover_work_artifact(
        state.get("data_dir", "data"),
        attempt.session_id,
        read_set_source,
        stage="read_qc",
        method_name=LENGTH_FILTER_METHOD_NAME,
        artifact_type=READ_QC_READ_SET_MANIFEST,
        metadata=read_set_metadata,
    )
    stats_metadata = {
        "attempt_id": attempt.attempt_id,
        "input_asset_ref": output.get("input_asset_ref"),
        "input_asset_refs": output.get("input_asset_refs") or [],
        "read_set": "post_filter",
        "format": "tsv",
        "thresholds": {"min_len": min_len},
        "tool": "seqkit",
    }
    stats_path = publish_or_recover_work_artifact(
        state.get("data_dir", "data"),
        attempt.session_id,
        stats_path,
        stage="read_qc",
        method_name=METHOD_NAME,
        artifact_type=READ_QC_LONG_READ_STATS,
        metadata=stats_metadata,
    ).path
    summary = parse_seqkit_summary(stats_path)
    summary_source = Path(attempt.working_dir) / "post_filter_read_qc_summary.json"
    summary_source.write_text(
        json.dumps(
            {
                "session_id": attempt.session_id,
                "method_name": METHOD_NAME,
                "input_asset_ref": output.get("input_asset_ref"),
                "input_asset_refs": output.get("input_asset_refs") or [],
                "raw_long_reads_path": output.get("raw_long_reads_path"),
                "raw_long_reads_paths": output.get("raw_long_reads_paths") or [],
                "filtered_long_reads_path": str(paths[0]),
                "filtered_long_reads_paths": [str(path) for path in paths],
                "thresholds": {"min_len": min_len},
                "read_set": "post_filter",
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
        "read_set": "post_filter",
        "format": "json",
        "thresholds": {"min_len": min_len},
        "tool": "seqkit",
    }
    summary_path = publish_or_recover_work_artifact(
        state.get("data_dir", "data"),
        attempt.session_id,
        summary_source,
        stage="read_qc",
        method_name=METHOD_NAME,
        artifact_type=READ_QC_LONG_READ_SUMMARY,
        metadata=summary_metadata,
    ).path
    artifacts = [
        *[
            ArtifactMeta.from_file(
                path,
                stage="read_qc",
                method_name=LENGTH_FILTER_METHOD_NAME,
                artifact_type=READ_QC_FILTERED_LONG_READS,
                metadata=filtered_metadata,
            )
            for path in paths
        ],
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
        ArtifactMeta.from_file(
            published_read_set.path,
            stage="read_qc",
            method_name=LENGTH_FILTER_METHOD_NAME,
            artifact_type=READ_QC_READ_SET_MANIFEST,
            metadata=read_set_metadata,
        ),
    ]
    data_dir = state.get("data_dir", "data")
    ledger = Ledger(data_dir)
    payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=payloads)
    record_registered_artifacts(
        state.get("data_dir", "data"),
        attempt.session_id,
        payloads,
        attempt_id=attempt.attempt_id,
    )
    substeps = dict(output.get("substeps") or {})
    previous = substeps.get("length_filter")
    length_filter_step: ReadQCSubstepJson = (
        previous.copy() if previous is not None else {}
    )
    length_filter_step.update(
        {
            "name": "length_filter",
            "status": "executed",
            "method_name": LENGTH_FILTER_METHOD_NAME,
            "thresholds": {"min_len": min_len},
            "artifact_ref": str(paths[0]),
            "artifact_refs": [str(path) for path in paths],
            "post_filter_stats_ref": str(stats_path),
            "post_filter_summary_ref": str(summary_path),
        }
    )
    substeps["length_filter"] = length_filter_step
    return update_read_qc_output(
        state,
        {
            "status": "length_filter_executed",
            "filtered_long_reads_ref": str(paths[0]),
            "filtered_long_reads_refs": [str(path) for path in paths],
            "filtered_long_reads_artifact_refs": [
                published.ref.model_dump(mode="json") for published in published_reads
            ],
            "read_set_manifest_ref": published_read_set.ref.model_dump(mode="json"),
            "post_filter_stats_artifact_ref": str(stats_path),
            "post_filter_summary_artifact_ref": str(summary_path),
            "post_filter_summary": summary,
            "summary": summary,
            "summary_artifact_ref": str(summary_path),
            "final_read_set": "post_filter",
            "artifact_refs": [
                *list(output.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "length_filter_method": LENGTH_FILTER_METHOD_NAME,
            "length_filter_decision": LENGTH_FILTER_DECISIONS[
                LENGTH_FILTER_METHOD_NAME
            ],
            "substeps": substeps,
        },
    )
