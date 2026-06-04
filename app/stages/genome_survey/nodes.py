"""Genome survey stage nodes for full and fallback survey contracts."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records
from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt, run_frozen_attempt
from app.graph.routing import next_stage_name
from app.stages.base import absolute_data_dir
from app.schemas.artifacts import (
    GENOME_SURVEY_KMER_DB,
    GENOME_SURVEY_KMER_HIST,
    GENOME_SURVEY_MODEL_SUMMARY,
    ArtifactMeta,
    GenomeSurveyResult,
)
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
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
JELLYFISH_METHOD = "genome_survey.kmer_count.jellyfish"
GENOMESCOPE2_METHOD = "genome_survey.model.genomescope2"
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
        params, error = _survey_parameters(survey_out)
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


def freeze_jellyfish_attempt(state: TopLevelState) -> dict:
    """Freeze the Jellyfish count/histo attempt."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "local_execution_planned":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, blocked = resolve_method_env(
        survey_out,
        stage="genome_survey",
        method_name=JELLYFISH_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_genome_survey_output(state, blocked)

    working_dir = data_dir / "work" / "genome_survey" / session_id / "jellyfish"
    artifact_dir = data_dir / "artifacts" / "genome_survey" / session_id
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    params = dict(survey_out["survey_parameters"])
    assets = (survey_out.get("resolved_assets") or {}).get("clean_short_dna_reads") or []
    read_paths = [str(Path(asset["path"])) for asset in assets]
    kmer_db = artifact_dir / "jellyfish_kmers.jf"
    kmer_hist = artifact_dir / "kmer_hist.tsv"
    script_path = working_dir / "run_jellyfish.sh"
    reads_arg = " ".join(shlex.quote(path) for path in read_paths)
    script_path.write_text(
        "set -e\n"
        f"jellyfish count -C -m {int(params['kmer_size'])} -s {shlex.quote(str(params['hash_size']))} "
        f"-t {int(params['threads'])} {reads_arg} -o {shlex.quote(str(kmer_db))}\n"
        f"jellyfish histo -t {int(params['threads'])} {shlex.quote(str(kmer_db))} > {shlex.quote(str(kmer_hist))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:genome_survey:jellyfish",
        session_id=session_id,
        stage="genome_survey",
        method_name=JELLYFISH_METHOD,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[kmer_db, kmer_hist],
        env=survey_out.get("env"),
        env_spec=env_spec,
        metadata={
            "input_asset_refs": survey_out.get("clean_short_dna_read_refs", []),
            "kmer_size": params["kmer_size"],
            "kmer_db_path": str(kmer_db),
            "kmer_hist_path": str(kmer_hist),
        },
    )
    return _update_genome_survey_output(
        state,
        {
            "status": "jellyfish_frozen",
            "jellyfish_frozen_attempt": attempt.model_dump(mode="json"),
            "kmer_db_path": str(kmer_db),
            "kmer_hist_path": str(kmer_hist),
        },
    )


def run_jellyfish_attempt(state: TopLevelState) -> dict:
    """Run the frozen Jellyfish attempt locally."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "jellyfish_frozen":
        return {}
    return _run_attempt(state, "jellyfish_frozen_attempt", "jellyfish_execution_result", "jellyfish_ran")


def collect_jellyfish_artifacts(state: TopLevelState) -> dict:
    """Collect Jellyfish k-mer database, histogram, and command logs."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "jellyfish_ran":
        return {}

    attempt = FrozenAttemptSpec.model_validate(survey_out["jellyfish_frozen_attempt"])
    result = survey_out["jellyfish_execution_result"]
    artifacts = _attempt_artifacts(
        attempt,
        result,
        {
            str(Path(survey_out["kmer_db_path"])): GENOME_SURVEY_KMER_DB,
            str(Path(survey_out["kmer_hist_path"])): GENOME_SURVEY_KMER_HIST,
        },
    )
    _record_attempt_success(state, attempt, artifacts)

    substeps = dict(survey_out.get("substeps") or {})
    substeps["kmer_count"] = {
        **dict(substeps.get("kmer_count") or {}),
        "name": "kmer_count",
        "status": "executed",
        "method_name": attempt.method_name,
        "kmer_db_ref": survey_out["kmer_db_path"],
        "kmer_hist_ref": survey_out["kmer_hist_path"],
    }
    return _update_genome_survey_output(
        state,
        {
            "status": "jellyfish_collected",
            "kmer_db_artifact_ref": survey_out["kmer_db_path"],
            "kmer_hist_artifact_ref": survey_out["kmer_hist_path"],
            "artifact_refs": [
                *list(survey_out.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "substeps": substeps,
        },
    )


def freeze_genomescope2_attempt(state: TopLevelState) -> dict:
    """Freeze the GenomeScope2 model attempt."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "jellyfish_collected":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, blocked = resolve_method_env(
        survey_out,
        stage="genome_survey",
        method_name=GENOMESCOPE2_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return _update_genome_survey_output(state, blocked)

    working_dir = data_dir / "work" / "genome_survey" / session_id / "genomescope2"
    artifact_dir = data_dir / "artifacts" / "genome_survey" / session_id / "genomescope2"
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    params = dict(survey_out["survey_parameters"])
    summary_path = artifact_dir / "summary.txt"
    script_path = working_dir / "run_genomescope2.sh"
    script_path.write_text(
        "set -e\n"
        f"mkdir -p {shlex.quote(str(artifact_dir))}\n"
        f"genomescope2 -i {shlex.quote(str(Path(survey_out['kmer_hist_path'])))} "
        f"-o {shlex.quote(str(artifact_dir))} -k {int(params['kmer_size'])}\n"
        f"test -f {shlex.quote(str(summary_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:genome_survey:genomescope2",
        session_id=session_id,
        stage="genome_survey",
        method_name=GENOMESCOPE2_METHOD,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[summary_path],
        env=survey_out.get("env"),
        env_spec=env_spec,
        metadata={
            "kmer_hist_path": survey_out.get("kmer_hist_path"),
            "summary_path": str(summary_path),
        },
    )
    return _update_genome_survey_output(
        state,
        {
            "status": "genomescope2_frozen",
            "genomescope2_frozen_attempt": attempt.model_dump(mode="json"),
            "genomescope2_summary_path": str(summary_path),
            "genomescope2_output_dir": str(artifact_dir),
        },
    )


def run_genomescope2_attempt(state: TopLevelState) -> dict:
    """Run the frozen GenomeScope2 attempt locally."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "genomescope2_frozen":
        return {}
    return _run_attempt(state, "genomescope2_frozen_attempt", "genomescope2_execution_result", "genomescope2_ran")


def collect_genomescope2_artifacts(state: TopLevelState) -> dict:
    """Collect GenomeScope2 summary and command logs."""
    survey_out = _genome_survey_output(state)
    if survey_out.get("status") != "genomescope2_ran":
        return {}

    attempt = FrozenAttemptSpec.model_validate(survey_out["genomescope2_frozen_attempt"])
    result = survey_out["genomescope2_execution_result"]
    artifacts = _attempt_artifacts(
        attempt,
        result,
        {str(Path(survey_out["genomescope2_summary_path"])): GENOME_SURVEY_MODEL_SUMMARY},
    )
    _record_attempt_success(state, attempt, artifacts)

    substeps = dict(survey_out.get("substeps") or {})
    substeps["genome_model"] = {
        **dict(substeps.get("genome_model") or {}),
        "name": "genome_model",
        "status": "executed",
        "method_name": attempt.method_name,
        "summary_ref": survey_out["genomescope2_summary_path"],
    }
    return _update_genome_survey_output(
        state,
        {
            "status": "genomescope2_collected",
            "genomescope2_summary_ref": survey_out["genomescope2_summary_path"],
            "genomescope2_output_dir_ref": survey_out["genomescope2_output_dir"],
            "artifact_refs": [
                *list(survey_out.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "substeps": substeps,
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


def _run_attempt(
    state: TopLevelState,
    attempt_key: str,
    result_key: str,
    next_status: str,
) -> dict:
    survey_out = _genome_survey_output(state)
    attempt = FrozenAttemptSpec.model_validate(survey_out[attempt_key])
    outcome = run_frozen_attempt(attempt, data_dir=state.get("data_dir", "data"), input_manifest_ref=state.get("input_manifest_ref", ""), fail_label=attempt.method_name)
    if not outcome.success:
        return _update_genome_survey_output(
            state,
            {
                "status": "failed",
                "reason": outcome.error,
                result_key: outcome.result.model_dump(mode="json"),
            },
        )

    return _update_genome_survey_output(
        state,
        {"status": next_status, result_key: outcome.result.model_dump(mode="json")},
    )


def _attempt_artifacts(
    attempt: FrozenAttemptSpec,
    execution_result: dict[str, Any],
    artifact_types_by_path: dict[str, str],
) -> list[ArtifactMeta]:
    artifacts: list[ArtifactMeta] = []
    for path in attempt.expected_outputs:
        if not path.exists():
            raise FileNotFoundError(f"expected output missing: {path}")
        artifacts.append(
            ArtifactMeta.from_file(
                path,
                stage="genome_survey",
                method_name=attempt.method_name,
                artifact_type=artifact_types_by_path[str(path)],
                metadata={"attempt_id": attempt.attempt_id},
            )
        )
    for path, artifact_type in [
        (Path(execution_result["stdout_path"]), "command.stdout"),
        (Path(execution_result["stderr_path"]), "command.stderr"),
    ]:
        if path.exists():
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage="genome_survey",
                    method_name=attempt.method_name,
                    artifact_type=artifact_type,
                    metadata={"attempt_id": attempt.attempt_id},
                )
            )
    return artifacts


def _record_attempt_success(state: TopLevelState, attempt: FrozenAttemptSpec, artifacts: list[ArtifactMeta]) -> None:
    data_dir = Path(state.get("data_dir", "data"))
    append_artifact_records(data_dir, attempt.session_id, artifacts)
    ledger = Ledger(data_dir)
    artifact_payloads = [artifact.model_dump(mode="json") for artifact in artifacts]
    ledger.record_attempt_succeeded(attempt.session_id, attempt, artifacts=artifact_payloads)
    for artifact in artifact_payloads:
        ledger.record_artifact_registered(attempt.session_id, artifact, attempt_id=attempt.attempt_id)


def _validate_methods(method_names: list[str]) -> str | None:
    registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
    for method_name in method_names:
        if method_name not in registry.methods:
            return f"descriptor missing: {method_name}"
    return None


def _survey_parameters(survey_out: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    raw_params = dict(survey_out.get("survey_parameters") or {})
    missing = [key for key in ["kmer_size", "read_length", "hash_size"] if key not in raw_params]
    if missing:
        return {}, "genome_survey local execution requires explicit survey_parameters." + missing[0]
    try:
        kmer_size = int(raw_params["kmer_size"])
        read_length = int(raw_params["read_length"])
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
