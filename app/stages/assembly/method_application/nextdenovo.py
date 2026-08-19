"""NextDenovo-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.configuration import load_descriptor_registry
from app.control.workflow_plan import current_workflow_step_id
from app.domains.assembly.constants import NEXTDENOVO_METHOD
from app.domains.read_inputs import AssemblyInputError, resolve_assembly_read_paths
from app.execution.attempt_owners import ParameterAttemptOwner
from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    CompiledMethodGraph,
    MethodExecutionLifecycle,
    build_method_graph,
    load_method_executions,
)
from app.execution.parameter_attempts import (
    current_parameter_attempt,
)
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly.command_builder import (
    ParameterCommandError,
    parameter_sources_payload,
    render_assembly_config,
    resolve_assembly_parameters,
)
from app.stages.assembly.method_application.support import (
    AssemblyNativeOutputDeclaration,
    approved_method_read_type,
    assembly_primary_method_outcome_policy,
    build_read_input_dependencies,
    collect_method_artifacts,
    fail_update,
    freeze_parameterized_attempt,
    load_assembly_execution_contract,
    load_current_approved_recommendation,
)
from app.stages.assembly.state import (
    assembly_stage_output,
    update_assembly_stage_output,
    validated_assembly_stage_payload,
)
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.storage.publish import publish_file, shell_publish_file

LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly",
    name="nextdenovo",
    method_name=NEXTDENOVO_METHOD,
    frozen_status="nextdenovo_frozen",
    ran_status="nextdenovo_ran",
    owner=ParameterAttemptOwner(),
    outcome_policy=assembly_primary_method_outcome_policy,
)


def build_nextdenovo_graph() -> CompiledMethodGraph:
    return build_method_graph(
        LIFECYCLE, freeze_nextdenovo_attempt, collect_nextdenovo_artifacts
    )


def freeze_nextdenovo_attempt(state: TopLevelState) -> StateUpdate:
    """Freeze a local NextDenovo attempt for the selected primary candidate."""
    output = assembly_stage_output(state)
    if output.get("status") != "nextdenovo_execution_planned":
        return {}
    if NEXTDENOVO_METHOD not in load_descriptor_registry().methods:
        return fail_update(
            state, f"assembly descriptors missing: [{NEXTDENOVO_METHOD!r}]"
        )
    try:
        execution_contract = load_assembly_execution_contract(
            output,
            method_id=NEXTDENOVO_METHOD,
        )
    except ValueError as exc:
        return fail_update(state, f"NextDenovo execution contract is invalid: {exc}")
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        {"env": execution_contract.environment.values},
        stage="assembly",
        method_name=NEXTDENOVO_METHOD,
        workflow_step_id=current_workflow_step_id(state, expected_stage="assembly"),
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if env_blocked is not None:
        return update_assembly_stage_output(
            state, validated_assembly_stage_payload(env_blocked)
        )

    approved_bundle, recommendation_error = load_current_approved_recommendation(
        state,
        output,
        method_id=NEXTDENOVO_METHOD,
        label="NextDenovo",
    )
    if recommendation_error or approved_bundle is None:
        return fail_update(
            state,
            recommendation_error or "NextDenovo approved parameter attempt is missing",
        )
    try:
        read_type = approved_method_read_type(
            approved_bundle,
            method_id=NEXTDENOVO_METHOD,
        )
    except ValueError as exc:
        return fail_update(state, f"NextDenovo approved read type is invalid: {exc}")
    thread_binding = execution_contract.l0_executor_values()
    approved_attempt = approved_bundle.attempt
    parameter_attempt_id = approved_attempt.attempt_id
    fofn, read_paths, error = _fofn(
        state,
        output.get("input_ref"),
        output.get("input_type"),
        attempt_id=parameter_attempt_id,
    )
    if error:
        return fail_update(state, error)
    attempt_scope = f"{parameter_attempt_id:03d}"
    working_dir = stage_work_dir(
        state, "assembly", "attempts", attempt_scope, "nextdenovo"
    )
    nextdenovo_work_dir = working_dir / "nextdenovo_work"
    artifact_dir = stage_artifacts_dir(
        state, "assembly", "attempts", attempt_scope, "outputs", "nextdenovo"
    )
    working_dir.mkdir(parents=True, exist_ok=True)
    nextdenovo_work_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    run_cfg = working_dir / "run.cfg"
    script = working_dir / "run_nextdenovo.sh"
    try:
        catalog = approved_bundle.chain.catalog
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=approved_bundle.chain.input_snapshot,
            approved_l2=approved_bundle.approved_l2,
            l0_executor_values={
                "assembly_method_read_type": read_type,
                "assembly_input_reads": str(fofn),
                "assembly_output_dir": str(nextdenovo_work_dir),
                **thread_binding,
            },
        )
        rendered_parameters = render_assembly_config(catalog, resolved)
        parameter_sources = parameter_sources_payload(resolved)
    except ParameterCommandError as exc:
        return fail_update(state, f"NextDenovo parameter sources are invalid: {exc}")
    run_cfg.write_text(_run_cfg(**rendered_parameters), encoding="utf-8")
    candidates = [
        nextdenovo_work_dir / "03.ctg_graph" / "nd.asm.fasta",
        nextdenovo_work_dir / "03.ctg_graph" / "nextdenovo.asm.fasta",
        nextdenovo_work_dir / "nextdenovo.asm.fasta",
    ]
    output_checks = "\n".join(
        f"if [ -f {shlex.quote(str(path))} ]; then "
        f"{shell_publish_file(path, draft)}; exit 0; fi"
        for path in candidates
    )
    argv = [catalog.tool, str(run_cfg)]
    command = " ".join(shlex.quote(arg) for arg in argv)
    script.write_text(
        "set -e\n"
        f"{command}\n"
        f"{output_checks}\n"
        "echo 'expected NextDenovo draft assembly missing' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    try:
        input_dependencies = build_read_input_dependencies(
            state, output, "primary", read_paths
        )
    except ValueError as exc:
        return fail_update(
            state, f"NextDenovo input dependency cardinality error: {exc}"
        )
    return freeze_parameterized_attempt(
        state,
        LIFECYCLE,
        label="NextDenovo",
        execution_contract=execution_contract,
        approved_attempt=approved_attempt,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=[draft],
        env_spec=env_spec,
        input_dependencies=input_dependencies,
        command_payload={
            "argv": argv,
            "command": command,
            "script_path": str(script),
            "working_dir": str(working_dir),
            "run_cfg": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
            "draft_assembly_path": str(draft),
            "parameter_sources": parameter_sources,
            "rendered_parameters": rendered_parameters,
        },
        frozen_parameters_payload={
            "parameter_sources": parameter_sources,
            "rendered_parameters": rendered_parameters,
        },
        frozen_attempt_metadata={
            "input_ref": output.get("input_ref"),
            "input_type": output.get("input_type"),
            "input_fofn": str(fofn),
            "read_paths": [str(path) for path in read_paths],
            "read_profile": rendered_parameters["read_type"],
            **rendered_parameters,
            "parameter_sources": parameter_sources,
            "rendered_parameters": rendered_parameters,
            "argv": argv,
            "run_cfg": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
            "draft_assembly_path": str(draft),
            "parameter_attempt_id": parameter_attempt_id,
        },
        stage_updates={
            "nextdenovo_run_cfg_path": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
        },
    )


def collect_nextdenovo_artifacts(state: TopLevelState) -> StateUpdate:
    output = assembly_stage_output(state)
    [execution] = load_method_executions(state, LIFECYCLE)
    run_cfg_value = output.get("nextdenovo_run_cfg_path")
    if not run_cfg_value:
        return fail_update(state, "NextDenovo run.cfg path is missing")
    attempt_id = current_parameter_attempt(
        output,
        method_id=NEXTDENOVO_METHOD,
        label="NextDenovo",
    ).attempt_id
    durable_run_cfg = stage_artifacts_dir(
        state,
        "assembly",
        "attempts",
        f"{attempt_id:03d}",
        "outputs",
        "nextdenovo",
        "run.cfg",
    )
    try:
        publish_file(Path(str(run_cfg_value)), durable_run_cfg)
    except (FileNotFoundError, FileExistsError, OSError) as exc:
        return fail_update(state, f"NextDenovo run.cfg could not be published: {exc}")

    collected = collect_method_artifacts(
        state,
        LIFECYCLE,
        execution=execution,
        substep_name="nextdenovo_assembly",
        native_outputs=[
            AssemblyNativeOutputDeclaration(
                path=durable_run_cfg,
                artifact_type="assembly.nextdenovo_run_cfg",
                artifact_class="config",
                native_role="nextdenovo.run_config",
                media_type="text/plain",
            )
        ],
    )
    stage_outputs = dict(collected.get("stage_outputs") or {})
    assembly = dict(stage_outputs.get("assembly") or {})
    if assembly:
        assembly["nextdenovo_run_cfg_path"] = str(durable_run_cfg)
        stage_outputs["assembly"] = assembly
        collected["stage_outputs"] = stage_outputs
    return collected


def _fofn(
    state: TopLevelState,
    input_ref: Any,
    input_type: Any,
    *,
    attempt_id: int,
) -> tuple[Path | None, list[Path], str | None]:
    try:
        paths = resolve_assembly_read_paths(input_ref, input_type)
    except AssemblyInputError as exc:
        return None, [], f"NextDenovo local execution {exc}"

    fofn = stage_work_dir(
        state, "assembly", "attempts", f"{attempt_id:03d}", "nextdenovo", "input.fofn"
    )
    fofn.parent.mkdir(parents=True, exist_ok=True)
    fofn.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
    return fofn, paths, None


def _run_cfg(
    *,
    read_type: str,
    input_fofn: str,
    workdir: str,
    genome_size: int,
    threads: int,
    read_cutoff: int | None = None,
    nextgraph_a: int | None = None,
) -> str:
    read_cutoff_line = (
        f"read_cutoff = {read_cutoff}\n" if read_cutoff is not None else ""
    )
    nextgraph_line = (
        f"nextgraph_options = -a {nextgraph_a}\n" if nextgraph_a is not None else ""
    )
    return (
        "[General]\n"
        "job_type = local\n"
        "job_prefix = nextdenovo\n"
        "task = all\n"
        "rewrite = yes\n"
        "deltmp = yes\n"
        "parallel_jobs = 1\n"
        "input_type = raw\n"
        f"read_type = {read_type}\n"
        f"input_fofn = {input_fofn}\n"
        f"workdir = {workdir}\n\n"
        "[correct_option]\n"
        f"{read_cutoff_line}genome_size = {genome_size}\n"
        f"sort_options = -m 1g -t {threads}\n"
        f"minimap2_options_raw = -t {threads}\n"
        "pa_correction = 3\n"
        f"correction_options = -p {threads}\n\n"
        "[assemble_option]\n"
        f"minimap2_options_cns = -t {threads}\n"
        f"{nextgraph_line}"
    )
