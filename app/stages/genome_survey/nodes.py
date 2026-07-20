"""Genome survey stage nodes for Jellyfish/GenomeScope survey contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.storage.ledger import Ledger
from app.domains.genome_size import resolve_explicit_genome_size
from app.storage.artifacts import publish_artifact_records
from app.workflow import next_stage_name
from app.schemas.artifacts import (
    ArtifactMeta,
    GenomeSurveyResult,
)
from app.configuration import load_descriptor_registry
from app.schemas.state import TopLevelState
from app.stages.base import (
    append_route_trace,
    stage_artifacts_dir,
    stage_output,
    update_stage_output,
)
from app.stages.resource_resolution import resolve_stage_resource
from app.stages.resource_specs import LONG_READS_SPEC, SHORT_READS_SPEC

FULL_SURVEY_METHODS = [
    "genome_survey.kmer_count.jellyfish",
    "genome_survey.model.genomescope2",
]
EXECUTED_FULL_SURVEY_METHODS = [
    "genome_survey.kmer_count.jellyfish",
    "genome_survey.model.genomescope2",
]
GENOME_SURVEY_METHOD = "genome_survey.contract"
GENOME_SIZE_UNAVAILABLE = "GENOME_SIZE_UNAVAILABLE"


def _genome_survey_output(state: TopLevelState) -> dict[str, Any]:
    return stage_output(state, "genome_survey")


def _update_genome_survey_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    return update_stage_output(state, "genome_survey", payload)


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_genome_survey_output(state, {"status": "failed", "reason": reason})


def _survey_input_family(assets: dict[str, Any]) -> str | None:
    if assets.get("clean_short_dna_reads"):
        return "clean_short_dna_reads"
    if assets.get("raw_short_dna_reads"):
        return "raw_short_dna_reads"
    if assets.get("clean_long_reads"):
        return "clean_long_reads"
    if assets.get("raw_long_reads"):
        return "raw_long_reads"
    return None


def _survey_input_assets(assets: dict[str, Any]) -> list[dict[str, Any]]:
    family = _survey_input_family(assets)
    return list(assets.get(family) or []) if family else []


def enter_genome_survey(state: TopLevelState) -> dict:
    """Mark genome survey as running."""
    return {"supervision_state": "running_stage"}


def resolve_survey_inputs(state: TopLevelState) -> dict:
    """Resolve survey reads from the central table, then the initial registry."""

    short_resolution = resolve_stage_resource(state, resource_spec=SHORT_READS_SPEC)
    long_resolution = resolve_stage_resource(state, resource_spec=LONG_READS_SPEC)
    short_assets, short_error = _resolution_assets(short_resolution, "short_reads")
    long_assets, long_error = _resolution_assets(long_resolution, "long_reads")
    if short_error or long_error:
        return _fail_update(state, short_error or long_error or "survey input error")

    short_types = set(short_resolution.metadata.get("asset_types") or [])
    long_types = set(long_resolution.metadata.get("asset_types") or [])
    clean_short = short_assets if "clean_short_dna_reads" in short_types else []
    raw_short = [] if clean_short else short_assets
    clean_long = (
        long_assets
        if long_resolution.source == "read_qc" or "clean_long_reads" in long_types
        else []
    )
    raw_long = [] if clean_long else long_assets
    long_reads = clean_long or raw_long

    return _update_genome_survey_output(
        state,
        {
            "status": "resolved",
            "clean_short_dna_read_refs": _asset_ids(clean_short),
            "raw_short_dna_read_refs": _asset_ids(raw_short),
            "long_read_refs": _asset_ids(long_reads),
            "resolved_assets": {
                "clean_short_dna_reads": clean_short,
                "raw_short_dna_reads": raw_short,
                "clean_long_reads": clean_long,
                "raw_long_reads": raw_long,
                "long_reads": long_reads,
            },
        },
    )


def _resolution_assets(resolution: Any, resource_type: str) -> tuple[list[dict[str, Any]], str | None]:
    """Adapt a logical resource to the survey stage's private asset cursor."""

    if not resolution.found:
        return [], None
    missing = [path for path in resolution.paths if not Path(path).is_file()]
    if missing:
        return [], f"{resource_type} resource path not found: {missing[0]}"
    metadata = resolution.metadata
    asset_ids = list(metadata.get("asset_ids") or metadata.get("input_asset_ids") or [])
    platforms = list(metadata.get("platforms") or [])
    formats = list(metadata.get("formats") or [])
    assets = []
    for index, path in enumerate(resolution.paths):
        assets.append(
            {
                "asset_id": asset_ids[index] if index < len(asset_ids) else f"{resource_type}:{index}",
                "asset_type": resource_type,
                "path": path,
                "platform": platforms[0] if platforms else metadata.get("platform"),
                "format": formats[0] if formats else metadata.get("format"),
                "source": resolution.source,
            }
        )
    return assets, None


def select_survey_mode(state: TopLevelState) -> dict:
    """Run a full survey whenever short or long genomic reads are available."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "resolved":
        return {}

    assets = survey_out.get("resolved_assets", {})
    input_family = _survey_input_family(assets)
    if input_family:
        return _update_genome_survey_output(
            state,
            {
                "status": "selected",
                "selected_mode": "full",
                "survey_input_family": input_family,
                "survey_input_refs": _asset_ids(assets.get(input_family) or []),
            },
        )

    explicit = resolve_explicit_genome_size(state)
    if explicit is not None:
        return _update_genome_survey_output(
            state,
            {
                "status": "selected",
                "selected_mode": "skipped_no_usable_reads",
                "genome_size_prior": explicit["value_bp"],
                "genome_size_source": "provided",
                "genome_size_source_path": explicit["source_path"],
            },
        )

    return _update_genome_survey_output(
        state,
        {
            "status": "failed",
            "error_code": GENOME_SIZE_UNAVAILABLE,
            "genome_size_prior": None,
            "reason": "genome survey requires usable short or long genomic reads when no explicit genome size is provided",
        },
    )


def plan_full_kmer_survey(state: TopLevelState) -> dict:
    """Plan or start a full k-mer survey for available short or long genomic reads."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("selected_mode") != "full":
        return {}

    descriptor_error = _validate_methods(EXECUTED_FULL_SURVEY_METHODS)
    if descriptor_error:
        return _fail_update(state, descriptor_error)

    assets = survey_out.get("resolved_assets", {})
    input_family = survey_out.get("survey_input_family") or _survey_input_family(assets)
    input_refs = survey_out.get("survey_input_refs") or _asset_ids(_survey_input_assets(assets))
    if survey_out.get("execution_mode") == "local":
        params, error = _survey_parameters(survey_out, state)
        if error:
            return _fail_update(state, error)
        substeps = dict(survey_out.get("substeps") or {})
        substeps["kmer_count"] = {
            "name": "kmer_count",
            "status": "planned",
            "method_name": "genome_survey.kmer_count.jellyfish",
        }
        substeps["genome_model"] = {
            "name": "genome_model",
            "status": "planned",
            "method_name": "genome_survey.model.genomescope2",
        }
        return _update_genome_survey_output(
            state,
            {
                "status": "local_execution_planned",
                "mode": "full",
                "confidence": "high",
                "candidate_methods": FULL_SURVEY_METHODS,
                "executed_methods": EXECUTED_FULL_SURVEY_METHODS,
                "survey_parameters": params,
                "survey_input_family": input_family,
                "survey_input_refs": input_refs,
                "substeps": substeps,
                "skip_reason": None,
            },
        )

    measured_estimates = {
        "genome_size": None,
        "heterozygosity": None,
        "repeat_fraction": None,
    }
    estimates, provided_overrides, estimate_sources, estimate_source_paths = _merge_provided_overrides(
        state,
        measured_estimates,
    )
    prior_update = _genome_size_prior_update(
        estimates,
        "local_execution_not_enabled",
        estimate_sources=estimate_sources,
        estimate_source_paths=estimate_source_paths,
    )
    if prior_update.get("status") == "failed":
        return _update_genome_survey_output(state, prior_update)
    notes = [
        "full_mode_uses_available_short_or_long_genomic_reads",
        "local_execution_not_enabled",
    ]
    if provided_overrides:
        notes.append("provided_values_override_corresponding_survey_estimates")

    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="full",
        status="planned",
        confidence="high",
        input_asset_refs=input_refs,
        candidate_methods=FULL_SURVEY_METHODS,
        estimates=estimates,
        measured_estimates=measured_estimates,
        provided_overrides=provided_overrides,
        estimate_sources=estimate_sources,
        estimate_source_paths=estimate_source_paths,
        genome_size_prior=prior_update.get("genome_size_prior"),
        genome_size_source=prior_update.get("genome_size_source"),
        genome_size_source_path=prior_update.get("genome_size_source_path"),
        notes=notes,
        metadata={"input_family": input_family},
    )
    return _record_result(
        state,
        result,
        {
            "status": "completed",
            "mode": result.mode,
            "confidence": result.confidence,
            **prior_update,
            "skip_reason": None,
            "candidate_methods": result.candidate_methods,
            "estimates": result.estimates,
            "measured_estimates": result.measured_estimates,
            "provided_overrides": result.provided_overrides,
            "estimate_sources": result.estimate_sources,
            "estimate_source_paths": result.estimate_source_paths,
            "notes": result.notes,
            "survey_input_family": input_family,
            "survey_input_refs": input_refs,
        },
    )


def write_full_survey_result(state: TopLevelState) -> dict:
    """Write completed full survey result after local execution."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "genomescope2_collected":
        return {}

    measured_estimates, parse_notes = _parse_genomescope_summary(Path(survey_out["genomescope2_summary_ref"]))
    estimates, provided_overrides, estimate_sources, estimate_source_paths = _merge_provided_overrides(
        state,
        measured_estimates,
    )
    prior_update = _genome_size_prior_update(
        estimates,
        "genomescope2_genome_size_unavailable",
        estimate_sources=estimate_sources,
        estimate_source_paths=estimate_source_paths,
    )
    notes = [
        "full_mode_uses_available_short_or_long_genomic_reads",
        "executed_jellyfish_and_genomescope2",
        *parse_notes,
    ]
    if provided_overrides:
        notes.append("provided_values_override_corresponding_survey_estimates")

    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="full",
        status="completed",
        confidence="high",
        input_asset_refs=survey_out.get("survey_input_refs", []),
        candidate_methods=FULL_SURVEY_METHODS,
        estimates=estimates,
        measured_estimates=measured_estimates,
        provided_overrides=provided_overrides,
        estimate_sources=estimate_sources,
        estimate_source_paths=estimate_source_paths,
        genome_size_prior=prior_update.get("genome_size_prior"),
        genome_size_source=prior_update.get("genome_size_source"),
        genome_size_source_path=prior_update.get("genome_size_source_path"),
        notes=notes,
        metadata={
            "input_family": survey_out.get("survey_input_family"),
            "executed_methods": EXECUTED_FULL_SURVEY_METHODS,
            "kmer_hist_ref": survey_out.get("kmer_hist_artifact_ref"),
            "genomescope2_summary_ref": survey_out.get("genomescope2_summary_ref"),
        },
    )
    if prior_update.get("status") == "failed":
        return _update_genome_survey_output(
            state,
            {
                **prior_update,
                "estimates": result.estimates,
                "measured_estimates": result.measured_estimates,
                "provided_overrides": result.provided_overrides,
                "estimate_sources": result.estimate_sources,
                "estimate_source_paths": result.estimate_source_paths,
                "notes": result.notes,
            },
        )
    return _record_result(
        state,
        result,
        {
            "status": "completed",
            "mode": result.mode,
            "confidence": result.confidence,
            **prior_update,
            "skip_reason": None,
            "candidate_methods": result.candidate_methods,
            "executed_methods": EXECUTED_FULL_SURVEY_METHODS,
            "estimates": result.estimates,
            "measured_estimates": result.measured_estimates,
            "provided_overrides": result.provided_overrides,
            "estimate_sources": result.estimate_sources,
            "estimate_source_paths": result.estimate_source_paths,
            "notes": result.notes,
        },
        preserve_artifact_refs=True,
    )


def write_survey_skip(state: TopLevelState) -> dict:
    """Use an explicit size only when no usable survey reads exist."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("selected_mode") != "skipped_no_usable_reads":
        return {}

    genome_size = survey_out.get("genome_size_prior")
    source_path = survey_out.get("genome_size_source_path")
    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="skipped",
        status="skipped",
        confidence="none",
        input_asset_refs=[],
        candidate_methods=[],
        estimates={"genome_size": genome_size},
        measured_estimates={},
        provided_overrides={"genome_size": genome_size},
        estimate_sources={"genome_size": "provided"},
        estimate_source_paths={"genome_size": source_path} if source_path else {},
        genome_size_prior=genome_size,
        genome_size_source="provided",
        genome_size_source_path=source_path,
        skip_reason="no_usable_survey_reads",
        notes=["genome_survey_skipped_because_no_usable_short_or_long_genomic_reads_exist"],
    )
    update = _record_result(
        state,
        result,
        {
            "status": "skipped",
            "mode": result.mode,
            "confidence": result.confidence,
            "genome_size_prior": survey_out.get("genome_size_prior"),
            "genome_size_source": "provided",
            "genome_size_source_path": survey_out.get("genome_size_source_path"),
            "skip_reason": result.skip_reason,
            "candidate_methods": [],
            "estimates": result.estimates,
            "measured_estimates": result.measured_estimates,
            "provided_overrides": result.provided_overrides,
            "estimate_sources": result.estimate_sources,
            "estimate_source_paths": result.estimate_source_paths,
            "notes": result.notes,
        },
    )
    update["skip_reasons"] = {
        **(state.get("skip_reasons") or {}),
        "genome_survey": result.skip_reason,
    }
    return update


def commit_genome_survey(state: TopLevelState) -> dict:
    """Advance to assembly after survey planning, execution, or skip."""
    survey_out = _genome_survey_output(state)
    committed_keys = [
        "artifact_refs",
        "candidate_methods",
        "confidence",
        "env_target",
        "error_code",
        "estimate_source_paths",
        "estimate_sources",
        "estimates",
        "executed_methods",
        "genomescope2_output_dir_ref",
        "genomescope2_summary_ref",
        "genome_size_prior",
        "genome_size_source",
        "genome_size_source_path",
        "kmer_db_artifact_ref",
        "kmer_hist_artifact_ref",
        "method_name",
        "missing_tools",
        "mode",
        "measured_estimates",
        "notes",
        "provided_overrides",
        "reason",
        "result_artifact_ref",
        "survey_input_family",
        "survey_input_refs",
        "skip_reason",
        "status",
        "substeps",
    ]
    outputs = dict(state.get("stage_outputs") or {})
    outputs["genome_survey"] = {key: survey_out[key] for key in committed_keys if key in survey_out}
    trace = append_route_trace(state, "genome_survey_stage")
    return {
        "current_stage": next_stage_name("genome_survey"),
        "supervision_state": "running_stage",
        "route_trace": trace,
        "stage_outputs": outputs,
    }


def fail_genome_survey(state: TopLevelState) -> dict:
    """Stop the workflow on genome survey setup failure or preflight block."""
    survey_out = _genome_survey_output(state)
    status = survey_out.get("status", "failed")
    default_reason = {
        "blocked_env": "genome_survey blocked on missing conda environment",
        "missing_tool": "genome_survey blocked on missing tool",
    }.get(status, "genome_survey failed")
    return {
        "current_stage": "genome_survey",
        "supervision_state": "failed_terminal",
        "last_error": survey_out.get("reason", default_reason),
    }


def _record_result(
    state: TopLevelState,
    result: GenomeSurveyResult,
    stage_payload: dict[str, Any],
    *,
    preserve_artifact_refs: bool = False,
) -> dict:
    data_dir = Path(state.get("data_dir", "data"))
    result_dir = stage_artifacts_dir(state, "genome_survey")
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / "genome_survey_result.json"
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    artifact = ArtifactMeta.from_file(
        result_path,
        stage="genome_survey",
        method_name=GENOME_SURVEY_METHOD,
        artifact_type="genome_survey_result",
        metadata={
            "mode": result.mode,
            "confidence": result.confidence,
            "skip_reason": result.skip_reason,
        },
    )
    publish_artifact_records(data_dir, state["session_id"], [artifact])
    Ledger(data_dir).record_artifact_registered(state["session_id"], artifact.model_dump(mode="json"))
    artifact_refs = [str(artifact.file_path)]
    if preserve_artifact_refs:
        artifact_refs = [*list(_genome_survey_output(state).get("artifact_refs") or []), *artifact_refs]

    return _update_genome_survey_output(
        state,
        {
            **stage_payload,
            "result_artifact_ref": str(result_path),
            "artifact_refs": artifact_refs,
        },
    )


def _validate_methods(method_names: list[str]) -> str | None:
    registry = load_descriptor_registry()
    for method_name in method_names:
        if method_name not in registry.methods:
            return f"descriptor missing: {method_name}"
    return None


def _survey_parameters(survey_out: dict[str, Any], state: TopLevelState) -> tuple[dict[str, Any], str | None]:
    raw_params = dict(survey_out.get("survey_parameters") or {})
    missing = [key for key in ["kmer_size", "hash_size"] if key not in raw_params]
    if missing:
        return {}, "genome_survey local execution requires explicit survey_parameters." + missing[0]
    try:
        kmer_size = int(raw_params["kmer_size"])
        threads = int(raw_params.get("threads", 1))
    except (TypeError, ValueError):
        return {}, "genome_survey local execution requires integer kmer_size and threads"
    if kmer_size <= 0 or threads <= 0:
        return {}, "genome_survey local execution requires positive kmer_size and threads"
    return {
        "kmer_size": kmer_size,
        "threads": threads,
        "hash_size": str(raw_params["hash_size"]),
    }, None


def _parse_genomescope_summary(path: Path) -> tuple[dict[str, Any], list[str]]:
    estimates: dict[str, Any] = {
        "genome_size": None,
        "heterozygosity": None,
        "repeat_fraction": None,
    }
    notes: list[str] = []
    parsed = False
    repeat_length: float | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line_text = line.strip()
        parts = [part for part in re.split(r"\t+| {2,}|=", line_text) if part]
        if len(parts) >= 2:
            key = parts[0].strip().lower()
            raw_value = parts[1].strip()
            # GenomeScope 2 writes values such as ``108,518 bp`` in its
            # space-aligned summary table for length fields.  Preserve the
            # first numeric value (the table's point estimate/minimum) and
            # discard its unit, without accepting malformed percentage
            # fields such as ``0.001-0.002``.
            if key in {"genome_size", "genome size", "haploid_length", "genome haploid length", "genome repeat length"}:
                numeric_value = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?", raw_value)
                value = numeric_value.group(0).replace(",", "") if numeric_value else raw_value.replace(",", "")
            else:
                value = raw_value.replace(",", "")
        else:
            lower_line = line_text.lower()
            values = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?", line_text)
            if not values:
                continue
            value = values[0].replace(",", "")
            if "genome haploid length" in lower_line:
                key = "genome haploid length"
            elif "genome repeat length" in lower_line:
                key = "genome repeat length"
            elif "heterozygosity" in lower_line:
                key = "heterozygosity"
            else:
                continue
        if key == "property":
            continue
        try:
            if key in {
                "genome_size",
                "genome size",
                "haploid_length",
                "genome haploid length",
            }:
                estimates["genome_size"] = int(float(value))
                parsed = True
            elif key in {"heterozygosity", "het"}:
                estimates["heterozygosity"] = float(value)
                parsed = True
            elif key in {"repeat_fraction", "repeat", "repetitiveness"}:
                estimates["repeat_fraction"] = float(value)
                parsed = True
            elif key == "genome repeat length":
                repeat_length = float(value)
                parsed = True
        except ValueError:
            normalized = key.replace(" ", "_")
            notes.append(f"genomescope2_{normalized}_unparsed")
    genome_size = estimates.get("genome_size")
    if estimates["repeat_fraction"] is None and repeat_length is not None and genome_size:
        estimates["repeat_fraction"] = repeat_length / float(genome_size)
    if not parsed:
        notes.append("genomescope2_estimates_unparsed")
    return estimates, notes


def _asset_ids(assets: list[dict[str, Any]]) -> list[str]:
    return [asset.get("asset_id", "") for asset in assets if asset.get("asset_id")]


def _merge_provided_overrides(
    state: TopLevelState,
    measured_estimates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, str]]:
    """Overlay TUI/operator values while retaining raw survey measurements."""

    estimates = dict(measured_estimates)
    provided_overrides: dict[str, Any] = {}
    estimate_sources = {
        key: "survey"
        for key, value in measured_estimates.items()
        if value is not None
    }
    estimate_source_paths: dict[str, str] = {}

    explicit = resolve_explicit_genome_size(state)
    if explicit is not None:
        estimates["genome_size"] = explicit["value_bp"]
        provided_overrides["genome_size"] = explicit["value_bp"]
        estimate_sources["genome_size"] = "provided"
        estimate_source_paths["genome_size"] = explicit["source_path"]

    return estimates, provided_overrides, estimate_sources, estimate_source_paths


def _genome_size_prior_update(
    estimates: dict[str, Any],
    reason: str,
    *,
    estimate_sources: dict[str, str] | None = None,
    estimate_source_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    survey_size = estimates.get("genome_size")
    if survey_size:
        source = (estimate_sources or {}).get("genome_size", "survey")
        update = {
            "genome_size_prior": int(survey_size),
            "genome_size_source": source,
        }
        source_path = (estimate_source_paths or {}).get("genome_size")
        if source_path:
            update["genome_size_source_path"] = source_path
        return update

    return {
        "status": "failed",
        "error_code": GENOME_SIZE_UNAVAILABLE,
        "genome_size_prior": None,
        "genome_size_source": None,
        "reason": reason,
    }
