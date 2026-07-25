"""Jellyfish count/histogram method subgraph and implementation."""

from __future__ import annotations

import shlex
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import GENOME_SURVEY_KMER_DB, GENOME_SURVEY_KMER_HIST
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, stage_work_dir
from app.stages.genome_survey.method_application.support import (
    attempt_artifacts,
    record_attempt_success,
    survey_output,
    update_survey_output,
)
from app.stages.genome_survey.state import (
    ResolvedSurveyAssetsCursor,
    SurveyAssetCursor,
)

METHOD_NAME = "genome_survey.kmer_count.jellyfish"
FROZEN_STATUS = "jellyfish_frozen"
RAN_STATUS = "jellyfish_ran"
COLLECTED_STATUS = "jellyfish_collected"
LIFECYCLE = MethodExecutionLifecycle(
    stage="genome_survey",
    name="jellyfish",
    method_name=METHOD_NAME,
    frozen_status=FROZEN_STATUS,
    ran_status=RAN_STATUS,
)


def build_jellyfish_graph() -> CompiledStateGraph[
    TopLevelState, None, TopLevelState, TopLevelState
]:
    return build_method_graph(
        LIFECYCLE,
        freeze_jellyfish_attempt,
        collect_jellyfish_artifacts,
    )


def freeze_jellyfish_attempt(state: TopLevelState) -> StateUpdate:
    """Freeze the Jellyfish count/histo attempt."""
    output = survey_output(state)
    if output.get("status") != "local_execution_planned":
        return {}

    params = output["survey_parameters"]
    input_family = output.get("survey_input_family") or "clean_short_dna_reads"
    resolved_assets = output.get("resolved_assets") or {}
    assets = _assets_for_family(resolved_assets, input_family)
    read_paths = [str(Path(asset["path"])) for asset in assets if asset.get("path")]
    if not read_paths:
        return update_survey_output(
            state,
            {
                "status": "failed",
                "reason": (
                    f"genome_survey jellyfish has no readable input paths for family "
                    f"{input_family!r} (survey_input_refs="
                    f"{output.get('survey_input_refs') or []})"
                ),
            },
        )
    missing = [path for path in read_paths if not Path(path).exists()]
    if missing:
        preview = ", ".join(missing[:3])
        more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
        return update_survey_output(
            state,
            {
                "status": "failed",
                "reason": (
                    f"genome_survey jellyfish input paths missing on disk: "
                    f"{preview}{more}"
                ),
            },
        )

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        dict(output),
        stage="genome_survey",
        method_name=METHOD_NAME,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_survey_output(state, blocked)

    working_dir = stage_work_dir(state, "genome_survey", "jellyfish")
    working_dir.mkdir(parents=True, exist_ok=True)

    kmer_db = working_dir / "jellyfish_kmers.jf"
    kmer_hist = working_dir / "kmer_hist.tsv"
    script_path = working_dir / "run_jellyfish.sh"
    reads_arg = " ".join(shlex.quote(path) for path in read_paths)
    script_path.write_text(
        "set -e\n"
        f"jellyfish count -C -m {int(params['kmer_size'])} "
        f"-s {shlex.quote(str(params['hash_size']))} "
        f"-t {int(params['threads'])} {reads_arg} -o {shlex.quote(str(kmer_db))}\n"
        f"jellyfish histo -t {int(params['threads'])} "
        f"{shlex.quote(str(kmer_db))} > {shlex.quote(str(kmer_hist))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:genome_survey:jellyfish",
        session_id=session_id,
        stage="genome_survey",
        method_name=METHOD_NAME,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[kmer_db, kmer_hist],
        env=output.get("env"),
        env_spec=env_spec,
        metadata={
            "input_asset_refs": output.get("survey_input_refs", []),
            "input_family": input_family,
            "kmer_size": params["kmer_size"],
            "kmer_db_path": str(kmer_db),
            "kmer_hist_path": str(kmer_hist),
        },
    )
    return freeze_method_attempts(
        state,
        LIFECYCLE,
        [attempt],
        stage_updates={
            "kmer_db_path": str(kmer_db),
            "kmer_hist_path": str(kmer_hist),
        },
    )


def collect_jellyfish_artifacts(state: TopLevelState) -> StateUpdate:
    """Collect Jellyfish k-mer database and histogram outputs."""
    output = survey_output(state)
    if output.get("status") != RAN_STATUS:
        return {}

    executions = load_method_executions(state, LIFECYCLE)
    if len(executions) != 1:
        raise ValueError("Jellyfish requires one execution attempt")
    attempt = executions[0].attempt
    artifacts = attempt_artifacts(
        state,
        attempt,
        {
            str(Path(output["kmer_db_path"])): GENOME_SURVEY_KMER_DB,
            str(Path(output["kmer_hist_path"])): GENOME_SURVEY_KMER_HIST,
        },
    )
    record_attempt_success(state, attempt, artifacts)
    kmer_db_ref = str(artifacts[0].file_path)
    kmer_hist_ref = str(artifacts[1].file_path)

    substeps = dict(output.get("substeps") or {})
    kmer_count = substeps.get("kmer_count") or {}
    substeps["kmer_count"] = {
        **kmer_count,
        "name": "kmer_count",
        "status": "executed",
        "method_name": attempt.method_name,
        "kmer_db_ref": kmer_db_ref,
        "kmer_hist_ref": kmer_hist_ref,
    }
    return update_survey_output(
        state,
        {
            "status": COLLECTED_STATUS,
            "kmer_db_path": kmer_db_ref,
            "kmer_hist_path": kmer_hist_ref,
            "kmer_db_artifact_ref": kmer_db_ref,
            "kmer_hist_artifact_ref": kmer_hist_ref,
            "artifact_refs": [
                *list(output.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "substeps": substeps,
        },
    )


def _assets_for_family(
    assets: ResolvedSurveyAssetsCursor,
    family: str,
) -> list[SurveyAssetCursor]:
    if family == "clean_short_dna_reads":
        return list(assets.get("clean_short_dna_reads") or [])
    if family == "raw_short_dna_reads":
        return list(assets.get("raw_short_dna_reads") or [])
    if family == "clean_long_reads":
        return list(assets.get("clean_long_reads") or [])
    if family == "raw_long_reads":
        return list(assets.get("raw_long_reads") or [])
    return []
