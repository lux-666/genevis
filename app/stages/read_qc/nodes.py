"""Read QC stage nodes for the first SeqKit stats slice."""

from __future__ import annotations

import csv
import json
import shlex
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.graph.routing import next_stage_name
from app.schemas.artifacts import (
    READ_QC_FILTERED_LONG_READS,
    READ_QC_FILTERED_LONG_READS_FOFN,
    READ_QC_LONG_READS_FOFN,
    READ_QC_LONG_READ_STATS,
    READ_QC_LONG_READ_SUMMARY,
    ArtifactMeta,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
from app.schemas.manifest import _detect_format
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir

METHOD_NAME = "read_qc.long_read_stats.seqkit_stats"
LENGTH_FILTER_METHOD_NAME = "read_qc.length_filter.seqkit_seq"
MAKE_FOFN_METHOD_NAME = "read_qc.make_fofn.local"
LENGTH_FILTER_SKIP = "skip"
LENGTH_FILTER_DECISIONS = {
    LENGTH_FILTER_SKIP: "skip filtering, use raw reads",
    "read_qc.length_filter.seqkit_seq": "seqkit_seq",
    "read_qc.length_filter.chopper": "chopper",
    "read_qc.length_filter.filtlong": "filtlong",
    "read_qc.length_filter.fastplong": "fastplong",
}


def _resolve_method_env(
    read_qc_out: dict[str, Any],
    method_name: str,
    *,
    session_id: str,
    data_dir: Path,
    input_manifest_ref: str,
):
    """Thin wrapper around the shared env dispatcher, scoped to read_qc."""
    return resolve_method_env(
        read_qc_out,
        stage="read_qc",
        method_name=method_name,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=input_manifest_ref,
    )


def _read_qc_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("read_qc", {}))


def _update_read_qc_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["read_qc"] = {**_read_qc_output(state), **payload}
    return {"stage_outputs": outputs}


def _substep_status_update(
    state: TopLevelState,
    name: str,
    payload: dict[str, Any],
    status: str,
) -> dict:
    read_qc_out = _read_qc_output(state)
    substeps = dict(read_qc_out.get("substeps") or {})
    substeps[name] = {**dict(substeps.get(name) or {}), "name": name, **payload}
    return _update_read_qc_output(state, {"status": status, "substeps": substeps})


def _length_filter_status_update(
    state: TopLevelState,
    payload: dict[str, Any],
    status: str,
    *,
    method_name: str,
) -> dict:
    read_qc_out = _read_qc_output(state)
    substeps = dict(read_qc_out.get("substeps") or {})
    substeps["length_filter"] = {**dict(substeps.get("length_filter") or {}), "name": "length_filter", **payload}
    return _update_read_qc_output(
        state,
        {
            "status": status,
            "length_filter_method": method_name,
            "length_filter_decision": LENGTH_FILTER_DECISIONS.get(method_name, str(method_name)),
            "substeps": substeps,
        },
    )


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_read_qc_output(state, {"status": "failed", "reason": reason})


def _blocked_update(state: TopLevelState, status: str, reason: str, extra: dict[str, Any] | None = None) -> dict:
    payload = {"status": status, "reason": reason}
    if extra:
        payload.update(extra)
    return _update_read_qc_output(state, payload)


def enter_read_qc(state: TopLevelState) -> dict:
    """Mark read QC as running."""
    return {"supervision_state": "running_stage"}


def resolve_raw_long_reads(state: TopLevelState) -> dict:
    """Resolve all raw_long_reads assets from the asset registry as one read set."""
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return _fail_update(state, "missing asset_registry_ref for read_qc")

    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return _fail_update(state, f"asset registry not found: {registry_ref}")

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assets = [asset for asset in registry.get("assets", []) if asset.get("asset_type") == "raw_long_reads"]
    if not assets:
        return _fail_update(state, "raw_long_reads asset not found in registry")

    paths = [Path(asset["path"]) for asset in assets]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        return _blocked_update(
            state,
            "blocked_input",
            f"raw_long_reads path not found: {missing[0]}",
            {"blocked_input_ref": missing[0], "blocked_input_refs": missing},
        )

    first_asset = assets[0]
    return _update_read_qc_output(
        state,
        {
            "status": "resolved",
            "input_asset_ref": first_asset.get("asset_id"),
            "input_asset_refs": [asset.get("asset_id") for asset in assets if asset.get("asset_id")],
            "raw_long_reads_path": str(paths[0]),
            "raw_long_reads_paths": [str(path) for path in paths],
            "platform": first_asset.get("platform"),
            "format": first_asset.get("format"),
        },
    )


def plan_seqkit_stats(state: TopLevelState) -> dict:
    """Load SeqKit descriptors and decide whether local execution is enabled."""
    read_qc_out = _read_qc_output(state)
    if read_qc_out.get("status") != "resolved":
        return {}

    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    if METHOD_NAME not in registry.methods:
        return _fail_update(state, f"descriptor missing: {METHOD_NAME}")

    update = {
        "status": "planned",
        "method_name": METHOD_NAME,
        "package_name": registry.methods[METHOD_NAME].package_ref,
    }
    if read_qc_out.get("execution_mode") != "local":
        update["skip_reason"] = "local_execution_not_enabled"
        update["substeps"] = {
            **dict(read_qc_out.get("substeps") or {}),
            "long_read_stats": {
                "name": "long_read_stats",
                "status": "skipped",
                "method_name": METHOD_NAME,
                "skip_reason": "local_execution_not_enabled",
            },
        }
    else:
        update["substeps"] = {
            **dict(read_qc_out.get("substeps") or {}),
            "long_read_stats": {
                "name": "long_read_stats",
                "status": "planned",
                "method_name": METHOD_NAME,
            },
        }
    return _update_read_qc_output(state, update)


def freeze_seqkit_attempt(state: TopLevelState) -> dict:
    """Write a deterministic SeqKit command script and freeze an attempt spec."""
    read_qc_out = _read_qc_output(state)
    if read_qc_out.get("status") != "planned":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, blocked = _resolve_method_env(
        read_qc_out,
        METHOD_NAME,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_read_qc_output(state, blocked)

    attempt_id = f"{session_id}:read_qc:seqkit_stats"
    working_dir = data_dir / "work" / "read_qc" / session_id / "seqkit_stats"
    artifact_dir = data_dir / "artifacts" / "read_qc" / session_id
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    input_paths = _read_paths_from_output(read_qc_out, "raw_long_reads_paths", "raw_long_reads_path")
    stats_path = artifact_dir / "seqkit_stats.tsv"
    script_path = working_dir / "run_seqkit_stats.sh"
    reads_arg = " ".join(shlex.quote(str(path)) for path in input_paths)
    script_path.write_text(
        "set -e\n"
        f"seqkit stats -a -T {reads_arg} > {shlex.quote(str(stats_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=attempt_id,
        session_id=session_id,
        stage="read_qc",
        method_name=METHOD_NAME,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[stats_path],
        env=read_qc_out.get("env"),
        env_spec=env_spec,
        metadata={
            "input_asset_ref": read_qc_out.get("input_asset_ref"),
            "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
            "raw_long_reads_path": str(input_paths[0]),
            "raw_long_reads_paths": [str(path) for path in input_paths],
            "stats_path": str(stats_path),
        },
    )
    return _update_read_qc_output(
        state,
        {
            "status": "frozen",
            "frozen_attempt": attempt.model_dump(mode="json"),
            "stats_output_path": str(stats_path),
        },
    )


def run_seqkit_stats(state: TopLevelState) -> dict:
    """Run the frozen SeqKit attempt locally."""
    read_qc_out = _read_qc_output(state)
    if read_qc_out.get("status") != "frozen":
        return {}

    attempt = FrozenAttemptSpec.model_validate(read_qc_out["frozen_attempt"])
    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label="seqkit stats",
    )
    if not outcome.success:
        return _update_read_qc_output(
            state,
            {
                "status": "failed",
                "reason": outcome.error,
                "execution_result": outcome.result.model_dump(mode="json"),
            },
        )

    return _update_read_qc_output(
        state,
        {
            "status": "ran",
            "execution_result": outcome.result.model_dump(mode="json"),
        },
    )


def collect_seqkit_artifacts(state: TopLevelState) -> dict:
    """Collect SeqKit stats and command artifacts."""
    read_qc_out = _read_qc_output(state)
    if read_qc_out.get("status") != "ran":
        return {}

    attempt = FrozenAttemptSpec.model_validate(read_qc_out["frozen_attempt"])

    stats_path = Path(read_qc_out["stats_output_path"])
    execution_result = read_qc_out["execution_result"]
    stdout_path = Path(execution_result["stdout_path"])
    stderr_path = Path(execution_result["stderr_path"])
    if not stats_path.exists():
        reason = f"expected SeqKit stats output missing: {stats_path}"
        Ledger(state.get("data_dir", "data")).record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=0,
            error=reason,
        )
        return _update_read_qc_output(state, {"status": "failed", "reason": reason})

    artifacts = [
        ArtifactMeta.from_file(
            stats_path,
            stage="read_qc",
            method_name=METHOD_NAME,
            artifact_type=READ_QC_LONG_READ_STATS,
            metadata={
                "attempt_id": attempt.attempt_id,
                "input_asset_ref": read_qc_out.get("input_asset_ref"),
                "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
                "format": "tsv",
                "tool": "seqkit",
            },
        )
    ]

    summary = _parse_seqkit_summary(stats_path)
    summary_path = stats_path.parent / "read_qc_summary.json"
    summary_payload = {
        "session_id": attempt.session_id,
        "method_name": METHOD_NAME,
        "input_asset_ref": read_qc_out.get("input_asset_ref"),
        "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
        "raw_long_reads_path": read_qc_out.get("raw_long_reads_path"),
        "raw_long_reads_paths": read_qc_out.get("raw_long_reads_paths") or [],
        "summary": summary,
    }
    summary_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    artifacts.append(
        ArtifactMeta.from_file(
            summary_path,
            stage="read_qc",
            method_name=METHOD_NAME,
            artifact_type=READ_QC_LONG_READ_SUMMARY,
            metadata={
                "attempt_id": attempt.attempt_id,
                "input_asset_ref": read_qc_out.get("input_asset_ref"),
                "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
                "format": "json",
                "tool": "seqkit",
            },
        )
    )

    for path, artifact_type in [
        (stdout_path, "command.stdout"),
        (stderr_path, "command.stderr"),
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="read_qc",
                    method_name=METHOD_NAME,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    append_artifact_records(state.get("data_dir", "data"), attempt.session_id, artifacts)
    ledger = Ledger(state.get("data_dir", "data"))
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(attempt.session_id, artifact, attempt_id=attempt.attempt_id)

    return _update_read_qc_output(
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
                **dict(read_qc_out.get("substeps") or {}),
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


def plan_length_filter(state: TopLevelState) -> dict:
    """Plan length filtering only when an explicit approved threshold is present."""
    read_qc_out = _read_qc_output(state)
    if read_qc_out.get("status") == "failed":
        return {}

    request = dict(read_qc_out.get("length_filter_request") or {})
    thresholds = dict(request.get("thresholds") or {})
    approved = bool(request.get("approved") or request.get("human_approved"))
    method_name = request.get("method_name") or request.get("method") or LENGTH_FILTER_SKIP

    if not request or method_name == LENGTH_FILTER_SKIP:
        return _length_filter_status_update(
            state,
            {
                "status": "skipped",
                "method_name": LENGTH_FILTER_SKIP,
                "skip_reason": "no_filtering_requested",
            },
            "length_filter_skipped",
            method_name=LENGTH_FILTER_SKIP,
        )

    if method_name not in LENGTH_FILTER_DECISIONS:
        return _length_filter_status_update(
            state,
            {
                "status": "skipped",
                "method_name": method_name,
                "skip_reason": "unknown_filter_method",
                "thresholds": thresholds,
            },
            "length_filter_skipped",
            method_name=method_name,
        )

    if method_name != LENGTH_FILTER_METHOD_NAME:
        return _length_filter_status_update(
            state,
            {
                "status": "skipped",
                "method_name": method_name,
                "skip_reason": "filter_method_not_executable",
                "thresholds": thresholds,
            },
            "length_filter_skipped",
            method_name=method_name,
        )

    min_len = thresholds.get("min_len")
    if not approved or not isinstance(min_len, int) or min_len <= 0:
        return _length_filter_status_update(
            state,
            {
                "status": "skipped",
                "method_name": method_name,
                "skip_reason": "threshold_decision_missing",
                "thresholds": thresholds,
            },
            "length_filter_skipped",
            method_name=method_name,
        )

    return _length_filter_status_update(
        state,
        {
            "status": "planned",
            "method_name": LENGTH_FILTER_METHOD_NAME,
            "thresholds": {"min_len": min_len},
        },
        "length_filter_planned",
        method_name=LENGTH_FILTER_METHOD_NAME,
    )


def maybe_run_length_filter(state: TopLevelState) -> dict:
    """Run SeqKit length filtering only for explicit approved thresholds."""
    read_qc_out = _read_qc_output(state)
    if read_qc_out.get("status") != "length_filter_planned":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, blocked = _resolve_method_env(
        read_qc_out,
        LENGTH_FILTER_METHOD_NAME,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_read_qc_output(state, blocked)

    substep = read_qc_out["substeps"]["length_filter"]
    min_len = int(substep["thresholds"]["min_len"])
    working_dir = data_dir / "work" / "read_qc" / session_id / "length_filter"
    artifact_dir = data_dir / "artifacts" / "read_qc" / session_id
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    input_paths = _read_paths_from_output(read_qc_out, "raw_long_reads_paths", "raw_long_reads_path")
    filtered_paths = [
        _filtered_long_reads_path(artifact_dir, input_path, index=index, total=len(input_paths), fmt=read_qc_out.get("format"))
        for index, input_path in enumerate(input_paths, start=1)
    ]
    post_filter_stats_path = artifact_dir / "post_filter_seqkit_stats.tsv"
    script_path = working_dir / "run_length_filter.sh"
    filter_commands = [
        f"seqkit seq -m {min_len} {shlex.quote(str(input_path))} > {shlex.quote(str(filtered_path))}"
        for input_path, filtered_path in zip(input_paths, filtered_paths)
    ]
    filtered_reads_arg = " ".join(shlex.quote(str(path)) for path in filtered_paths)
    script_path.write_text(
        "set -e\n"
        + "\n".join(filter_commands)
        + "\n"
        + f"seqkit stats -a -T {filtered_reads_arg} > {shlex.quote(str(post_filter_stats_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:read_qc:length_filter",
        session_id=session_id,
        stage="read_qc",
        method_name=LENGTH_FILTER_METHOD_NAME,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[*filtered_paths, post_filter_stats_path],
        env=read_qc_out.get("env"),
        env_spec=env_spec,
        metadata={
            "input_asset_ref": read_qc_out.get("input_asset_ref"),
            "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
            "raw_long_reads_path": str(input_paths[0]),
            "raw_long_reads_paths": [str(path) for path in input_paths],
            "filtered_long_reads_path": str(filtered_paths[0]),
            "filtered_long_reads_paths": [str(path) for path in filtered_paths],
            "post_filter_stats_path": str(post_filter_stats_path),
            "thresholds": {"min_len": min_len},
        },
    )

    outcome = run_frozen_attempt(
        attempt,
        data_dir=state.get("data_dir", "data"),
        input_manifest_ref=state.get("input_manifest_ref", ""),
        fail_label="seqkit seq",
    )
    if not outcome.success:
        return _update_read_qc_output(state, {"status": "failed", "reason": outcome.error})
    result = outcome.result
    ledger = Ledger(data_dir)
    missing_outputs = [str(path) for path in attempt.expected_outputs if not path.exists()]
    if missing_outputs:
        stdout = result.stdout_path.read_text(encoding="utf-8") if result.stdout_path.exists() else ""
        stderr = result.stderr_path.read_text(encoding="utf-8") if result.stderr_path.exists() else ""
        reason = f"seqkit seq expected outputs missing: {missing_outputs}"
        detail = "\n".join(part for part in [reason, stdout.strip(), stderr.strip()] if part)
        ledger.record_attempt_failed(session_id, attempt, return_code=0, error=detail)
        return _update_read_qc_output(state, {"status": "failed", "reason": detail})

    artifacts = [
        ArtifactMeta.from_file(
            filtered_path,
            stage="read_qc",
            method_name=LENGTH_FILTER_METHOD_NAME,
            artifact_type=READ_QC_FILTERED_LONG_READS,
            metadata={
                "attempt_id": attempt.attempt_id,
                "input_asset_ref": read_qc_out.get("input_asset_ref"),
                "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
                "thresholds": {"min_len": min_len},
                "tool": "seqkit",
            },
        )
        for filtered_path in filtered_paths
    ]
    artifacts.append(
        ArtifactMeta.from_file(
            post_filter_stats_path,
            stage="read_qc",
            method_name=METHOD_NAME,
            artifact_type=READ_QC_LONG_READ_STATS,
            metadata={
                "attempt_id": attempt.attempt_id,
                "input_asset_ref": read_qc_out.get("input_asset_ref"),
                "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
                "read_set": "post_filter",
                "format": "tsv",
                "thresholds": {"min_len": min_len},
                "tool": "seqkit",
            },
        )
    )
    post_filter_summary = _parse_seqkit_summary(post_filter_stats_path)
    post_filter_summary_path = artifact_dir / "post_filter_read_qc_summary.json"
    post_filter_summary_payload = {
        "session_id": attempt.session_id,
        "method_name": METHOD_NAME,
        "input_asset_ref": read_qc_out.get("input_asset_ref"),
        "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
        "raw_long_reads_path": read_qc_out.get("raw_long_reads_path"),
        "raw_long_reads_paths": read_qc_out.get("raw_long_reads_paths") or [],
        "filtered_long_reads_path": str(filtered_paths[0]),
        "filtered_long_reads_paths": [str(path) for path in filtered_paths],
        "thresholds": {"min_len": min_len},
        "read_set": "post_filter",
        "summary": post_filter_summary,
    }
    post_filter_summary_path.write_text(json.dumps(post_filter_summary_payload, indent=2), encoding="utf-8")
    artifacts.append(
        ArtifactMeta.from_file(
            post_filter_summary_path,
            stage="read_qc",
            method_name=METHOD_NAME,
            artifact_type=READ_QC_LONG_READ_SUMMARY,
            metadata={
                "attempt_id": attempt.attempt_id,
                "input_asset_ref": read_qc_out.get("input_asset_ref"),
                "input_asset_refs": read_qc_out.get("input_asset_refs") or [],
                "read_set": "post_filter",
                "format": "json",
                "thresholds": {"min_len": min_len},
                "tool": "seqkit",
            },
        )
    )
    for path, artifact_type in [
        (result.stdout_path, "command.stdout"),
        (result.stderr_path, "command.stderr"),
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="read_qc",
                    method_name=LENGTH_FILTER_METHOD_NAME,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )

    append_artifact_records(data_dir, session_id, artifacts)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(session_id, artifact, attempt_id=attempt.attempt_id)

    substeps = dict(read_qc_out.get("substeps") or {})
    substeps["length_filter"] = {
        **dict(substeps.get("length_filter") or {}),
        "name": "length_filter",
        "status": "executed",
        "method_name": LENGTH_FILTER_METHOD_NAME,
        "thresholds": {"min_len": min_len},
        "artifact_ref": str(filtered_paths[0]),
        "artifact_refs": [str(path) for path in filtered_paths],
        "post_filter_stats_ref": str(post_filter_stats_path),
        "post_filter_summary_ref": str(post_filter_summary_path),
    }
    return _update_read_qc_output(
        state,
        {
            "status": "length_filter_executed",
            "filtered_long_reads_ref": str(filtered_paths[0]),
            "filtered_long_reads_refs": [str(path) for path in filtered_paths],
            "post_filter_stats_artifact_ref": str(post_filter_stats_path),
            "post_filter_summary_artifact_ref": str(post_filter_summary_path),
            "post_filter_summary": post_filter_summary,
            "summary": post_filter_summary,
            "summary_artifact_ref": str(post_filter_summary_path),
            "final_read_set": "post_filter",
            "artifact_refs": [
                *list(read_qc_out.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "length_filter_method": LENGTH_FILTER_METHOD_NAME,
            "length_filter_decision": LENGTH_FILTER_DECISIONS[LENGTH_FILTER_METHOD_NAME],
            "substeps": substeps,
        },
    )


def make_fofn(state: TopLevelState) -> dict:
    """Write a deterministic FOFN for raw or filtered long reads."""
    read_qc_out = _read_qc_output(state)
    if read_qc_out.get("status") not in {"length_filter_skipped", "length_filter_executed"}:
        return {}

    session_id = state["session_id"]
    data_dir = Path(state.get("data_dir", "data"))
    artifact_dir = data_dir / "artifacts" / "read_qc" / session_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    if read_qc_out.get("filtered_long_reads_ref"):
        input_paths = _read_paths_from_output(read_qc_out, "filtered_long_reads_refs", "filtered_long_reads_ref")
        fofn_path = artifact_dir / "filtered_long_reads.fofn"
        artifact_type = READ_QC_FILTERED_LONG_READS_FOFN
        ref_key = "filtered_long_reads_fofn_ref"
    else:
        input_paths = _read_paths_from_output(read_qc_out, "raw_long_reads_paths", "raw_long_reads_path")
        fofn_path = artifact_dir / "long_reads.fofn"
        artifact_type = READ_QC_LONG_READS_FOFN
        ref_key = "long_reads_fofn_ref"

    fofn_path.write_text("".join(f"{path}\n" for path in input_paths), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        fofn_path,
        stage="read_qc",
        method_name=MAKE_FOFN_METHOD_NAME,
        artifact_type=artifact_type,
        metadata={
            "source_read_ref": str(input_paths[0]),
            "source_read_refs": [str(path) for path in input_paths],
        },
    )
    append_artifact_records(data_dir, session_id, [artifact])
    ledger = Ledger(data_dir)
    ledger.create_session(session_id, state.get("input_manifest_ref", ""))
    ledger.record_artifact_registered(session_id, artifact.model_dump(mode="json"))

    substeps = dict(read_qc_out.get("substeps") or {})
    substeps["make_fofn"] = {
        "name": "make_fofn",
        "status": "executed",
        "method_name": MAKE_FOFN_METHOD_NAME,
        "artifact_ref": str(fofn_path),
    }
    return _update_read_qc_output(
        state,
        {
            "status": "fofn_ready",
            ref_key: str(fofn_path),
            "assembly_input_ref": str(fofn_path),
            "substeps": substeps,
        },
    )


def commit_read_qc(state: TopLevelState) -> dict:
    """Advance to genome_survey after read QC planning or execution."""
    read_qc_out = _read_qc_output(state)
    trace = [*state.get("route_trace", []), "read_qc_stage"]
    next_stage = next_stage_name("read_qc")
    status = (
        "completed"
        if read_qc_out.get("status") in {"completed", "fofn_ready"}
        else read_qc_out.get("status", "planned")
    )
    committed_keys = [
        "artifact_refs",
        "assembly_input_ref",
        "blocked_input_ref",
        "env_target",
        "final_read_set",
        "filtered_long_reads_fofn_ref",
        "filtered_long_reads_ref",
        "filtered_long_reads_refs",
        "format",
        "input_asset_ref",
        "input_asset_refs",
        "long_reads_fofn_ref",
        "length_filter_decision",
        "length_filter_method",
        "method_name",
        "missing_tools",
        "package_name",
        "platform",
        "post_filter_stats_artifact_ref",
        "post_filter_summary",
        "post_filter_summary_artifact_ref",
        "raw_long_reads_paths",
        "raw_stats_artifact_ref",
        "raw_summary",
        "raw_summary_artifact_ref",
        "reason",
        "skip_reason",
        "stats_artifact_ref",
        "substeps",
        "summary",
        "summary_artifact_ref",
    ]
    committed_read_qc = {key: read_qc_out[key] for key in committed_keys if key in read_qc_out}
    outputs = dict(state.get("stage_outputs") or {})
    outputs["read_qc"] = {**committed_read_qc, "status": status}
    return {
        "current_stage": next_stage,
        "supervision_state": "running_stage",
        "route_trace": trace,
        "stage_outputs": outputs,
    }


def fail_read_qc(state: TopLevelState) -> dict:
    """Stop the workflow on read QC failure or preflight block."""
    read_qc_out = _read_qc_output(state)
    status = read_qc_out.get("status", "failed")
    default_reason = {
        "blocked_input": "read_qc blocked on missing input",
        "blocked_env": "read_qc blocked on missing conda environment",
        "missing_tool": "read_qc blocked on missing tool",
    }.get(status, "read_qc failed")
    reason = read_qc_out.get("reason") or default_reason
    return {
        "current_stage": "read_qc",
        "supervision_state": "failed_terminal",
        "last_error": reason,
    }


def _parse_seqkit_summary(stats_path: Path) -> dict[str, Any]:
    """Parse compact summary fields from SeqKit TSV output."""
    with stats_path.open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows:
        return {}
    if len(rows) == 1:
        row = rows[0]
        sum_len = _parse_int(row.get("sum_len"))
        return {
            "num_seqs": _parse_int(row.get("num_seqs")),
            "sum_len": sum_len,
            "total_bases": sum_len,
            "min_len": _parse_int(row.get("min_len")),
            "avg_len": _parse_float(row.get("avg_len")),
            "max_len": _parse_int(row.get("max_len")),
            "n50": _parse_int(_row_value(row, "N50", "n50")),
            "q20_percent": _parse_float(_row_value(row, "Q20(%)", "q20_percent", "Q20")),
            "q30_percent": _parse_float(_row_value(row, "Q30(%)", "q30_percent", "Q30")),
            "avg_qual": _parse_float(_row_value(row, "AvgQual", "avg_qual")),
        }

    num_seqs = _sum_ints(row.get("num_seqs") for row in rows)
    sum_len = _sum_ints(row.get("sum_len") for row in rows)
    min_lens = [_parse_int(row.get("min_len")) for row in rows]
    max_lens = [_parse_int(row.get("max_len")) for row in rows]
    return {
        "files": len(rows),
        "num_seqs": num_seqs,
        "sum_len": sum_len,
        "total_bases": sum_len,
        "min_len": min((value for value in min_lens if value is not None), default=None),
        "avg_len": (sum_len / num_seqs) if num_seqs and sum_len is not None else None,
        "max_len": max((value for value in max_lens if value is not None), default=None),
        "n50": _max_int(_row_value(row, "N50", "n50") for row in rows),
        "q20_percent": _weighted_average(rows, "Q20(%)", "q20_percent", "Q20"),
        "q30_percent": _weighted_average(rows, "Q30(%)", "q30_percent", "Q30"),
        "avg_qual": _weighted_average(rows, "AvgQual", "avg_qual"),
    }


def _read_paths_from_output(read_qc_out: dict[str, Any], plural_key: str, single_key: str) -> list[Path]:
    values = read_qc_out.get(plural_key)
    if values:
        return [Path(str(value)) for value in values]
    value = read_qc_out.get(single_key)
    return [Path(str(value))] if value else []


def _filtered_long_reads_path(
    artifact_dir: Path,
    input_path: Path,
    *,
    index: int,
    total: int,
    fmt: str | None,
) -> Path:
    resolved_format = fmt or _detect_format(input_path)
    ext = ".fasta" if resolved_format == "fasta" else ".fastq"
    if total == 1:
        return artifact_dir / f"filtered_long_reads{ext}"
    return artifact_dir / f"filtered_long_reads_{index}{ext}"


def _row_value(row: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        if key in row:
            return row[key]
    return None


def _parse_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value.replace(",", ""))


def _parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value.replace(",", ""))


def _sum_ints(values) -> int | None:
    parsed = [_parse_int(value) for value in values]
    present = [value for value in parsed if value is not None]
    if not present:
        return None
    return sum(present)


def _max_int(values) -> int | None:
    parsed = [_parse_int(value) for value in values]
    present = [value for value in parsed if value is not None]
    if not present:
        return None
    return max(present)


def _weighted_average(rows: list[dict[str, str]], *keys: str) -> float | None:
    total_weight = 0
    weighted_sum = 0.0
    for row in rows:
        value = _parse_float(_row_value(row, *keys))
        weight = _parse_int(row.get("sum_len")) or _parse_int(row.get("num_seqs")) or 0
        if value is None or weight <= 0:
            continue
        total_weight += weight
        weighted_sum += value * weight
    if total_weight == 0:
        return None
    return weighted_sum / total_weight
