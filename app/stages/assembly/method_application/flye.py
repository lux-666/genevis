"""Flye-specific adapter for the shared assembly method lifecycle."""

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
from app.domains.assembly.constants import FLYE_METHOD
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
from app.storage.publish import shell_publish_file

FLYE_READ_TYPE_FLAGS = {
    "nano-raw": "--nano-raw",
    "nano-hq": "--nano-hq",
    "pacbio-raw": "--pacbio-raw",
    "pacbio-hifi": "--pacbio-hifi",
}

LIFECYCLE = MethodLifecycle(
    name="flye",
    method_name=FLYE_METHOD,
    planned_status="flye_execution_planned",
    frozen_status="flye_frozen",
    ran_status="flye_ran",
    draft_assembly_key="flye_draft_assembly_path",
    substep_key="flye_assembly",
)


def build_flye_graph():
    return build_method_graph(LIFECYCLE, freeze_flye_attempt, collect_flye_artifacts)


def freeze_flye_attempt(state: TopLevelState) -> dict:
    """Freeze a local Flye attempt for the selected primary candidate."""
    output = stage_output(state, "assembly")
    if output.get("status") != LIFECYCLE.planned_status:
        return {}
    if FLYE_METHOD not in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods:
        return fail_update(state, f"assembly descriptors missing: [{FLYE_METHOD!r}]")

    params, blocked = _parameters(output)
    if blocked:
        return update_stage_output(state, "assembly", blocked)
    try:
        read_paths = resolve_assembly_read_paths(output.get("input_ref"), output.get("input_type"))
    except AssemblyInputError as exc:
        return fail_update(state, f"Flye local execution {exc}")
    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        output,
        stage="assembly",
        method_name=FLYE_METHOD,
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
            method_id=FLYE_METHOD,
            label="Flye",
        )
    )
    if recommendation_error or approved_attempt is None:
        return fail_update(
            state,
            recommendation_error or "Flye approved parameter attempt is missing",
        )
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
        parameter_sources = build_parameter_sources(
            method_id=FLYE_METHOD,
            catalog=catalog,
            state=state,
            assembly_out=output,
            attempt_id=parameter_attempt_id,
            recommended_params=recommended_params,
            execution_context={
                "output_dir": str(output_dir),
                "input_reads": [str(path) for path in read_paths],
            },
        )
        argv = _build_flye_argv(catalog, parameter_sources, fallback_read_type=params["read_type"])
    except (KeyError, ParameterCommandError) as exc:
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
        output,
        LIFECYCLE,
        label="Flye",
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
            "estimated_genome_size": params["estimated_genome_size"],
            "threads": params["threads"],
            "parameter_sources": parameter_sources,
            "argv": argv,
            "flye_output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "parameter_attempt_id": parameter_attempt_id,
        },
        stage_updates={
            "flye_output_dir": str(output_dir),
            LIFECYCLE.draft_assembly_key: str(draft),
        },
    )


def collect_flye_artifacts(state: TopLevelState) -> dict:
    output = stage_output(state, "assembly")
    attempt = load_current_frozen_attempt(
        state,
        output,
        stage="assembly",
        method_id=FLYE_METHOD,
        label="Flye",
    )
    metadata = dict(attempt.metadata or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        attempt=attempt,
        extra_artifacts=[(Path(output.get("flye_output_dir", "")) / "flye.log", "assembly.flye_log")],
        bundle_logs={},
        bundle_metadata={
            "read_profile": metadata.get("read_profile"),
            "estimated_genome_size": metadata.get("estimated_genome_size"),
            "threads": metadata.get("threads"),
        },
    )


def _parameters(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    params = dict(output.get("flye_parameters") or {})
    missing: list[str] = []
    invalid: list[dict[str, Any]] = []
    read_type = params.get("read_type") or dict(output.get("read_type_prior") or {}).get("flye")
    if not read_type:
        missing.append("flye_parameters.read_type")
    elif read_type not in FLYE_READ_TYPE_FLAGS:
        invalid.append({"param": "flye_parameters.read_type", "value": read_type, "accepted": sorted(FLYE_READ_TYPE_FLAGS)})
    resolved_genome_size = dict(output.get("resolved_genome_size") or {})
    genome_size = positive_int(
        resolved_genome_size.get("value_bp"),
        "flye_parameters.estimated_genome_size",
        missing,
        invalid,
    )
    threads_value = params.get("threads")
    if threads_value is None:
        threads_value = 32
    threads = positive_int(threads_value, "flye_parameters.threads", missing, invalid)
    if missing or invalid:
        return {}, blocked_parameters("flye_parameters", FLYE_METHOD, missing, invalid)
    return {"read_type": read_type, "estimated_genome_size": genome_size, "threads": threads}, None


def _load_catalog():
    try:
        return load_parameter_catalog(FLYE_METHOD), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Flye parameter catalog is not usable: {exc}"


def _build_flye_argv(catalog, parameter_sources: dict[str, dict[str, Any]], *, fallback_read_type: str) -> list[str]:
    """Build Flye argv with reads immediately following the read-type flag."""

    l0 = dict(parameter_sources.get("l0_fixed") or {})
    read_type = str(l0.pop("read_type", fallback_read_type))
    input_reads = l0.pop("input_reads", None)
    if not isinstance(input_reads, list) or not input_reads:
        raise ParameterCommandError("Flye input_reads must be a non-empty list")

    argv_sources = {
        "l0_fixed": l0,
        "l1_readonly": {},
        "l2_recommended": dict(parameter_sources.get("l2_recommended") or {}),
    }
    emitted_flags = build_command(method_id=FLYE_METHOD, catalog=catalog, parameter_sources=argv_sources)
    return [catalog.tool, FLYE_READ_TYPE_FLAGS[read_type], *[str(path) for path in input_reads], *emitted_flags[1:]]
