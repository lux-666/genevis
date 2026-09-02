"""GenomeScope2 model method subgraph and implementation."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from app.control.workflow_plan import current_workflow_step, current_workflow_step_id
from app.execution.env_dispatch import resolve_method_env
from app.execution.input_integrity import artifact_input_dependency
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_outcomes import MethodOutcome
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    GENOME_SURVEY_KMER_DB,
    GENOME_SURVEY_KMER_HIST,
    GENOME_SURVEY_MODEL_SUMMARY,
    ArtifactRefJson,
)
from app.schemas.execution import ArtifactInputDependency
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir, stage_work_dir
from app.stages.genome_survey.method_application.support import (
    attempt_artifacts,
    record_attempt_success,
    survey_output,
    update_survey_output,
)
from app.workflow import is_terminal_failure

METHOD_NAME = "genome_survey.model.genomescope2"
FROZEN_STATUS = "genomescope2_frozen"
RAN_STATUS = "genomescope2_ran"
COLLECTED_STATUS = "genomescope2_collected"


def genomescope2_outcome(state: TopLevelState) -> MethodOutcome | None:
    """Return GenomeScope2's disposition without settling a retry path."""

    if not _is_selected(state):
        return None
    output = survey_output(state)
    substep = (output.get("substeps") or {}).get("genome_model") or {}
    if substep.get("status") == "executed":
        return MethodOutcome(METHOD_NAME, "completed")
    if substep.get("status") == "skipped":
        return MethodOutcome(
            METHOD_NAME,
            "skipped",
            reason=str(substep.get("skip_reason") or "local_execution_not_enabled"),
        )
    if output.get("status") == "skipped":
        return MethodOutcome(METHOD_NAME, "skipped", reason="no_usable_reads")
    if not is_terminal_failure(output.get("status")):
        return None
    if not _has_execution_result(output):
        return MethodOutcome(METHOD_NAME, "not_started")
    return MethodOutcome(
        METHOD_NAME,
        "failed",
        reason=str(output.get("reason") or "genomescope2_failed"),
    )


def _is_selected(state: TopLevelState) -> bool:
    step = current_workflow_step(state)
    roster = step.get("selected_method_names") if step is not None else None
    return isinstance(roster, list) and METHOD_NAME in roster


def _has_execution_result(output: Mapping[str, object]) -> bool:
    attempts = output.get("execution_attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        attempt.get("method_id") == METHOD_NAME
        and attempt.get("status") in {"executed", "execution_failed"}
        for attempt in attempts
        if isinstance(attempt, Mapping)
    )


LIFECYCLE = MethodExecutionLifecycle(
    stage="genome_survey",
    name="genomescope2",
    method_name=METHOD_NAME,
    frozen_status=FROZEN_STATUS,
    ran_status=RAN_STATUS,
    outcome_policy=genomescope2_outcome,
)


def build_genomescope2_graph() -> CompiledStateGraph[
    TopLevelState, None, TopLevelState, TopLevelState
]:
    return build_method_graph(
        LIFECYCLE,
        freeze_genomescope2_attempt,
        collect_genomescope2_artifacts,
    )


def freeze_genomescope2_attempt(state: TopLevelState) -> StateUpdate:
    """Freeze the GenomeScope2 model attempt."""
    output = survey_output(state)
    if output.get("status") != "jellyfish_collected":
        return {}

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        dict(output),
        stage="genome_survey",
        method_name=METHOD_NAME,
        workflow_step_id=current_workflow_step_id(
            state, expected_stage="genome_survey"
        ),
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_survey_output(state, blocked)

    working_dir = stage_work_dir(state, "genome_survey", "genomescope2")
    working_dir.mkdir(parents=True, exist_ok=True)

    params = output["survey_parameters"]
    output_dir = working_dir / "output"
    summary_path = output_dir / "summary.txt"
    script_path = working_dir / "run_genomescope2.sh"
    script_path.write_text(
        "set -e\n"
        f"mkdir -p {shlex.quote(str(output_dir))}\n"
        f"genomescope2 -i {shlex.quote(str(Path(output['kmer_hist_path'])))} "
        f"-o {shlex.quote(str(output_dir))} -k {int(params['kmer_size'])} "
        f"-p {int(params.get('ploidy', 2))}\n"
        f"test -f {shlex.quote(str(summary_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    try:
        db_dependency = _artifact_dependency(
            state,
            output["kmer_db_artifact_ref"],
            output["kmer_db_path"],
            expected_type=GENOME_SURVEY_KMER_DB,
        )
        input_dependency = _artifact_dependency(
            state,
            output["kmer_hist_artifact_ref"],
            output["kmer_hist_path"],
            expected_type=GENOME_SURVEY_KMER_HIST,
        )
    except (OSError, ValueError) as exc:
        return update_survey_output(
            state,
            {
                "status": "failed",
                "failure_kind": "invalid_input",
                "reason": f"GenomeScope2 artifact dependency is invalid: {exc}",
            },
        )
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
        input_dependencies=[input_dependency],
        metadata={
            "kmer_hist_path": output.get("kmer_hist_path"),
            "summary_path": str(summary_path),
        },
    )
    return freeze_method_attempts(
        state,
        LIFECYCLE,
        [attempt],
        stage_updates={
            "kmer_db_artifact_ref": db_dependency.artifact_ref.model_dump(mode="json"),
            "kmer_hist_artifact_ref": input_dependency.artifact_ref.model_dump(
                mode="json"
            ),
            "genomescope2_summary_path": str(summary_path),
            "genomescope2_output_dir": str(output_dir),
        },
    )


def collect_genomescope2_artifacts(state: TopLevelState) -> StateUpdate:
    """Collect the GenomeScope2 summary output."""
    output = survey_output(state)
    if output.get("status") != RAN_STATUS:
        return {}

    executions = load_method_executions(state, LIFECYCLE)
    if len(executions) != 1:
        raise ValueError("GenomeScope2 requires one execution attempt")
    attempt = executions[0].attempt
    artifacts = attempt_artifacts(
        state,
        attempt,
        {str(Path(output["genomescope2_summary_path"])): GENOME_SURVEY_MODEL_SUMMARY},
    )
    record_attempt_success(state, attempt, artifacts)
    summary_ref = str(artifacts[0].file_path)

    substeps = dict(output.get("substeps") or {})
    genome_model = substeps.get("genome_model") or {}
    substeps["genome_model"] = {
        **genome_model,
        "name": "genome_model",
        "status": "executed",
        "method_name": attempt.method_name,
        "summary_ref": summary_ref,
    }
    return update_survey_output(
        state,
        {
            "status": COLLECTED_STATUS,
            "genomescope2_summary_path": summary_ref,
            "genomescope2_summary_ref": summary_ref,
            "artifact_refs": [
                *list(output.get("artifact_refs") or []),
                *[str(artifact.file_path) for artifact in artifacts],
            ],
            "substeps": substeps,
        },
    )


def _artifact_dependency(
    state: TopLevelState,
    raw_ref: ArtifactRefJson,
    path: str,
    *,
    expected_type: str,
) -> ArtifactInputDependency:
    dependency = artifact_input_dependency(state, raw_ref, path)
    if dependency.artifact_ref.artifact_type != expected_type:
        raise ValueError(
            "artifact dependency has type "
            f"{dependency.artifact_ref.artifact_type!r}, expected {expected_type!r}"
        )
    return dependency
