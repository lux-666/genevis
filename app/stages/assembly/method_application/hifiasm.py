"""Hifiasm-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import cast

from app.configuration import load_descriptor_registry
from app.control.workflow_plan import current_workflow_step_id
from app.domains.assembly.constants import HIFIASM_METHOD
from app.domains.read_inputs import (
    AssemblyInputError,
    AssemblyReadInputType,
    resolve_assembly_read_paths,
)
from app.execution.attempt_owners import ParameterAttemptOwner
from app.execution.env_dispatch import resolve_method_env
from app.execution.input_integrity import (
    artifact_input_dependency,
    verify_input_dependencies,
)
from app.execution.lifecycle import (
    CompiledMethodGraph,
    MethodExecutionLifecycle,
    build_method_graph,
    load_method_executions,
)
from app.schemas.artifacts import ArtifactRefJson
from app.schemas.execution import ArtifactInputDependency
from app.schemas.state import StageCursor, StateUpdate, TopLevelState
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
    assembly_primary_method_outcome_policy,
    build_read_input_dependencies,
    collect_method_artifacts,
    fail_update,
    freeze_parameterized_attempt,
    load_assembly_execution_contract,
    load_current_approved_recommendation,
)
from app.stages.assembly.state import (
    AssemblyStageCursor,
    assembly_stage_output,
    update_assembly_stage_output,
    validated_assembly_stage_payload,
)
from app.stages.base import absolute_data_dir, stage_artifacts_dir, stage_work_dir
from app.stages.sequences import read_sequences
from app.storage.artifacts import publish_or_recover_work_artifact
from app.storage.assembly_candidates import load_executed_candidate_bundle
from app.storage.publish import shell_publish_file

LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly",
    name="hifiasm",
    method_name=HIFIASM_METHOD,
    frozen_status="hifiasm_frozen",
    ran_status="hifiasm_ran",
    owner=ParameterAttemptOwner(),
    outcome_policy=assembly_primary_method_outcome_policy,
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
    attempt_scope = f"{parameter_attempt_id:03d}"
    working_dir = stage_work_dir(
        state, "assembly", "attempts", attempt_scope, "hifiasm"
    )
    output_dir = working_dir / "hifiasm_out"
    artifact_dir = stage_artifacts_dir(
        state, "assembly", "attempts", attempt_scope, "outputs", "hifiasm"
    )
    working_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    temp_draft = working_dir / "draft_assembly.fasta"
    prefix = output_dir / "hifiasm"
    gfa_bp = output_dir / "hifiasm.bp.p_ctg.gfa"
    gfa_hic = output_dir / "hifiasm.hic.p_ctg.gfa"
    phased_kind = "trio" if output.get("trio_enabled") else "hic"
    phased = bool(output.get("trio_enabled") or output.get("use_hic_phasing"))
    hap_prefix = "hifiasm" if phased_kind == "trio" else "hifiasm.hic"
    gfa_hap1 = output_dir / f"{hap_prefix}.hap1.p_ctg.gfa"
    gfa_hap2 = output_dir / f"{hap_prefix}.hap2.p_ctg.gfa"
    fasta_hap1 = artifact_dir / "hifiasm.hap1.p_ctg.fasta"
    fasta_hap2 = artifact_dir / "hifiasm.hap2.p_ctg.fasta"
    temp_fasta_hap1 = working_dir / "hifiasm.hap1.p_ctg.fasta"
    temp_fasta_hap2 = working_dir / "hifiasm.hap2.p_ctg.fasta"
    pat_yak = output_dir / "pat.yak"
    mat_yak = output_dir / "mat.yak"
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
        trio_enabled = bool(output.get("trio_enabled"))
        use_hic_phasing = bool(output.get("use_hic_phasing"))
        phasing_input_kind = (
            "trio" if trio_enabled else "hic" if use_hic_phasing else "none"
        )
        trio_maternal_reads = list(output.get("trio_maternal_read_refs") or [])
        trio_paternal_reads = list(output.get("trio_paternal_read_refs") or [])
        hic_r1 = list(output.get("hic_r1_reads") or [])
        hic_r2 = list(output.get("hic_r2_reads") or [])
        if trio_enabled:
            if not trio_maternal_reads or not trio_paternal_reads:
                return fail_update(
                    state,
                    "Hifiasm Trio requires non-empty maternal and paternal reads",
                )
            l0_values["assembly_trio_paternal_yak"] = str(pat_yak)
            l0_values["assembly_trio_maternal_yak"] = str(mat_yak)
        if use_hic_phasing:
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
        parameter_sources = parameter_sources_payload(resolved)
        yak_threads = int(resolved.effective_parameters["threads"])
        argv = render_assembly_cli(catalog, resolved)
    except ParameterCommandError as exc:
        return fail_update(state, f"Hifiasm parameter sources are invalid: {exc}")
    command = " ".join(shlex.quote(arg) for arg in argv)
    script_lines = ["set -e"]
    normalized_parent_deps: list[ArtifactInputDependency] = []
    if trio_enabled:
        try:
            for role, reads, yak in (
                ("trio_paternal", trio_paternal_reads, pat_yak),
                ("trio_maternal", trio_maternal_reads, mat_yak),
            ):
                parent = _prepare_parent_reads(state, output, working_dir, role, reads)
                normalized_parent_deps.append(parent)
                script_lines.append(
                    f"yak count -k31 -b37 -t{yak_threads} "
                    f"-o {shlex.quote(str(yak))} {shlex.quote(parent.path)}"
                )
                script_lines.append(f"test -s {shlex.quote(str(yak))}")
        except (OSError, ValueError) as exc:
            return fail_update(state, f"Trio parental read preparation failed: {exc}")
    if phased:
        script_lines.extend(
            [
                command,
                f"test -s {shlex.quote(str(gfa_hap1))}",
                f"test -s {shlex.quote(str(gfa_hap2))}",
                (
                    f'awk \'/^S/{{print ">"$2"\\n"$3}}\' '
                    f"{shlex.quote(str(gfa_hap1))} > "
                    f"{shlex.quote(str(temp_fasta_hap1))}"
                ),
                (
                    f'awk \'/^S/{{print ">"$2"\\n"$3}}\' '
                    f"{shlex.quote(str(gfa_hap2))} > "
                    f"{shlex.quote(str(temp_fasta_hap2))}"
                ),
                f"test -s {shlex.quote(str(temp_fasta_hap1))}",
                f"test -s {shlex.quote(str(temp_fasta_hap2))}",
                (
                    f"cp {shlex.quote(str(temp_fasta_hap1))} "
                    f"{shlex.quote(str(temp_draft))}"
                ),
                shell_publish_file(temp_fasta_hap1, fasta_hap1),
                shell_publish_file(temp_fasta_hap2, fasta_hap2),
                # The existing Assembly backbone contract is single-valued. Use
                # hap1 as that explicit non-concatenated view; hap2 is separately
                # published and exposed through phased_output_refs.
                shell_publish_file(temp_draft, draft),
            ]
        )
    else:
        script_lines.extend(
            [
                command,
                (
                    f"if [ -f {shlex.quote(str(gfa_bp))} ]; "
                    f"then gfa={shlex.quote(str(gfa_bp))};"
                ),
                (
                    f" elif [ -f {shlex.quote(str(gfa_hic))} ]; "
                    f"then gfa={shlex.quote(str(gfa_hic))};"
                ),
                (
                    " else echo 'expected hifiasm primary contig GFA missing' "
                    ">&2; exit 1; fi"
                ),
                (
                    'awk \'/^S/{print ">"$2"\\n"$3}\' "$gfa" > '
                    f"{shlex.quote(str(temp_draft))}"
                ),
                f"{shell_publish_file(temp_draft, draft)}",
            ]
        )
    script.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
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
        trio_maternal_deps = (
            build_read_input_dependencies(
                state,
                output,
                "trio_maternal",
                trio_maternal_reads,
            )
            if trio_enabled
            else []
        )
        trio_paternal_deps = (
            build_read_input_dependencies(
                state,
                output,
                "trio_paternal",
                trio_paternal_reads,
            )
            if trio_enabled
            else []
        )
        hic_r1_deps = (
            build_read_input_dependencies(state, output, "hic_r1", hic_r1)
            if use_hic_phasing
            else []
        )
        hic_r2_deps = (
            build_read_input_dependencies(state, output, "hic_r2", hic_r2)
            if use_hic_phasing
            else []
        )
        input_dependencies = (
            primary_deps
            + ul_deps
            + trio_maternal_deps
            + trio_paternal_deps
            + hic_r1_deps
            + hic_r2_deps
            + normalized_parent_deps
        )
    except ValueError as exc:
        return fail_update(state, f"Hifiasm input dependency cardinality error: {exc}")
    return freeze_parameterized_attempt(
        state,
        LIFECYCLE,
        label="Hifiasm",
        execution_contract=execution_contract,
        approved_attempt=approved_attempt,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=(
            [draft, fasta_hap1, fasta_hap2, gfa_hap1, gfa_hap2]
            + ([pat_yak, mat_yak] if trio_enabled else [])
            if phased
            else [draft]
        ),
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
            "phasing_input_kind": phasing_input_kind,
            "trio_paternal_yak_path": str(pat_yak) if trio_enabled else None,
            "trio_maternal_yak_path": str(mat_yak) if trio_enabled else None,
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
            "trio_enabled": trio_enabled,
            "phasing_input_kind": phasing_input_kind,
            "phased_hap1_fasta_path": str(fasta_hap1) if phased else None,
            "phased_hap2_fasta_path": str(fasta_hap2) if phased else None,
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
        trio_enabled=bool(metadata.get("trio_enabled")),
        phased_fasta_dir=(
            Path(str(metadata["phased_hap1_fasta_path"])).parent
            if metadata.get("phased_hap1_fasta_path")
            else None
        ),
    )
    try:
        update = collect_method_artifacts(
            state,
            LIFECYCLE,
            execution=execution,
            substep_name="hifiasm_assembly",
            native_outputs=native_outputs,
        )
    except (OSError, TypeError, ValueError) as exc:
        return fail_update(state, f"Hifiasm artifact collection failed: {exc}")
    updated_output = dict((update.get("stage_outputs") or {}).get("assembly") or {})
    executed_ref = updated_output.get("executed_candidate_bundle_ref")
    if not isinstance(executed_ref, dict):
        return update
    try:
        bundle = load_executed_candidate_bundle(
            absolute_data_dir(state),
            state["session_id"],
            executed_ref,
            expected_method_name=HIFIASM_METHOD,
        )
    except (OSError, TypeError, ValueError) as exc:
        merged_output = {
            **updated_output,
            "status": "failed",
            "reason": f"Hifiasm executed candidate bundle is invalid: {exc}",
        }
        return {
            **update,
            "stage_outputs": {
                **dict(update.get("stage_outputs") or {}),
                "assembly": merged_output,
            },
        }
    phased_refs: dict[str, list[ArtifactRefJson]] = {}
    for native in bundle.bundle.native_outputs:
        if native.artifact_class != "haplotype_contigs":
            continue
        phased_refs.setdefault(native.native_role, []).append(
            cast(ArtifactRefJson, native.artifact_ref.model_dump(mode="json"))
        )
    return {
        **update,
        "stage_outputs": {
            **dict(update.get("stage_outputs") or {}),
            "assembly": cast(
                StageCursor,
                {**updated_output, "phased_output_refs": phased_refs},
            ),
        },
    }


def _prepare_parent_reads(
    state: TopLevelState,
    output: AssemblyStageCursor,
    working_dir: Path,
    role: str,
    paths: list[str],
) -> ArtifactInputDependency:
    """Stream every parent member into one verified yak input, without file limits."""
    dependencies = build_read_input_dependencies(state, output, role, paths)
    verify_input_dependencies(state, dependencies)
    normalized = working_dir / f"{role}.fasta"
    with normalized.open("w", encoding="ascii") as handle:
        for member, path in enumerate(paths):
            for ordinal, (_, sequence) in enumerate(read_sequences(Path(path))):
                handle.write(f">{role}_{member}_{ordinal}\n{sequence}\n")
    verify_input_dependencies(state, dependencies)
    published = publish_or_recover_work_artifact(
        state["data_dir"],
        state["session_id"],
        normalized,
        stage="assembly",
        method_name=HIFIASM_METHOD,
        artifact_type="assembly.trio_parent_reads",
        metadata={"role": role, "source_paths": paths},
    )
    return artifact_input_dependency(state, published.ref, published.path)
