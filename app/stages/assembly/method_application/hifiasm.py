"""Hifiasm-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import cast

from app.configuration import load_descriptor_registry
from app.domains.assembly.constants import HIFIASM_METHOD
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
from app.stages.assembly.method_application.hifiasm_outputs import (
    hifiasm_native_output_declarations,
)
from app.stages.assembly.method_application.support import (
    approved_assembly_read_type,
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
from app.stages.base import absolute_data_dir
from app.storage.paths import SessionPaths
from app.storage.publish import shell_publish_file

LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly",
    name="hifiasm",
    method_name=HIFIASM_METHOD,
    frozen_status="hifiasm_frozen",
    ran_status="hifiasm_ran",
    owner=ParameterAttemptOwner(),
)


def build_hifiasm_graph() -> CompiledMethodGraph:
    return build_method_graph(
        LIFECYCLE, freeze_hifiasm_attempt, collect_hifiasm_artifacts
    )


def freeze_hifiasm_attempt(state: TopLevelState) -> StateUpdate:
    """Freeze a local hifiasm attempt for the selected primary candidate."""
    output = assembly_stage_output(state)
    if output.get("status") != "hifiasm_execution_planned":
        return {}
    if HIFIASM_METHOD not in load_descriptor_registry().methods:
        return fail_update(state, f"assembly descriptors missing: [{HIFIASM_METHOD!r}]")

    try:
        read_paths = resolve_assembly_read_paths(
            output.get("input_ref"),
            cast(AssemblyReadInputType | None, output.get("input_type")),
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
        return update_assembly_stage_output(
            state, validated_assembly_stage_payload(env_blocked)
        )

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
    temp_draft = working_dir / "draft_assembly.fasta"
    prefix = output_dir / "hifiasm"
    gfa_bp = output_dir / "hifiasm.bp.p_ctg.gfa"
    gfa_legacy = output_dir / "hifiasm.p_ctg.gfa"
    script = working_dir / "run_hifiasm.sh"
    try:
        catalog = approved_bundle.chain.catalog
        l0_values: dict[str, object] = {
            "assembly_output_prefix": str(prefix),
            "assembly_input_reads": [str(path) for path in read_paths],
            **thread_binding,
        }
        if output.get("use_ultralong_ont"):
            ultralong = list(output.get("ultralong_read_refs") or [])
            if not ultralong:
                return fail_update(
                    state,
                    "Hifiasm --ul was enabled without registered ONT reads",
                )
            l0_values["assembly_ultralong_reads"] = ultralong
        if output.get("use_hic_phasing"):
            hic_r1 = list(output.get("hic_r1_reads") or [])
            hic_r2 = list(output.get("hic_r2_reads") or [])
            if not hic_r1 or len(hic_r1) != len(hic_r2):
                return fail_update(
                    state,
                    "Hifiasm Hi-C phasing requires equal non-empty R1/R2 lanes",
                )
            l0_values["assembly_hic_r1_reads"] = hic_r1
            l0_values["assembly_hic_r2_reads"] = hic_r2
        resolved = resolve_assembly_parameters(
            catalog=catalog,
            snapshot=approved_bundle.chain.input_snapshot,
            approved_l2=approved_bundle.approved_l2,
            l0_executor_values=l0_values,
        )
        argv = render_assembly_cli(catalog, resolved)
        parameter_sources = parameter_sources_payload(resolved)
    except ParameterCommandError as exc:
        return fail_update(state, f"Hifiasm parameter sources are invalid: {exc}")
    command = " ".join(shlex.quote(arg) for arg in argv)
    script.write_text(
        "set -e\n"
        f"{command}\n"
        f"if [ -f {shlex.quote(str(gfa_bp))} ]; then gfa={shlex.quote(str(gfa_bp))};"
        f" elif [ -f {shlex.quote(str(gfa_legacy))} ];"
        f" then gfa={shlex.quote(str(gfa_legacy))};"
        " else echo 'expected hifiasm primary contig GFA missing' >&2; exit 1; fi\n"
        f'awk \'/^S/{{print ">"$2"\\n"$3}}\' "$gfa" > {shlex.quote(str(temp_draft))}\n'
        f"{shell_publish_file(temp_draft, draft)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    try:
        primary_deps = build_read_input_dependencies(
            state, output, "primary", read_paths
        )
        ul_deps = (
            build_read_input_dependencies(
                state,
                output,
                "ultralong",
                list(output.get("ultralong_read_refs") or []),
            )
            if output.get("use_ultralong_ont")
            else []
        )
        hic_r1_deps = (
            build_read_input_dependencies(
                state,
                output,
                "hic_r1",
                list(output.get("hic_r1_reads") or []),
            )
            if output.get("use_hic_phasing")
            else []
        )
        hic_r2_deps = (
            build_read_input_dependencies(
                state,
                output,
                "hic_r2",
                list(output.get("hic_r2_reads") or []),
            )
            if output.get("use_hic_phasing")
            else []
        )
        input_dependencies = primary_deps + ul_deps + hic_r1_deps + hic_r2_deps
    except ValueError as exc:
        return fail_update(
            state, f"Hifiasm input dependency cardinality error: {exc}"
        )
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
        input_dependencies=input_dependencies,
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


def collect_hifiasm_artifacts(state: TopLevelState) -> StateUpdate:
    [execution] = load_method_executions(state, LIFECYCLE)
    attempt = execution.attempt
    metadata = dict(attempt.metadata or {})
    output_dir = Path(str(metadata.get("hifiasm_output_dir") or ""))
    native_outputs = hifiasm_native_output_declarations(
        output_dir,
        output_prefix="hifiasm",
        role_namespace="hifiasm",
        artifact_type="assembly.hifiasm_native_gfa",
    )
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        execution=execution,
        substep_name="hifiasm_assembly",
        native_outputs=native_outputs,
    )
