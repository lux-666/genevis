"""Read-QC stage orchestration nodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from app.configuration import load_descriptor_registry
from app.control.workflow_plan import (
    advance_workflow_step,
    current_workflow_step,
    fail_workflow_step,
)
from app.execution.method_outcomes import MethodOutcome
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import (
    append_route_trace,
)
from app.stages.read_qc.constants import (
    LENGTH_FILTER_DECISIONS,
    LENGTH_FILTER_METHOD_NAME,
    LENGTH_FILTER_SKIP,
    LENGTH_FILTER_SKIP_METHOD_NAME,
    METHOD_NAME,
)
from app.stages.read_qc.pairs import resolve_qc_pairs
from app.stages.read_qc.state import (
    ReadQCStageCursor,
    ReadQCSubstepJson,
    read_read_qc_stage_output,
    replace_read_qc_stage_output,
    serialize_read_qc_stage_output,
    update_read_qc_stage_output,
)
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import HIC_READS_SPEC, LONG_READS_SPEC, SHORT_READS_SPEC


def _read_qc_output(state: TopLevelState) -> ReadQCStageCursor:
    return read_read_qc_stage_output(state)


def _update_read_qc_output(
    state: TopLevelState,
    payload: dict[str, Any],
) -> StateUpdate:
    return update_read_qc_stage_output(state, payload)


def _pair_list(output: ReadQCStageCursor, key: str) -> list[dict[str, Any]]:
    value = output.get(key)
    return cast(list[dict[str, Any]], value) if isinstance(value, list) else []


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


def length_filter_skip_outcome(state: TopLevelState) -> MethodOutcome | None:
    """Settle the selected planner-only raw-read pass-through method."""

    step = current_workflow_step(state)
    if (
        step is None
        or LENGTH_FILTER_SKIP_METHOD_NAME not in step["selected_method_names"]
    ):
        return None
    output = _read_qc_output(state)
    substep = (output.get("substeps") or {}).get("length_filter") or {}
    if (
        substep.get("method_name") == LENGTH_FILTER_SKIP_METHOD_NAME
        and substep.get("status") == "skipped"
    ):
        return MethodOutcome(LENGTH_FILTER_SKIP_METHOD_NAME, "completed")
    if output.get("status") in {
        "failed",
        "blocked_env",
        "missing_tool",
        "blocked_input",
    }:
        return MethodOutcome(LENGTH_FILTER_SKIP_METHOD_NAME, "not_started")
    return None


def enter_read_qc(state: TopLevelState) -> StateUpdate:
    """Mark read QC as running."""
    return {"supervision_state": "running_stage"}


def resolve_raw_long_reads(state: TopLevelState) -> StateUpdate:
    """Resolve every read family that this QC occurrence can consume."""
    try:
        resolution = resolve_stage_resource(state, resource_spec=LONG_READS_SPEC)
    except ValueError as exc:
        return _fail_update(state, f"long_reads resolution failed: {exc}")
    if not resolution.found:
        paths: list[Path] = []
        metadata: dict[str, Any] = {}
    else:
        paths = [Path(path) for path in resolution.paths]
        metadata = resolution.metadata
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        return _blocked_update(
            state,
            "blocked_input",
            f"raw_long_reads path not found: {missing[0]}",
            {"blocked_input_ref": missing[0], "blocked_input_refs": missing},
        )
    registry_backed = (
        bool(paths)
        and bool(metadata.get("asset_ids"))
        and (
            metadata.get("asset_registry_ref") is not None
            or (
                resolution is not None
                and resolution.source_type == "asset"
                and state.get("asset_registry_ref") is not None
            )
        )
    )
    artifact_backed = metadata.get("read_set_manifest_ref") is not None and bool(
        metadata.get("artifact_refs")
    )
    if paths and not registry_backed and not artifact_backed:
        return _fail_update(
            state,
            "long_reads resource has no immutable registry or read-set provenance",
        )
    asset_ids = list(metadata.get("asset_ids") or [])
    if not asset_ids:
        asset_ids = list(metadata.get("source_asset_ids") or [])
    artifact_refs = list(metadata.get("artifact_refs") or [])
    runtime_assets = [
        asset for asset in (metadata.get("assets") or []) if isinstance(asset, dict)
    ]
    platforms = [
        str(asset.get("platform") or "unknown") for asset in runtime_assets
    ] or [
        str(value)
        for value in (
            metadata.get("member_platforms") or metadata.get("platforms") or []
        )
    ]
    formats = [str(asset.get("format") or "unknown") for asset in runtime_assets] or [
        str(value)
        for value in (metadata.get("member_formats") or metadata.get("formats") or [])
    ]
    unique_platforms = list(dict.fromkeys(platforms))
    unique_formats = list(dict.fromkeys(formats))
    families: list[str] = []
    if paths:
        families.append("long_reads")
    family_errors: list[str] = []
    for family, spec in (
        ("short_reads", SHORT_READS_SPEC),
        ("hic_reads", HIC_READS_SPEC),
    ):
        try:
            family_resolution = resolve_stage_resource(state, resource_spec=spec)
            if family_resolution.found:
                pairs = resolve_qc_pairs(state, family)
                if not pairs:
                    return _fail_update(
                        state, f"{family} resource has no complete R1/R2 pairs"
                    )
                families.append(family)
        except ValueError as exc:
            detail = str(exc)
            if family == "short_reads" and "asset_registry_ref is missing" in detail:
                detail += " (paired members require pair_index/mate provenance)"
            selected_method = {
                "short_reads": "read_qc.short_reads.fastp",
                "hic_reads": "read_qc.hic_reads.fastp",
            }[family]
            current_step = current_workflow_step(state)
            if (
                current_step is not None
                and selected_method in current_step["selected_method_names"]
            ):
                return _fail_update(
                    state,
                    f"{family} resolution failed for selected fastp method: {detail}",
                )
            family_errors.append(f"{family} resolution failed: {detail}")
    if not families:
        errors: list[str] = []
        if not resolution.found:
            errors.append("raw_long_reads resource was not found")
        errors.extend(family_errors)
        return _fail_update(
            state,
            "; ".join(errors) if errors else "read_qc found no supported read family",
        )
    return _update_read_qc_output(
        state,
        {
            "status": "resolved",
            "input_asset_ref": asset_ids[0] if asset_ids else None,
            "input_asset_refs": asset_ids,
            "input_artifact_refs": artifact_refs,
            "input_member_platforms": platforms,
            "input_member_formats": formats,
            "raw_long_reads_path": str(paths[0]) if paths else "",
            "raw_long_reads_paths": [str(path) for path in paths],
            "platform": (unique_platforms[0] if len(unique_platforms) == 1 else None),
            "format": unique_formats[0] if len(unique_formats) == 1 else None,
            "input_source": resolution.source if resolution is not None else "missing",
            "input_families": families,
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
    if "long_reads" not in (output.get("input_families") or []):
        update["skip_reason"] = "no_long_reads_resource"
        status = "skipped"
    elif output.get("execution_mode") != "local":
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
                {"skip_reason": str(update["skip_reason"])}
                if status == "skipped"
                else {}
            ),
        },
    }
    return _update_read_qc_output(state, update)


def plan_fastp_family(state: TopLevelState, family: str) -> StateUpdate:
    """Plan the configured paired-read QC method for one data family."""
    output = _read_qc_output(state)
    method_name = {
        "short_reads": "read_qc.short_reads.fastp",
        "hic_reads": "read_qc.hic_reads.fastp",
    }[family]
    step = current_workflow_step(state)
    selected = step["selected_method_names"] if step is not None else []
    if method_name not in selected:
        return {}
    if output.get("status") in {"failed", "blocked_input"}:
        return {}
    request = output.get(f"{family}_request") or {}
    if family not in (output.get("input_families") or []):
        return _update_read_qc_output(
            state,
            {
                "status": f"{family}_fastp_executed",
                "substeps": {
                    **dict(output.get("substeps") or {}),
                    family: {
                        "name": family,
                        "status": "skipped",
                        "method_name": method_name,
                        "skip_reason": f"no_{family}_resource",
                    },
                },
            },
        )
    if output.get("execution_mode") != "local":
        return _update_read_qc_output(
            state,
            {
                "status": f"{family}_fastp_executed",
                "skip_reason": "local_execution_not_enabled",
                "substeps": {
                    **dict(output.get("substeps") or {}),
                    family: {
                        "name": family,
                        "status": "skipped",
                        "method_name": method_name,
                        "skip_reason": "local_execution_not_enabled",
                    },
                },
            },
        )
    if not request:
        return _fail_update(
            state, f"{family} fastp input exists but configuration is missing"
        )
    return _update_read_qc_output(
        state,
        {"status": f"{family}_fastp_planned", "method_name": method_name},
    )


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
    if not request or method_name in {
        LENGTH_FILTER_SKIP,
        LENGTH_FILTER_SKIP_METHOD_NAME,
    }:
        if LENGTH_FILTER_SKIP_METHOD_NAME not in load_descriptor_registry().methods:
            return _fail_update(
                state,
                f"descriptor missing: {LENGTH_FILTER_SKIP_METHOD_NAME}",
            )
        return _length_filter_status_update(
            state,
            {
                "status": "skipped",
                "method_name": LENGTH_FILTER_SKIP_METHOD_NAME,
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
        return _fail_update(
            state,
            f"unsupported length filter method: {method_name}",
        )
    if LENGTH_FILTER_METHOD_NAME not in load_descriptor_registry().methods:
        return _fail_update(state, f"descriptor missing: {LENGTH_FILTER_METHOD_NAME}")
    if "long_reads" not in (output.get("input_families") or []):
        return _fail_update(
            state,
            "length filtering was approved but no long-read resource is available",
        )
    min_len = thresholds.get("min_len")
    platform_min_len = thresholds.get("platform_min_len") or {}
    platforms = list(output.get("input_member_platforms") or [])
    paths = list(output.get("raw_long_reads_paths") or [])
    resolved_min_len: list[int] = []
    if platform_min_len:
        for platform in platforms:
            value = platform_min_len.get(platform, min_len)
            if not isinstance(value, int) or value <= 0:
                return _fail_update(
                    state,
                    "length filter threshold missing for long-read platform "
                    f"{platform!r}",
                )
            resolved_min_len.append(value)
        if len(resolved_min_len) != len(paths):
            return _fail_update(
                state,
                "length filter platform threshold cardinality does not match "
                "input reads",
            )
    elif isinstance(min_len, int) and min_len > 0:
        resolved_min_len = [min_len] * len(paths)
    if not approved or not resolved_min_len:
        return _length_filter_status_update(
            state,
            {
                "status": "skipped",
                "method_name": LENGTH_FILTER_SKIP_METHOD_NAME,
                "skip_reason": "threshold_decision_missing",
                "thresholds": thresholds,
            },
            "length_filter_skipped",
            method_name=LENGTH_FILTER_SKIP,
        )
    return _length_filter_status_update(
        state,
        {
            "status": "planned",
            "method_name": LENGTH_FILTER_METHOD_NAME,
            "thresholds": {**thresholds, "resolved_min_len": resolved_min_len},
        },
        "length_filter_planned",
        method_name=LENGTH_FILTER_METHOD_NAME,
    )


def commit_read_qc(state: TopLevelState) -> StateUpdate:
    """Advance to genome survey after read-QC planning or execution."""
    output = _read_qc_output(state)
    post_filter_summary = output.get("post_filter_summary") or {}
    if (
        output.get("filtered_long_reads_ref") or output.get("filtered_long_reads_refs")
    ) and post_filter_summary.get("num_seqs") == 0:
        reason = "no_reads_retained"
        failed_cursor = replace_read_qc_stage_output(
            state,
            {
                **serialize_read_qc_stage_output(output),
                "status": "failed",
                "reason": reason,
            },
        )
        return {
            **fail_workflow_step(state, "read_qc", reason),
            "route_trace": append_route_trace(state, "read_qc_stage"),
            "stage_outputs": failed_cursor["stage_outputs"],
        }
    zero_fastp_family: str | None = None
    for family in ("short_reads", "hic_reads"):
        summary = output.get(f"{family}_summary")
        if isinstance(summary, dict) and summary.get("total_reads_after") == 0:
            zero_fastp_family = family
            break
    if zero_fastp_family is not None:
        reason = "no_reads_retained"
        failed_cursor = replace_read_qc_stage_output(
            state,
            {
                **serialize_read_qc_stage_output(output),
                "status": "failed",
                "reason": reason,
            },
        )
        return {
            **fail_workflow_step(state, "read_qc", reason),
            "route_trace": append_route_trace(state, "read_qc_stage"),
            "stage_outputs": failed_cursor["stage_outputs"],
        }
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
    # Fastp business outputs are committed alongside the long-read cursor
    # fields. Build the projection only after adding those keys;
    # otherwise clean pairs are delivered to resource_refs but disappear from
    # the durable stage cursor on the same commit.
    update: StateUpdate = {
        **advance_workflow_step(state, "read_qc", outcome="completed"),
        "route_trace": append_route_trace(state, "read_qc_stage"),
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
            "formats": list(dict.fromkeys(output.get("input_member_formats") or [])),
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
            metadata["source_asset_ids"] = list(output.get("input_asset_refs") or [])
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
    committed_keys.extend(
        [
            "short_reads_clean_pairs",
            "hic_reads_clean_pairs",
            "short_reads_summary",
            "hic_reads_summary",
            "short_reads_report_artifact_refs",
            "hic_reads_report_artifact_refs",
            "short_reads_read_set_manifest_ref",
            "hic_reads_read_set_manifest_ref",
        ]
    )
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
    update["stage_outputs"] = cursor_update["stage_outputs"]
    for family, resource_type in (
        ("short_reads", "short_reads"),
        ("hic_reads", "hic_reads"),
    ):
        pairs = _pair_list(output, f"{family}_clean_pairs")
        manifest_ref = output.get(f"{family}_read_set_manifest_ref")
        if not pairs or not manifest_ref:
            continue
        paths = [
            member["path"] for pair in pairs for member in (pair["r1"], pair["r2"])
        ]
        update["resource_refs"] = update_resource_ref(
            {
                **state,
                "resource_refs": update.get(
                    "resource_refs", state.get("resource_refs") or {}
                ),
            },
            resource_type,
            paths,
            "read_qc",
            {
                "read_set_manifest_ref": manifest_ref,
                # Keep the manifest-owned lane/mate grouping on the logical
                # resource so a later QC occurrence can resolve clean pairs
                # without re-inferring pairing from filenames.
                "paired_members": pairs,
                "artifact_refs": [
                    member["artifact_ref"]
                    for pair in pairs
                    for member in (pair["r1"], pair["r2"])
                ],
                "member_count": len(paths),
                "source_asset_ids": [
                    member["source_asset_id"]
                    for pair in pairs
                    for member in (pair["r1"], pair["r2"])
                ],
                "platforms": list(
                    dict.fromkeys(
                        member["platform"]
                        for pair in pairs
                        for member in (pair["r1"], pair["r2"])
                    )
                ),
                "formats": ["fastq"],
            },
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
    reason = output.get("reason") or default_reason
    return fail_workflow_step(state, "read_qc", reason)
