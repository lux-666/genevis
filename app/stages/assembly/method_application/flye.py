"""Flye-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import cast

from app.configuration import load_descriptor_registry
from app.domains.assembly.constants import FLYE_METHOD
from app.domains.read_inputs import (
    AssemblyInputError,
    AssemblyReadInputType,
    resolve_assembly_read_paths,
)
from app.execution.attempt_owners import ParameterAttemptOwner
from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    CompiledMethodGraph,
    MethodExecutionLifecycle,
    build_method_graph,
    load_method_executions,
)
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly.command_builder import (
    ParameterCommandError,
    parameter_sources_payload,
    render_assembly_cli,
    resolve_assembly_parameters,
)
from app.stages.assembly.method_application.support import (
    approved_method_read_type,
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
from app.stages.base import absolute_data_dir
from app.storage.paths import SessionPaths
from app.storage.publish import shell_publish_file

FLYE_READ_TYPE_FLAGS = {
    "nano-raw": "--nano-raw",
    "nano-hq": "--nano-hq",
    "pacbio-raw": "--pacbio-raw",
    "pacbio-hifi": "--pacbio-hifi",
}

LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly",
    name="flye",
    method_name=FLYE_METHOD,
    frozen_status="flye_frozen",
    ran_status="flye_ran",
    owner=ParameterAttemptOwner(),
)


def build_flye_graph() -> CompiledMethodGraph:
    return build_method_graph(LIFECYCLE, freeze_flye_attempt, collect_flye_artifacts)


def freeze_flye_attempt(state: TopLevelState) -> StateUpdate:
    """Freeze a local Flye attempt for the selected primary candidate."""
    output = assembly_stage_output(state)
    if output.get("status") != "flye_execution_planned":
        return {}
    if FLYE_METHOD not in load_descriptor_registry().methods:
        return fail_update(state, f"assembly descriptors missing: [{FLYE_METHOD!r}]")

    try:
        read_paths = resolve_assembly_read_paths(
            output.get("input_ref"),
            cast(AssemblyReadInputType | None, output.get("input_type")),
        )
    except AssemblyInputError as exc:
        return fail_update(state, f"Flye local execution {exc}")
    try:
        execution_contract = load_assembly_execution_contract(
            output,
            method_id=FLYE_METHOD,
        )
    except ValueError as exc:
        return fail_update(state, f"Flye execution contract is invalid: {exc}")
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        {"env": execution_contract.environment.values},
        stage="assembly",
        method_name=FLYE_METHOD,
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
        method_id=FLYE_METHOD,
        label="Flye",
    )
    if recommendation_error or approved_bundle is None:
        return fail_update(
            state,
            recommendation_error or "Flye approved parameter attempt is missing",
        )
    try:
        read_type = approved_method_read_type(
            approved_bundle,
            method_id=FLYE_METHOD,
        )
    except ValueError as exc:
        return fail_update(state, f"Flye approved read type is invalid: {exc}")
    thread_binding = execution_contract.l0_executor_values()
    approved_attempt = approved_bundle.attempt
    parameter_attempt_id = approved_attempt.attempt_id
    paths = SessionPaths.from_run_dir(data_dir)
    attempt_scope = f"{parameter_attempt_id:03d}"
    working_dir = paths.work_path("assembly", "attempts", attempt_scope, "flye")
    output_dir = working_dir / "flye_out"
    artifact_dir = paths.artifact_path(
        "assembly", "attempts", attempt_scope, "outputs", "flye"
    )
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    script = working_dir / "run_flye.sh"
    try:
        catalog = approved_bundle.chain.catalog
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=approved_bundle.chain.input_snapshot,
            approved_l2=approved_bundle.approved_l2,
            l0_executor_values={
                "assembly_method_read_type": read_type,
                "assembly_input_reads": [str(path) for path in read_paths],
                "assembly_output_dir": str(output_dir),
                **thread_binding,
            },
        )
        argv = render_assembly_cli(catalog, resolved)
        parameter_sources = parameter_sources_payload(resolved)
    except ParameterCommandError as exc:
        return fail_update(state, f"Flye parameter sources are invalid: {exc}")
    command = " ".join(shlex.quote(arg) for arg in argv)
    script.write_text(
        "set -e\n"
        f"{command}\n"
        f"test -f {shlex.quote(str(output_dir / 'assembly.fasta'))}\n"
        f"{shell_publish_file(output_dir / 'assembly.fasta', draft)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return freeze_parameterized_attempt(
        state,
        LIFECYCLE,
        label="Flye",
        execution_contract=execution_contract,
        approved_attempt=approved_attempt,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=[draft],
        env_spec=env_spec,
        command_payload={
            "argv": argv,
            "command": command,
            "script_path": str(script),
            "working_dir": str(working_dir),
            "output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "parameter_sources": parameter_sources,
        },
        frozen_parameters_payload={
            "parameter_sources": parameter_sources,
        },
        frozen_attempt_metadata={
            "input_ref": output.get("input_ref"),
            "input_type": output.get("input_type"),
            "read_paths": [str(path) for path in read_paths],
            "read_profile": resolved.effective_parameters["read_type"],
            "estimated_genome_size": resolved.effective_parameters["genome_size"],
            "threads": resolved.effective_parameters["threads"],
            "parameter_sources": parameter_sources,
            "argv": argv,
            "flye_output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "parameter_attempt_id": parameter_attempt_id,
        },
        stage_updates={
            "flye_output_dir": str(output_dir),
        },
    )


def collect_flye_artifacts(state: TopLevelState) -> StateUpdate:
    output = assembly_stage_output(state)
    [execution] = load_method_executions(state, LIFECYCLE)
    attempt = execution.attempt
    metadata = dict(attempt.metadata or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        execution=execution,
        substep_name="flye_assembly",
        extra_artifacts=[
            (Path(output.get("flye_output_dir", "")) / "flye.log", "assembly.flye_log")
        ],
        bundle_logs={},
        bundle_metadata={
            "read_profile": metadata.get("read_profile"),
            "estimated_genome_size": metadata.get("estimated_genome_size"),
            "threads": metadata.get("threads"),
        },
    )
