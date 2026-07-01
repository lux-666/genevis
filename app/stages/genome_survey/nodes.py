"""Genome survey stage nodes for Jellyfish/GenomeScope survey contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.schemas.artifacts import (
    ArtifactMeta,
    GenomeSurveyResult,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.base import stage_output, update_stage_output

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
    """Resolve short-read and long-read survey inputs from the asset registry."""
    from app.stages.base import load_assets_from_registry

    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return _fail_update(state, "missing asset_registry_ref for genome_survey")

    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return _fail_update(state, f"asset registry not found: {registry_ref}")

    clean_short = load_assets_from_registry(state, ["clean_short_dna_reads"])
    raw_short = load_assets_from_registry(state, ["raw_short_dna_reads"])
    clean_long = load_assets_from_registry(state, ["clean_long_reads"])
    raw_long = load_assets_from_registry(state, ["raw_long_reads"])
    long_reads = load_assets_from_registry(state, ["raw_long_reads", "clean_long_reads"])

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


def select_survey_mode(state: TopLevelState) -> dict:
    """Select a full survey or skip only when an explicit prior is available."""
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

    explicit_prior = _explicit_genome_size_prior(state)
    if explicit_prior is not None:
        return _update_genome_survey_output(
            state,
            {
                "status": "selected",
                "selected_mode": "skipped_explicit_prior",
                "genome_size_prior": explicit_prior,
            },
        )
    return _update_genome_survey_output(
        state,
        {
            "status": "failed",
            "error_code": GENOME_SIZE_UNAVAILABLE,
            "genome_size_prior": None,
            "reason": "genome survey requires short or long DNA reads when no explicit genome size is provided",
        },
    )


def plan_full_kmer_survey(state: TopLevelState) -> dict:
    """Plan or start a full k-mer survey for available DNA reads."""
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

    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="full",
        status="planned",
        confidence="high",
        input_asset_refs=input_refs,
        candidate_methods=FULL_SURVEY_METHODS,
        estimates={
            "genome_size": None,
            "heterozygosity": None,
            "repeat_fraction": None,
        },
        notes=[
            "full_mode_uses_available_dna_reads",
            "local_execution_not_enabled",
        ],
        metadata={"input_family": input_family},
    )
    prior_update = _genome_size_prior_update(state, result.estimates, "local_execution_not_enabled")
    if prior_update.get("status") == "failed":
        return _update_genome_survey_output(state, prior_update)
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

    estimates, parse_notes = _parse_genomescope_summary(Path(survey_out["genomescope2_summary_ref"]))
    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="full",
        status="completed",
        confidence="high",
        input_asset_refs=survey_out.get("survey_input_refs", []),
        candidate_methods=FULL_SURVEY_METHODS,
        estimates=estimates,
        notes=[
            "full_mode_uses_available_dna_reads",
            "executed_jellyfish_and_genomescope2",
            *parse_notes,
        ],
        metadata={
            "input_family": survey_out.get("survey_input_family"),
            "executed_methods": EXECUTED_FULL_SURVEY_METHODS,
            "kmer_hist_ref": survey_out.get("kmer_hist_artifact_ref"),
            "genomescope2_summary_ref": survey_out.get("genomescope2_summary_ref"),
        },
    )
    prior_update = _genome_size_prior_update(state, result.estimates, "genomescope2_genome_size_unavailable")
    if prior_update.get("status") == "failed":
        return _update_genome_survey_output(state, {**prior_update, "estimates": result.estimates, "notes": result.notes})
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
            "notes": result.notes,
        },
        preserve_artifact_refs=True,
    )


def write_survey_skip(state: TopLevelState) -> dict:
    """Write an explicit skip record when an operator supplied genome size."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("selected_mode") != "skipped_explicit_prior":
        return {}

    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="skipped",
        status="skipped",
        confidence="none",
        input_asset_refs=[],
        candidate_methods=[],
        estimates={},
        skip_reason="explicit_genome_size_prior",
        notes=["genome_survey_skipped_because_explicit_genome_size_prior_is_available"],
    )
    update = _record_result(
        state,
        result,
        {
            "status": "skipped",
            "mode": result.mode,
            "confidence": result.confidence,
            "genome_size_prior": survey_out.get("genome_size_prior"),
            "skip_reason": result.skip_reason,
            "candidate_methods": [],
            "estimates": {},
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
        "estimates",
        "executed_methods",
        "genomescope2_output_dir_ref",
        "genomescope2_summary_ref",
        "genome_size_prior",
        "genome_size_source",
        "kmer_db_artifact_ref",
        "kmer_hist_artifact_ref",
        "method_name",
        "missing_tools",
        "mode",
        "notes",
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
    trace = [*state.get("route_trace", []), "genome_survey_stage"]
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
    result_dir = data_dir / "artifacts" / "genome_survey" / state["session_id"]
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
    append_artifact_records(data_dir, state["session_id"], [artifact])
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
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
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


def _explicit_genome_size_prior(state: TopLevelState) -> int | None:
    """Return only an operator-provided genome size, never survey output."""
    shared = state.get("shared_parameters") or {}
    if hasattr(shared, "model_dump"):
        shared = shared.model_dump(exclude_none=True)
    value = dict(shared).get("estimated_genome_size")
    if value:
        return int(value)

    assembly = (state.get("stage_outputs") or {}).get("assembly") or {}
    for params, key in (
        (assembly.get("flye_parameters") or {}, "estimated_genome_size"),
        (assembly.get("nextdenovo_parameters") or {}, "genome_size"),
    ):
        if params.get(key):
            return int(params[key])
    return None


def _genome_size_prior_update(
    state: TopLevelState,
    estimates: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    explicit_prior = _explicit_genome_size_prior(state)
    if explicit_prior is not None:
        return {"genome_size_prior": explicit_prior, "genome_size_source": "user"}

    survey_size = estimates.get("genome_size")
    if survey_size:
        return {"genome_size_prior": int(survey_size), "genome_size_source": "survey"}

    return {
        "status": "failed",
        "error_code": GENOME_SIZE_UNAVAILABLE,
        "genome_size_prior": None,
        "genome_size_source": None,
        "reason": reason,
    }
