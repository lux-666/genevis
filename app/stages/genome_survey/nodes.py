"""Genome survey stage nodes for full and fallback survey contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.execution.artifacts import append_artifact_records
from app.graph.routing import next_stage_name
from app.schemas.artifacts import ArtifactMeta, GenomeSurveyResult
from app.schemas.state import TopLevelState

FULL_SURVEY_METHODS = [
    "genome_survey.kmer_count.jellyfish",
    "genome_survey.kmer_count.meryl",
    "genome_survey.model.genomescope2",
    "genome_survey.model.smudgeplot",
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
    """Write a full k-mer survey plan for low-error short DNA reads."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("selected_mode") != "full":
        return {}

    assets = survey_out.get("resolved_assets", {})
    input_refs = _asset_ids(assets.get("clean_short_dna_reads") or [])
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
            "execution_deferred_to_method_specific_task",
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
    trace = [*state.get("route_trace", []), "genome_survey_stage"]
    return {
        "current_stage": next_stage_name("genome_survey"),
        "supervision_state": "running_stage",
        "route_trace": trace,
    }


def fail_genome_survey(state: TopLevelState) -> dict:
    """Stop the workflow on genome survey setup failure."""
    survey_out = _genome_survey_output(state)
    return {
        "current_stage": "genome_survey",
        "supervision_state": "failed_terminal",
        "last_error": survey_out.get("reason", "genome_survey failed"),
    }


def _record_result(
    state: TopLevelState,
    result: GenomeSurveyResult,
    stage_payload: dict[str, Any],
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

    return _update_genome_survey_output(
        state,
        {
            **stage_payload,
            "result_artifact_ref": str(result_path),
            "artifact_refs": [str(artifact.file_path)],
        },
    )


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
