"""Read-QC stage orchestration nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.graph.routing import next_stage_name
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.base import load_assets_from_registry, stage_output, update_stage_output
from app.stages.read_qc.constants import (
    LENGTH_FILTER_DECISIONS,
    LENGTH_FILTER_METHOD_NAME,
    LENGTH_FILTER_SKIP,
    METHOD_NAME,
)


def _read_qc_output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "read_qc")


def _update_read_qc_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "read_qc", payload)


def _length_filter_status_update(
    state: TopLevelState, payload: dict[str, Any], status: str, *, method_name: str
) -> dict:
    output = _read_qc_output(state)
    substeps = dict(output.get("substeps") or {})
    substeps["length_filter"] = {
        **dict(substeps.get("length_filter") or {}),
        "name": "length_filter",
        **payload,
    }
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


def _blocked_update(
    state: TopLevelState, status: str, reason: str, extra: dict[str, Any] | None = None
) -> dict:
    payload = {"status": status, "reason": reason}
    if extra:
        payload.update(extra)
    return _update_read_qc_output(state, payload)


def enter_read_qc(state: TopLevelState) -> dict:
    """Mark read QC as running."""
    return {"supervision_state": "running_stage"}


def resolve_raw_long_reads(state: TopLevelState) -> dict:
    """Resolve all raw-long-read assets into one ordered read set."""
    assets = load_assets_from_registry(state, ["raw_long_reads"])
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
    """Select SeqKit stats and decide whether it can run locally."""
    output = _read_qc_output(state)
    if output.get("status") != "resolved":
        return {}
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    if METHOD_NAME not in registry.methods:
        return _fail_update(state, f"descriptor missing: {METHOD_NAME}")
    update = {
        "status": "planned",
        "method_name": METHOD_NAME,
        "package_name": registry.methods[METHOD_NAME].package_ref,
    }
    if output.get("execution_mode") != "local":
        update["skip_reason"] = "local_execution_not_enabled"
        status = "skipped"
    else:
        status = "planned"
    update["substeps"] = {
        **dict(output.get("substeps") or {}),
        "long_read_stats": {
            "name": "long_read_stats",
            "status": status,
            "method_name": METHOD_NAME,
            **({"skip_reason": "local_execution_not_enabled"} if status == "skipped" else {}),
        },
    }
    return _update_read_qc_output(state, update)


def plan_length_filter(state: TopLevelState) -> dict:
    """Choose a filter method only for explicitly approved thresholds."""
    output = _read_qc_output(state)
    if output.get("status") == "failed":
        return {}
    request = dict(output.get("length_filter_request") or {})
    thresholds = dict(request.get("thresholds") or {})
    approved = bool(request.get("approved") or request.get("human_approved"))
    method_name = request.get("method_name") or request.get("method") or LENGTH_FILTER_SKIP
    if not request or method_name == LENGTH_FILTER_SKIP:
        return _length_filter_status_update(state, {"status": "skipped", "method_name": LENGTH_FILTER_SKIP, "skip_reason": "no_filtering_requested"}, "length_filter_skipped", method_name=LENGTH_FILTER_SKIP)
    if method_name != LENGTH_FILTER_METHOD_NAME:
        return _length_filter_status_update(state, {"status": "skipped", "method_name": method_name, "skip_reason": "unknown_filter_method", "thresholds": thresholds}, "length_filter_skipped", method_name=method_name)
    min_len = thresholds.get("min_len")
    if not approved or not isinstance(min_len, int) or min_len <= 0:
        return _length_filter_status_update(state, {"status": "skipped", "method_name": method_name, "skip_reason": "threshold_decision_missing", "thresholds": thresholds}, "length_filter_skipped", method_name=method_name)
    return _length_filter_status_update(state, {"status": "planned", "method_name": LENGTH_FILTER_METHOD_NAME, "thresholds": {"min_len": min_len}}, "length_filter_planned", method_name=LENGTH_FILTER_METHOD_NAME)


def commit_read_qc(state: TopLevelState) -> dict:
    """Advance to genome survey after read-QC planning or execution."""
    output = _read_qc_output(state)
    status = "completed" if output.get("status") in {"completed", "fofn_ready"} else output.get("status", "planned")
    committed_keys = [
        "artifact_refs", "assembly_input_ref", "blocked_input_ref", "env_target", "final_read_set",
        "filtered_long_reads_fofn_ref", "filtered_long_reads_ref", "filtered_long_reads_refs", "format",
        "input_asset_ref", "input_asset_refs", "long_reads_fofn_ref", "length_filter_decision",
        "length_filter_method", "method_name", "missing_tools", "package_name", "platform",
        "post_filter_stats_artifact_ref", "post_filter_summary", "post_filter_summary_artifact_ref",
        "raw_long_reads_paths", "raw_stats_artifact_ref", "raw_summary", "raw_summary_artifact_ref",
        "reason", "skip_reason", "stats_artifact_ref", "substeps", "summary", "summary_artifact_ref",
    ]
    outputs = dict(state.get("stage_outputs") or {})
    outputs["read_qc"] = {**{key: output[key] for key in committed_keys if key in output}, "status": status}
    return {
        "current_stage": next_stage_name("read_qc"),
        "supervision_state": "running_stage",
        "route_trace": [*state.get("route_trace", []), "read_qc_stage"],
        "stage_outputs": outputs,
    }


def fail_read_qc(state: TopLevelState) -> dict:
    """Stop the workflow on read-QC failure or preflight block."""
    output = _read_qc_output(state)
    status = output.get("status", "failed")
    default_reason = {
        "blocked_input": "read_qc blocked on missing input",
        "blocked_env": "read_qc blocked on missing conda environment",
        "missing_tool": "read_qc blocked on missing tool",
    }.get(status, "read_qc failed")
    return {"current_stage": "read_qc", "supervision_state": "failed_terminal", "last_error": output.get("reason") or default_reason}
