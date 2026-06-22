"""Genome survey stage nodes for full and fallback survey contracts."""

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

FULL_SURVEY_METHODS = [
    "genome_survey.kmer_count.jellyfish",
    "genome_survey.kmer_count.meryl",
    "genome_survey.model.genomescope2",
    "genome_survey.model.smudgeplot",
]
EXECUTED_FULL_SURVEY_METHODS = [
    "genome_survey.kmer_count.jellyfish",
    "genome_survey.model.genomescope2",
]
GENOME_SURVEY_METHOD = "genome_survey.contract"
NO_LOW_ERROR_SHORT_READS = "no_low_error_short_reads"


def _genome_survey_output(state: TopLevelState) -> dict[str, Any]:
    return dict((state.get("stage_outputs") or {}).get("genome_survey", {}))


def _update_genome_survey_output(state: TopLevelState, payload: dict[str, Any]) -> dict:
    outputs = dict(state.get("stage_outputs") or {})
    outputs["genome_survey"] = {**_genome_survey_output(state), **payload}
    return {"stage_outputs": outputs}


def _fail_update(state: TopLevelState, reason: str) -> dict:
    return _update_genome_survey_output(state, {"status": "failed", "reason": reason})


def enter_genome_survey(state: TopLevelState) -> dict:
    """Mark genome survey as running."""
    return {"supervision_state": "running_stage"}


def resolve_survey_inputs(state: TopLevelState) -> dict:
    """Resolve short-read and long-read survey inputs from the asset registry."""
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref:
        return _fail_update(state, "missing asset_registry_ref for genome_survey")

    registry_path = Path(registry_ref)
    if not registry_path.exists():
        return _fail_update(state, f"asset registry not found: {registry_ref}")

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assets = registry.get("assets", [])
    clean_short = _assets_of_type(assets, "clean_short_dna_reads")
    raw_short = _assets_of_type(assets, "raw_short_dna_reads")
    long_reads = [
        asset
        for asset in assets
        if asset.get("asset_type") in {"raw_long_reads", "clean_long_reads"}
    ]

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
                "long_reads": long_reads,
            },
        },
    )


def select_survey_mode(state: TopLevelState) -> dict:
    """Select full, fallback, or skipped mode from available inputs."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "resolved":
        return {}

    assets = survey_out.get("resolved_assets", {})
    clean_short = assets.get("clean_short_dna_reads") or []
    long_reads = assets.get("long_reads") or []
    if clean_short:
        mode = "full"
    elif long_reads:
        mode = "fallback_low_confidence"
    else:
        mode = "skipped"

    return _update_genome_survey_output(state, {"status": "selected", "selected_mode": mode})


def plan_full_kmer_survey(state: TopLevelState) -> dict:
    """Plan or start a full k-mer survey for low-error short DNA reads."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("selected_mode") != "full":
        return {}

    descriptor_error = _validate_methods(EXECUTED_FULL_SURVEY_METHODS)
    if descriptor_error:
        return _fail_update(state, descriptor_error)

    assets = survey_out.get("resolved_assets", {})
    input_refs = _asset_ids(assets.get("clean_short_dna_reads") or [])
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
            "full_mode_requires_low_error_short_dna_reads",
            "local_execution_not_enabled",
        ],
        metadata={"input_family": "clean_short_dna_reads"},
    )
    return _record_result(
        state,
        result,
        {
            "status": "completed",
            "mode": result.mode,
            "confidence": result.confidence,
            "skip_reason": None,
            "candidate_methods": result.candidate_methods,
            "estimates": result.estimates,
            "notes": result.notes,
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
        input_asset_refs=survey_out.get("clean_short_dna_read_refs", []),
        candidate_methods=FULL_SURVEY_METHODS,
        estimates=estimates,
        notes=[
            "full_mode_requires_low_error_short_dna_reads",
            "executed_jellyfish_and_genomescope2",
            *parse_notes,
        ],
        metadata={
            "input_family": "clean_short_dna_reads",
            "executed_methods": EXECUTED_FULL_SURVEY_METHODS,
            "kmer_hist_ref": survey_out.get("kmer_hist_artifact_ref"),
            "genomescope2_summary_ref": survey_out.get("genomescope2_summary_ref"),
        },
    )
    return _record_result(
        state,
        result,
        {
            "status": "completed",
            "mode": result.mode,
            "confidence": result.confidence,
            "skip_reason": None,
            "candidate_methods": result.candidate_methods,
            "executed_methods": EXECUTED_FULL_SURVEY_METHODS,
            "estimates": result.estimates,
            "notes": result.notes,
        },
        preserve_artifact_refs=True,
    )


def plan_long_read_fallback(state: TopLevelState) -> dict:
    """Write a low-confidence CycloneSEQ-only fallback summary."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("selected_mode") != "fallback_low_confidence":
        return {}

    long_reads = (survey_out.get("resolved_assets") or {}).get("long_reads") or []
    total_bases = _read_qc_total_bases(state)
    genome_size_prior = _genome_size_prior(state, long_reads)
    estimates: dict[str, Any] = {
        "total_bases": total_bases,
        "genome_size_prior": genome_size_prior,
    }
    if total_bases and genome_size_prior:
        estimates["coverage"] = total_bases / genome_size_prior

    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="fallback_low_confidence",
        status="planned",
        confidence="low",
        input_asset_refs=_asset_ids(long_reads),
        candidate_methods=[],
        estimates=estimates,
        notes=[
            "not_equivalent_to_kmer_survey",
            "coverage_estimate_requires_total_bases_and_genome_size_prior",
        ],
        metadata={"input_family": "cycloneseq_long_reads"},
    )
    return _record_result(
        state,
        result,
        {
            "status": "completed",
            "mode": result.mode,
            "confidence": result.confidence,
            "skip_reason": None,
            "candidate_methods": result.candidate_methods,
            "estimates": result.estimates,
            "notes": result.notes,
        },
    )


def write_survey_skip(state: TopLevelState) -> dict:
    """Write an explicit skip record for missing usable survey inputs."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("selected_mode") != "skipped":
        return {}

    result = GenomeSurveyResult(
        session_id=state["session_id"],
        mode="skipped",
        status="skipped",
        confidence="none",
        input_asset_refs=[],
        candidate_methods=[],
        estimates={},
        skip_reason=NO_LOW_ERROR_SHORT_READS,
        notes=["no_clean_short_dna_reads_or_cycloneseq_long_reads_available"],
    )
    update = _record_result(
        state,
        result,
        {
            "status": "skipped",
            "mode": result.mode,
            "confidence": result.confidence,
            "skip_reason": result.skip_reason,
            "candidate_methods": [],
            "estimates": {},
            "notes": result.notes,
        },
    )
    update["skip_reasons"] = {
        **(state.get("skip_reasons") or {}),
        "genome_survey": NO_LOW_ERROR_SHORT_READS,
    }
    return update


def commit_genome_survey(state: TopLevelState) -> dict:
    """Advance to assembly after survey planning, fallback, or skip."""
    survey_out = _genome_survey_output(state)
    committed_keys = [
        "artifact_refs",
        "candidate_methods",
        "confidence",
        "env_target",
        "estimates",
        "executed_methods",
        "genomescope2_output_dir_ref",
        "genomescope2_summary_ref",
        "kmer_db_artifact_ref",
        "kmer_hist_artifact_ref",
        "method_name",
        "missing_tools",
        "mode",
        "notes",
        "reason",
        "result_artifact_ref",
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
    read_length_raw = raw_params.get("read_length")
    if read_length_raw is None:
        read_length_raw = _read_length_from_read_qc(state)
    if read_length_raw is None:
        return {}, (
            "genome_survey local execution requires read_length from "
            "read_qc.summary.avg_len or explicit survey_parameters.read_length"
        )
    try:
        kmer_size = int(raw_params["kmer_size"])
        read_length = int(read_length_raw)
        threads = int(raw_params.get("threads", 1))
    except (TypeError, ValueError):
        return {}, "genome_survey local execution requires integer kmer_size, read_length, and threads"
    if kmer_size <= 0 or read_length <= 0 or threads <= 0:
        return {}, "genome_survey local execution requires positive kmer_size, read_length, and threads"
    return {
        "kmer_size": kmer_size,
        "read_length": read_length,
        "threads": threads,
        "hash_size": str(raw_params["hash_size"]),
    }, None


def _read_length_from_read_qc(state: TopLevelState) -> int | None:
    summary = _read_qc_summary(state)
    for key in ("avg_len", "n50", "max_len"):
        value = summary.get(key)
        if value is None:
            continue
        try:
            parsed = int(round(float(value)))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _read_qc_summary(state: TopLevelState) -> dict[str, Any]:
    read_qc = ((state.get("stage_outputs") or {}).get("read_qc") or {})
    summary = read_qc.get("summary")
    if isinstance(summary, dict):
        return summary
    summary_ref = read_qc.get("summary_artifact_ref")
    if not summary_ref:
        return {}
    try:
        payload = json.loads(Path(summary_ref).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    loaded = payload.get("summary") if isinstance(payload, dict) else None
    return loaded if isinstance(loaded, dict) else {}


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
            value = parts[1].strip().replace(",", "")
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


def _assets_of_type(assets: list[dict[str, Any]], asset_type: str) -> list[dict[str, Any]]:
    return [asset for asset in assets if asset.get("asset_type") == asset_type]


def _asset_ids(assets: list[dict[str, Any]]) -> list[str]:
    return [asset.get("asset_id", "") for asset in assets if asset.get("asset_id")]


def _read_qc_total_bases(state: TopLevelState) -> int | None:
    summary = ((state.get("stage_outputs") or {}).get("read_qc") or {}).get("summary") or {}
    total = summary.get("total_bases", summary.get("sum_len"))
    return int(total) if total else None


def _genome_size_prior(state: TopLevelState, assets: list[dict[str, Any]]) -> int | None:
    inventory_prior = ((state.get("stage_outputs") or {}).get("inventory") or {}).get("genome_size_estimate")
    if inventory_prior:
        return int(inventory_prior)
    for asset in assets:
        metadata = asset.get("metadata") or {}
        prior = metadata.get("genome_size_estimate")
        if prior:
            return int(prior)
    return None
