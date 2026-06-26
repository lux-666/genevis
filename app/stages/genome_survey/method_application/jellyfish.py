"""Jellyfish count/histogram method subgraph and implementation."""

from __future__ import annotations

import shlex
from pathlib import Path

from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import GENOME_SURVEY_KMER_DB, GENOME_SURVEY_KMER_HIST
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir
from app.stages.genome_survey.method_application.lifecycle import (
    attempt_artifacts,
    build_method_graph,
    genome_survey_output,
    record_attempt_success,
    run_method_attempt,
    update_genome_survey_output,
)

METHOD_NAME = "genome_survey.kmer_count.jellyfish"
FROZEN_STATUS = "jellyfish_frozen"
RAN_STATUS = "jellyfish_ran"
COLLECTED_STATUS = "jellyfish_collected"
FROZEN_ATTEMPT_KEY = "jellyfish_frozen_attempt"
EXECUTION_RESULT_KEY = "jellyfish_execution_result"


def build_jellyfish_graph():
    return build_method_graph(
        freeze_jellyfish_attempt,
        run_jellyfish_attempt,
        collect_jellyfish_artifacts,
        frozen_status=FROZEN_STATUS,
        ran_status=RAN_STATUS,
    )


def freeze_jellyfish_attempt(state: TopLevelState) -> dict:
    """Freeze the Jellyfish count/histo attempt."""
    output = genome_survey_output(state)
    if output.get("status") != "local_execution_planned":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        output,
        stage="genome_survey",
        method_name=METHOD_NAME,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_genome_survey_output(state, blocked)

    working_dir = data_dir / "work" / "genome_survey" / session_id / "jellyfish"
    artifact_dir = data_dir / "artifacts" / "genome_survey" / session_id
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    params = dict(output["survey_parameters"])
    input_family = output.get("survey_input_family") or "clean_short_dna_reads"
    assets = (output.get("resolved_assets") or {}).get(input_family) or []
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
    return update_genome_survey_output(
        state,
        {
            "status": FROZEN_STATUS,
            FROZEN_ATTEMPT_KEY: attempt.model_dump(mode="json"),
            "kmer_db_path": str(kmer_db),
            "kmer_hist_path": str(kmer_hist),
        },
    )


def run_jellyfish_attempt(state: TopLevelState) -> dict:
    """Run the frozen Jellyfish attempt locally."""
    return run_method_attempt(
        state,
        frozen_status=FROZEN_STATUS,
        frozen_attempt_key=FROZEN_ATTEMPT_KEY,
        execution_result_key=EXECUTION_RESULT_KEY,
        ran_status=RAN_STATUS,
    )


def collect_jellyfish_artifacts(state: TopLevelState) -> dict:
    """Collect Jellyfish k-mer database, histogram, and command logs."""
    output = genome_survey_output(state)
    if output.get("status") != RAN_STATUS:
        return {}

    attempt = FrozenAttemptSpec.model_validate(output[FROZEN_ATTEMPT_KEY])
    artifacts = attempt_artifacts(
        attempt,
        output[EXECUTION_RESULT_KEY],
        {
            str(Path(output["kmer_db_path"])): GENOME_SURVEY_KMER_DB,
            str(Path(output["kmer_hist_path"])): GENOME_SURVEY_KMER_HIST,
        },
    )
    record_attempt_success(state, attempt, artifacts)

    substeps = dict(output.get("substeps") or {})
    substeps["kmer_count"] = {
        **dict(substeps.get("kmer_count") or {}),
        "name": "kmer_count",
        "status": "executed",
        "method_name": attempt.method_name,
        "kmer_db_ref": output["kmer_db_path"],
        "kmer_hist_ref": output["kmer_hist_path"],
    }
    return update_genome_survey_output(
        state,
        {
            "status": COLLECTED_STATUS,
            "kmer_db_artifact_ref": output["kmer_db_path"],
            "kmer_hist_artifact_ref": output["kmer_hist_path"],
            "artifact_refs": [
                *list(output.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "substeps": substeps,
        },
    )
