"""Read-QC stage orchestration nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.configuration import load_descriptor_registry
from app.control.workflow_plan import advance_workflow_step
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    append_route_trace,
)
from app.stages.read_qc.constants import (
    LENGTH_FILTER_DECISIONS,
    LENGTH_FILTER_METHOD_NAME,
    LENGTH_FILTER_SKIP,
    METHOD_NAME,
)
from app.stages.read_qc.state import (
    ReadQCStageCursor,
    ReadQCSubstepJson,
    read_read_qc_stage_output,
    replace_read_qc_stage_output,
    serialize_read_qc_stage_output,
    update_read_qc_stage_output,
)
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import LONG_READS_SPEC


def _read_qc_output(state: TopLevelState) -> ReadQCStageCursor:
    return read_read_qc_stage_output(state)


def _update_read_qc_output(
    state: TopLevelState,
    payload: dict[str, Any],
) -> StateUpdate:
    return update_read_qc_stage_output(state, payload)


def _length_filter_status_update(
    state: TopLevelState,
    payload: ReadQCSubstepJson,
    status: str,
    *,
    method_name: str,
) -> StateUpdate:
    output = _read_qc_output(state)
    substeps = dict(output.get("substeps") or {})
    previous = substeps.get("length_filter")
    length_filter_step: ReadQCSubstepJson = (
        previous.copy() if previous is not None else {}
    )
    length_filter_step["name"] = "length_filter"
    length_filter_step.update(payload)
    substeps["length_filter"] = length_filter_step
    return _update_read_qc_output(
        state,
        {
            "status": status,
            "length_filter_method": method_name,
            "length_filter_decision": LENGTH_FILTER_DECISIONS.get(
                method_name, str(method_name)
            ),
            "substeps": substeps,
        },
    )


def _fail_update(state: TopLevelState, reason: str) -> StateUpdate:
    return _update_read_qc_output(state, {"status": "failed", "reason": reason})


def _blocked_update(
    state: TopLevelState, status: str, reason: str, extra: dict[str, Any] | None = None
) -> StateUpdate:
    payload = {"status": status, "reason": reason}
    if extra:
        payload.update(extra)
    return _update_read_qc_output(state, payload)


def enter_read_qc(state: TopLevelState) -> StateUpdate:
    """Mark read QC as running."""
    return {"supervision_state": "running_stage"}


def resolve_raw_long_reads(state: TopLevelState) -> StateUpdate:
    """Resolve the current logical long-read set."""
    try:
        resolution = resolve_stage_resource(state, resource_spec=LONG_READS_SPEC)
    except ValueError as exc:
        return _fail_update(state, f"raw_long_reads resolution failed: {exc}")
    if not resolution.found:
        return _fail_update(state, "raw_long_reads/long_reads resource not found")
    paths = [Path(path) for path in resolution.paths]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        return _blocked_update(
            state,
            "blocked_input",
            f"raw_long_reads path not found: {missing[0]}",
            {"blocked_input_ref": missing[0], "blocked_input_refs": missing},
        )
    metadata = resolution.metadata
    asset_ids = list(metadata.get("asset_ids") or [])
    if not asset_ids:
        asset_ids = list(metadata.get("source_asset_ids") or [])
    artifact_refs = list(metadata.get("artifact_refs") or [])
    runtime_assets = [
        asset
        for asset in (metadata.get("assets") or [])
        if isinstance(asset, dict)
    ]
    platforms = [
        str(asset.get("platform") or "unknown")
        for asset in runtime_assets
    ] or [
        str(value)
        for value in (
            metadata.get("member_platforms") or metadata.get("platforms") or []
        )
    ]
    formats = [
        str(asset.get("format") or "unknown")
        for asset in runtime_assets
    ] or [
        str(value)
        for value in (
            metadata.get("member_formats") or metadata.get("formats") or []
        )
    ]
    unique_platforms = list(dict.fromkeys(platforms))
    unique_formats = list(dict.fromkeys(formats))
    return _update_read_qc_output(
        state,
        {
            "status": "resolved",
            "input_asset_ref": asset_ids[0] if asset_ids else None,
            "input_asset_refs": asset_ids,
            "input_artifact_refs": artifact_refs,
            "input_member_platforms": platforms,
            "input_member_formats": formats,
            "raw_long_reads_path": str(paths[0]),
            "raw_long_reads_paths": [str(path) for path in paths],
            "platform": (
                unique_platforms[0] if len(unique_platforms) == 1 else None
            ),
            "format": unique_formats[0] if len(unique_formats) == 1 else None,
            "input_source": resolution.source,
        },
    )


def plan_seqkit_stats(state: TopLevelState) -> StateUpdate:
    """Select SeqKit stats and decide whether it can run locally."""
    output = _read_qc_output(state)
    if output.get("status") != "resolved":
        return {}
    registry = load_descriptor_registry()
    if METHOD_NAME not in registry.methods:
        return _fail_update(state, f"descriptor missing: {METHOD_NAME}")
    update: dict[str, Any] = {
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
            **(
                {"skip_reason": "local_execution_not_enabled"}
                if status == "skipped"
                else {}
            ),
        },
    }
    return _update_read_qc_output(state, update)


def plan_length_filter(state: TopLevelState) -> StateUpdate:
    """Choose a filter method only for explicitly approved thresholds."""
    output = _read_qc_output(state)
    if output.get("status") == "failed":
        return {}
    request = output.get("length_filter_request") or {}
    thresholds = request.get("thresholds") or {}
    approved = bool(request.get("approved") or request.get("human_approved"))
    method_name = (
        request.get("method_name") or request.get("method") or LENGTH_FILTER_SKIP
    )
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
    if output.get("execution_mode") != "local":
        return _length_filter_status_update(
            state,
            {
                "status": "skipped",
                "method_name": method_name,
                "skip_reason": "local_execution_not_enabled",
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
                "skip_reason": "unknown_filter_method",
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


def commit_read_qc(state: TopLevelState) -> StateUpdate:
    """Advance to genome survey after read-QC planning or execution."""
    output = _read_qc_output(state)
    status = "completed"
    committed_keys = [
        "artifact_refs",
        "blocked_input_ref",
        "env_target",
        "final_read_set",
        "filtered_long_reads_ref",
        "filtered_long_reads_refs",
        "filtered_long_reads_artifact_refs",
        "format",
        "input_asset_ref",
        "input_asset_refs",
        "input_artifact_refs",
        "input_member_formats",
        "input_member_platforms",
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
        "read_set_manifest_ref",
        "raw_summary",
        "raw_summary_artifact_ref",
        "reason",
        "skip_reason",
        "stats_artifact_ref",
        "substeps",
        "summary",
        "summary_artifact_ref",
    ]
    serialized_output = serialize_read_qc_stage_output(output)
    cursor_update = replace_read_qc_stage_output(
        state,
        {
            **{
                key: serialized_output[key]
                for key in committed_keys
                if key in serialized_output
            },
            "status": status,
        },
    )
    update: StateUpdate = {
        **advance_workflow_step(state, "read_qc"),
        "route_trace": append_route_trace(state, "read_qc_stage"),
        "stage_outputs": cursor_update["stage_outputs"],
    }
    read_paths = _committed_long_read_paths(output)
    if read_paths:
        filtered = bool(
            output.get("filtered_long_reads_ref")
            or output.get("filtered_long_reads_refs")
        )
        metadata: dict[str, Any] = {
            "platforms": list(
                dict.fromkeys(output.get("input_member_platforms") or [])
            ),
            "formats": list(
                dict.fromkeys(output.get("input_member_formats") or [])
            ),
            "member_platforms": list(output.get("input_member_platforms") or []),
            "member_formats": list(output.get("input_member_formats") or []),
            "member_count": len(read_paths),
            "filtered": filtered,
        }
        if filtered:
            if not output.get("read_set_manifest_ref"):
                raise ValueError(
                    "filtered read-QC output requires read_set_manifest_ref"
                )
            metadata["read_set_manifest_ref"] = output["read_set_manifest_ref"]
            metadata["artifact_refs"] = list(
                output.get("filtered_long_reads_artifact_refs") or []
            )
            metadata["source_asset_ids"] = list(
                output.get("input_asset_refs") or []
            )
        else:
            metadata["asset_registry_ref"] = state.get("asset_registry_ref")
            metadata["asset_ids"] = list(output.get("input_asset_refs") or [])
        update["resource_refs"] = update_resource_ref(
            state,
            "long_reads",
            read_paths,
            "read_qc",
            metadata,
        )
    return update


def _committed_long_read_paths(output: ReadQCStageCursor) -> list[str]:
    """Return concrete reads for the central table, never a FOFN path."""

    refs = output.get("filtered_long_reads_refs")
    if isinstance(refs, list) and refs:
        return [str(path) for path in refs if path]
    if output.get("filtered_long_reads_ref"):
        return [str(output["filtered_long_reads_ref"])]
    return [str(path) for path in output.get("raw_long_reads_paths") or [] if path]


def fail_read_qc(state: TopLevelState) -> StateUpdate:
    """Stop the workflow on read-QC failure or preflight block."""
    output = _read_qc_output(state)
    status = output.get("status", "failed")
    default_reason = {
        "blocked_input": "read_qc blocked on missing input",
        "blocked_env": "read_qc blocked on missing conda environment",
        "missing_tool": "read_qc blocked on missing tool",
    }.get(status, "read_qc failed")
    return {
        "current_stage": "read_qc",
        "supervision_state": "failed_terminal",
        "last_error": output.get("reason") or default_reason,
    }
