"""Hifiasm-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.domains.read_inputs import AssemblyInputError, resolve_assembly_read_paths
from app.execution.env_dispatch import resolve_method_env
from app.execution.parameter_attempts import load_current_frozen_attempt
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.assembly.command_builder import ParameterCommandError, build_command, build_parameter_sources
from app.domains.assembly.constants import HIFIASM_METHOD
from app.stages.assembly.method_application.lifecycle import (
    MethodLifecycle,
    blocked_parameters,
    build_method_graph,
    collect_method_artifacts,
    fail_update,
    freeze_parameterized_attempt,
    load_current_approved_recommendation,
    positive_int,
)
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.base import absolute_data_dir, stage_output, update_stage_output
from app.storage.paths import SessionPaths

HIFIASM_READ_TYPE_FLAGS = {"hifi": ""}

LIFECYCLE = MethodLifecycle(
    name="hifiasm",
    method_name=HIFIASM_METHOD,
    planned_status="hifiasm_execution_planned",
    frozen_status="hifiasm_frozen",
    ran_status="hifiasm_ran",
    draft_assembly_key="hifiasm_draft_assembly_path",
    substep_key="hifiasm_assembly",
)


def build_hifiasm_graph():
    return build_method_graph(LIFECYCLE, freeze_hifiasm_attempt, collect_hifiasm_artifacts)




def freeze_hifiasm_attempt(state: TopLevelState) -> dict:
    """Freeze a local hifiasm attempt for the selected primary candidate."""
    output = stage_output(state, "assembly")
    if output.get("status") != LIFECYCLE.planned_status:
        return {}
    if HIFIASM_METHOD not in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods:
        return fail_update(state, f"assembly descriptors missing: [{HIFIASM_METHOD!r}]")

    params, blocked = _parameters(output)
    if blocked:
        return update_stage_output(state, "assembly", blocked)
    try:
        read_paths = resolve_assembly_read_paths(output.get("input_ref"), output.get("input_type"))
    except AssemblyInputError as exc:
        return fail_update(state, f"Hifiasm local execution {exc}")
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        output,
        stage="assembly",
        method_name=HIFIASM_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if env_blocked is not None:
        return update_stage_output(state, "assembly", env_blocked)

    catalog, catalog_error = _load_catalog()
    if catalog_error:
        return fail_update(state, catalog_error)
    approved_attempt, recommended_params, recommendation_error = (
        load_current_approved_recommendation(
            state,
            output,
            method_id=HIFIASM_METHOD,
            label="Hifiasm",
        )
    )
    if recommendation_error or approved_attempt is None:
        return fail_update(
            state,
            recommendation_error or "Hifiasm approved parameter attempt is missing",
        )
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
        parameter_sources = build_parameter_sources(
            method_id=HIFIASM_METHOD,
            catalog=catalog,
            state=state,
            assembly_out=output,
            attempt_id=parameter_attempt_id,
            recommended_params=recommended_params,
            execution_context={
                "output_prefix": str(prefix),
                "input_reads": [str(path) for path in read_paths],
            },
        )
        argv = build_command(method_id=HIFIASM_METHOD, catalog=catalog, parameter_sources=parameter_sources)
    except ParameterCommandError as exc:
        return fail_update(state, f"Hifiasm parameter sources are invalid: {exc}")
    command = " ".join(shlex.quote(arg) for arg in argv)
    script.write_text(
        "set -e\n"
        f"{command}\n"
        f"if [ -f {shlex.quote(str(gfa))} ]; then gfa={shlex.quote(str(gfa))};"
        f" elif [ -f {shlex.quote(str(fallback_gfa))} ]; then gfa={shlex.quote(str(fallback_gfa))};"
        " else echo 'expected hifiasm primary contig GFA missing' >&2; exit 1; fi\n"
        f"awk '/^S/{{print \">\"$2\"\\n\"$3}}' \"$gfa\" > {shlex.quote(str(draft))}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return freeze_parameterized_attempt(
        state,
        output,
        LIFECYCLE,
        label="Hifiasm",
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
            "read_profile": params["read_type"],
            "threads": params["threads"],
            "parameter_sources": parameter_sources,
            "argv": argv,
            "hifiasm_output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "parameter_attempt_id": parameter_attempt_id,
        },
        stage_updates={
            "hifiasm_output_dir": str(output_dir),
            LIFECYCLE.draft_assembly_key: str(draft),
        },
    )


def collect_hifiasm_artifacts(state: TopLevelState) -> dict:
    output = stage_output(state, "assembly")
    attempt = load_current_frozen_attempt(
        state,
        output,
        stage="assembly",
        method_id=HIFIASM_METHOD,
        label="Hifiasm",
    )
    metadata = dict(attempt.metadata or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        attempt=attempt,
        extra_artifacts=[],
        bundle_logs={},
        bundle_metadata={
            "read_profile": metadata.get("read_profile"),
            "threads": metadata.get("threads"),
        },
    )


def _parameters(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Extract and validate hifiasm parameters (Level 0: from config/priors, not from L2 recommendations)."""
    # read_type and threads are Level 0 parameters, not Level 2
    # They come from read_type_prior and hifiasm_parameters, not from recommended_params
    missing: list[str] = []
    invalid: list[dict[str, Any]] = []

    # Get read_type from read_type_prior (set by input resolution) or fallback to hifiasm_parameters
    read_type = dict(output.get("read_type_prior") or {}).get("hifiasm")
    if not read_type:
        # Fallback for tests that don't set read_type_prior
        params = dict(output.get("hifiasm_parameters") or {})
        read_type = params.get("read_type")
    if not read_type:
        missing.append("read_type (from read_type_prior.hifiasm or hifiasm_parameters.read_type)")
    elif read_type not in HIFIASM_READ_TYPE_FLAGS:
        invalid.append({"param": "read_type", "value": read_type, "accepted": sorted(HIFIASM_READ_TYPE_FLAGS)})

    # Get threads from hifiasm_parameters (run config) or use catalog default (32)
    params = dict(output.get("hifiasm_parameters") or {})
    threads = params.get("threads")
    if threads is None:
        threads = 32  # Catalog default
    else:
        threads = positive_int(threads, "hifiasm_parameters.threads", missing, invalid)

    if missing or invalid:
        return {}, blocked_parameters("hifiasm level-0 parameters", HIFIASM_METHOD, missing, invalid)
    return {"read_type": str(read_type), "threads": threads}, None


def _load_catalog():
    try:
        return load_parameter_catalog(HIFIASM_METHOD), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Hifiasm parameter catalog is not usable: {exc}"
