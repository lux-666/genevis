"""Hifiasm ONT-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import cast

from app.configuration import load_descriptor_registry
from app.control.workflow_plan import current_workflow_step_id
from app.domains.assembly.constants import HIFIASM_ONT_METHOD
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
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.storage.publish import shell_publish_file

LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly",
    name="hifiasm_ont",
    method_name=HIFIASM_ONT_METHOD,
    frozen_status="hifiasm_ont_frozen",
    ran_status="hifiasm_ont_ran",
    owner=ParameterAttemptOwner(),
)


def build_hifiasm_ont_graph() -> CompiledMethodGraph:
    return build_method_graph(
        LIFECYCLE, freeze_hifiasm_ont_attempt, collect_hifiasm_ont_artifacts
    )


def freeze_hifiasm_ont_attempt(state: TopLevelState) -> StateUpdate:
    """Freeze a local hifiasm ONT attempt for the selected primary candidate."""
    output = assembly_stage_output(state)
    if output.get("status") != "hifiasm_ont_execution_planned":
        return {}
    if HIFIASM_ONT_METHOD not in load_descriptor_registry().methods:
        return fail_update(
            state, f"assembly descriptors missing: [{HIFIASM_ONT_METHOD!r}]"
        )

    try:
        read_paths = resolve_assembly_read_paths(
            output.get("input_ref"),
            cast(AssemblyReadInputType | None, output.get("input_type")),
        )
    except AssemblyInputError as exc:
        return fail_update(state, f"Hifiasm ONT local execution {exc}")
    non_fastq = [
        str(path)
        for path in read_paths
        if not str(path).endswith((".fastq", ".fq", ".fastq.gz", ".fq.gz"))
    ]
    if non_fastq:
        return fail_update(
            state,
            "Hifiasm ONT mode requires FASTQ format (not FASTA). "
            f"Non-FASTQ files: {non_fastq}",
        )
    try:
        execution_contract = load_assembly_execution_contract(
            output,
            method_id=HIFIASM_ONT_METHOD,
        )
    except ValueError as exc:
        return fail_update(state, f"Hifiasm ONT execution contract is invalid: {exc}")
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        {"env": execution_contract.environment.values},
        stage="assembly",
        method_name=HIFIASM_ONT_METHOD,
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
        method_id=HIFIASM_ONT_METHOD,
        label="Hifiasm ONT",
    )
    if recommendation_error or approved_bundle is None:
        return fail_update(
            state,
            recommendation_error or "Hifiasm ONT approved parameter attempt is missing",
        )
    try:
        read_type = approved_assembly_read_type(approved_bundle)
    except ValueError as exc:
        return fail_update(state, f"Hifiasm ONT approved read type is invalid: {exc}")
    thread_binding = execution_contract.l0_executor_values()
    approved_attempt = approved_bundle.attempt
    parameter_attempt_id = approved_attempt.attempt_id
    attempt_scope = f"{parameter_attempt_id:03d}"
    working_dir = stage_work_dir(
        state, "assembly", "attempts", attempt_scope, "hifiasm_ont"
    )
    output_dir = working_dir / "hifiasm_ont_out"
    artifact_dir = stage_artifacts_dir(
        state,
        "assembly", "attempts", attempt_scope, "outputs", "hifiasm_ont"
    )
    working_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    temp_draft = working_dir / "draft_assembly.fasta"
    prefix = output_dir / "hifiasm_ont"
    gfa = output_dir / "hifiasm_ont.bp.p_ctg.gfa"
    fallback_gfa = output_dir / "hifiasm_ont.p_ctg.gfa"
    script = working_dir / "run_hifiasm_ont.sh"
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
        return fail_update(state, f"Hifiasm ONT parameter sources are invalid: {exc}")
    command = " ".join(shlex.quote(arg) for arg in argv)
    script.write_text(
        "set -e\n"
        f"{command}\n"
        f"if [ -f {shlex.quote(str(gfa))} ]; then gfa={shlex.quote(str(gfa))};"
        f" elif [ -f {shlex.quote(str(fallback_gfa))} ];"
        f" then gfa={shlex.quote(str(fallback_gfa))};"
        " else echo 'expected hifiasm primary contig GFA missing' >&2; exit 1; fi\n"
        f'awk \'/^S/{{print ">"$2"\\n"$3}}\' "$gfa" > '
        f"{shlex.quote(str(temp_draft))}\n"
        f"{shell_publish_file(temp_draft, draft)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    try:
        input_dependencies = build_read_input_dependencies(
            state, output, "primary", read_paths
        )
    except ValueError as exc:
        return fail_update(
            state, f"Hifiasm ONT input dependency cardinality error: {exc}"
        )
    return freeze_parameterized_attempt(
        state,
        LIFECYCLE,
        label="Hifiasm ONT",
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
            "hifiasm_ont_output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "parameter_attempt_id": parameter_attempt_id,
        },
        stage_updates={
            "hifiasm_ont_output_dir": str(output_dir),
        },
    )


def collect_hifiasm_ont_artifacts(state: TopLevelState) -> StateUpdate:
    [execution] = load_method_executions(state, LIFECYCLE)
    attempt = execution.attempt
    metadata = dict(attempt.metadata or {})
    output_dir = Path(str(metadata.get("hifiasm_ont_output_dir") or ""))
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        execution=execution,
        substep_name="hifiasm_ont_assembly",
        native_outputs=hifiasm_native_output_declarations(
            output_dir,
            output_prefix="hifiasm_ont",
            role_namespace="hifiasm_ont",
            artifact_type="assembly.hifiasm_ont_native_gfa",
        ),
    )
