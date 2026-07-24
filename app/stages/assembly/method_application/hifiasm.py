"""Hifiasm-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex

from app.configuration import load_descriptor_registry
from app.domains.assembly.constants import HIFIASM_METHOD
from app.domains.read_inputs import AssemblyInputError, resolve_assembly_read_paths
from app.execution.attempt_owners import ParameterAttemptOwner
from app.execution.env_dispatch import resolve_method_env
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    load_method_executions,
)
from app.schemas.state import TopLevelState
from app.stages.assembly.command_builder import (
    ParameterCommandError,
    parameter_sources_payload,
    render_assembly_cli,
    resolve_assembly_parameters,
)
from app.stages.assembly.method_application.support import (
    approved_assembly_read_type,
    collect_method_artifacts,
    fail_update,
    freeze_parameterized_attempt,
    load_assembly_execution_contract,
    load_current_approved_recommendation,
)
from app.stages.base import absolute_data_dir, stage_output, update_stage_output
from app.storage.paths import SessionPaths

LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly",
    name="hifiasm",
    method_name=HIFIASM_METHOD,
    frozen_status="hifiasm_frozen",
    ran_status="hifiasm_ran",
    owner=ParameterAttemptOwner(),
)


def build_hifiasm_graph():
    return build_method_graph(
        LIFECYCLE, freeze_hifiasm_attempt, collect_hifiasm_artifacts
    )


def freeze_hifiasm_attempt(state: TopLevelState) -> dict:
    """Freeze a local hifiasm attempt for the selected primary candidate."""
    output = stage_output(state, "assembly")
    if output.get("status") != "hifiasm_execution_planned":
        return {}
    if HIFIASM_METHOD not in load_descriptor_registry().methods:
        return fail_update(state, f"assembly descriptors missing: [{HIFIASM_METHOD!r}]")

    try:
        read_paths = resolve_assembly_read_paths(
            output.get("input_ref"), output.get("input_type")
        )
    except AssemblyInputError as exc:
        return fail_update(state, f"Hifiasm local execution {exc}")
    try:
        execution_contract = load_assembly_execution_contract(
            output,
            method_id=HIFIASM_METHOD,
        )
    except ValueError as exc:
        return fail_update(state, f"Hifiasm execution contract is invalid: {exc}")
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        {"env": execution_contract.environment.values},
        stage="assembly",
        method_name=HIFIASM_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if env_blocked is not None:
        return update_stage_output(state, "assembly", env_blocked)

    approved_bundle, recommendation_error = load_current_approved_recommendation(
        state,
        output,
        method_id=HIFIASM_METHOD,
        label="Hifiasm",
    )
    if recommendation_error or approved_bundle is None:
        return fail_update(
            state,
            recommendation_error or "Hifiasm approved parameter attempt is missing",
        )
    try:
        read_type = approved_assembly_read_type(approved_bundle)
    except ValueError as exc:
        return fail_update(state, f"Hifiasm approved read type is invalid: {exc}")
    thread_binding = execution_contract.l0_executor_values()
    approved_attempt = approved_bundle.attempt
    parameter_attempt_id = approved_attempt.attempt_id
    paths = SessionPaths.from_run_dir(data_dir)
    attempt_scope = f"{parameter_attempt_id:03d}"
    working_dir = paths.work_path("assembly", "attempts", attempt_scope, "hifiasm")
    output_dir = working_dir / "hifiasm_out"
    artifact_dir = paths.artifact_path(
        "assembly", "attempts", attempt_scope, "outputs", "hifiasm"
    )
    working_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    prefix = output_dir / "hifiasm"
    gfa = output_dir / "hifiasm.bp.p_ctg.gfa"
    fallback_gfa = output_dir / "hifiasm.p_ctg.gfa"
    script = working_dir / "run_hifiasm.sh"
    try:
        catalog = approved_bundle.chain.catalog
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=approved_bundle.chain.input_snapshot,
            approved_l2=approved_bundle.approved_l2,
            l0_executor_values={
                "assembly_output_prefix": str(prefix),
                "assembly_input_reads": [str(path) for path in read_paths],
                **thread_binding,
            },
        )
        argv = render_assembly_cli(catalog, resolved)
        parameter_sources = parameter_sources_payload(resolved)
    except ParameterCommandError as exc:
        return fail_update(state, f"Hifiasm parameter sources are invalid: {exc}")
    command = " ".join(shlex.quote(arg) for arg in argv)
    script.write_text(
        "set -e\n"
        f"{command}\n"
        f"if [ -f {shlex.quote(str(gfa))} ]; then gfa={shlex.quote(str(gfa))};"
        f" elif [ -f {shlex.quote(str(fallback_gfa))} ];"
        f" then gfa={shlex.quote(str(fallback_gfa))};"
        " else echo 'expected hifiasm primary contig GFA missing' >&2; exit 1; fi\n"
        f'awk \'/^S/{{print ">"$2"\\n"$3}}\' "$gfa" > {shlex.quote(str(draft))}\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return freeze_parameterized_attempt(
        state,
        LIFECYCLE,
        label="Hifiasm",
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
            "read_profile": read_type,
            "threads": resolved.effective_parameters["threads"],
            "parameter_sources": parameter_sources,
            "argv": argv,
            "hifiasm_output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "parameter_attempt_id": parameter_attempt_id,
        },
        stage_updates={
            "hifiasm_output_dir": str(output_dir),
        },
    )


def collect_hifiasm_artifacts(state: TopLevelState) -> dict:
    [execution] = load_method_executions(state, LIFECYCLE)
    attempt = execution.attempt
    metadata = dict(attempt.metadata or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        execution=execution,
        substep_name="hifiasm_assembly",
        extra_artifacts=[],
        bundle_logs={},
        bundle_metadata={
            "read_profile": metadata.get("read_profile"),
            "threads": metadata.get("threads"),
        },
    )
