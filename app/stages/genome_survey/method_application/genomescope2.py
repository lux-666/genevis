"""GenomeScope2 model method subgraph and implementation."""

from __future__ import annotations

import shlex
from pathlib import Path

from app.execution.env_dispatch import resolve_method_env
from app.execution.evidence import load_frozen_attempt, persist_frozen_attempt
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import GENOME_SURVEY_MODEL_SUMMARY
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir, stage_work_dir
from app.stages.genome_survey.method_application.lifecycle import (
    attempt_artifacts,
    build_method_graph,
    genome_survey_output,
    record_attempt_success,
    run_method_attempt,
    update_genome_survey_output,
    validate_attempt_execution,
)

METHOD_NAME = "genome_survey.model.genomescope2"
FROZEN_STATUS = "genomescope2_frozen"
RAN_STATUS = "genomescope2_ran"
COLLECTED_STATUS = "genomescope2_collected"
FROZEN_ATTEMPT_KEY = "genomescope2_frozen_attempt_ref"
EXECUTION_RESULT_KEY = "genomescope2_execution_result"


def build_genomescope2_graph():
    return build_method_graph(
        freeze_genomescope2_attempt,
        run_genomescope2_attempt,
        collect_genomescope2_artifacts,
        frozen_status=FROZEN_STATUS,
        ran_status=RAN_STATUS,
    )


def freeze_genomescope2_attempt(state: TopLevelState) -> dict:
    """Freeze the GenomeScope2 model attempt."""
    output = genome_survey_output(state)
    if output.get("status") != "jellyfish_collected":
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

    working_dir = stage_work_dir(state, "genome_survey", "genomescope2")
    working_dir.mkdir(parents=True, exist_ok=True)

    params = dict(output["survey_parameters"])
    output_dir = working_dir / "output"
    summary_path = output_dir / "summary.txt"
    script_path = working_dir / "run_genomescope2.sh"
    script_path.write_text(
        "set -e\n"
        f"mkdir -p {shlex.quote(str(output_dir))}\n"
        f"genomescope2 -i {shlex.quote(str(Path(output['kmer_hist_path'])))} "
        f"-o {shlex.quote(str(output_dir))} -k {int(params['kmer_size'])}\n"
        f"test -f {shlex.quote(str(summary_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:genome_survey:genomescope2",
        session_id=session_id,
        stage="genome_survey",
        method_name=METHOD_NAME,
        script_path=script_path,
        working_dir=working_dir,
        expected_outputs=[summary_path],
        env=output.get("env"),
        env_spec=env_spec,
        metadata={
            "kmer_hist_path": output.get("kmer_hist_path"),
            "summary_path": str(summary_path),
        },
    )
    attempt_ref = persist_frozen_attempt(state, attempt)
    return update_genome_survey_output(
        state,
        {
            "status": FROZEN_STATUS,
            FROZEN_ATTEMPT_KEY: attempt_ref.model_dump(mode="json"),
            "genomescope2_summary_path": str(summary_path),
            "genomescope2_output_dir": str(output_dir),
        },
    )


def run_genomescope2_attempt(state: TopLevelState) -> dict:
    """Run the frozen GenomeScope2 attempt locally."""
    return run_method_attempt(
        state,
        frozen_status=FROZEN_STATUS,
        frozen_attempt_key=FROZEN_ATTEMPT_KEY,
        execution_result_key=EXECUTION_RESULT_KEY,
        ran_status=RAN_STATUS,
    )


def collect_genomescope2_artifacts(state: TopLevelState) -> dict:
    """Collect the GenomeScope2 summary output."""
    output = genome_survey_output(state)
    if output.get("status") != RAN_STATUS:
        return {}

    attempt = load_frozen_attempt(
        state,
        output[FROZEN_ATTEMPT_KEY],
        expected_stage="genome_survey",
        expected_method_name=METHOD_NAME,
    )
    validate_attempt_execution(state, attempt, output[EXECUTION_RESULT_KEY])
    artifacts = attempt_artifacts(
        state,
        attempt,
        {str(Path(output["genomescope2_summary_path"])): GENOME_SURVEY_MODEL_SUMMARY},
    )
    record_attempt_success(state, attempt, artifacts)
    summary_ref = str(artifacts[0].file_path)

    substeps = dict(output.get("substeps") or {})
    substeps["genome_model"] = {
        **dict(substeps.get("genome_model") or {}),
        "name": "genome_model",
        "status": "executed",
        "method_name": attempt.method_name,
        "summary_ref": summary_ref,
    }
    return update_genome_survey_output(
        state,
        {
            "status": COLLECTED_STATUS,
            "genomescope2_summary_path": summary_ref,
            "genomescope2_summary_ref": summary_ref,
            "genomescope2_output_dir_ref": str(Path(summary_ref).parent),
            "artifact_refs": [
                *list(output.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "substeps": substeps,
        },
    )
