"""NextDenovo-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.domains.read_inputs import AssemblyInputError, resolve_assembly_read_paths
from app.execution.env_dispatch import resolve_method_env
from app.execution.parameter_attempts import (
    current_parameter_attempt,
    load_current_frozen_attempt,
)
from app.configuration import load_descriptor_registry
from app.schemas.state import TopLevelState
from app.stages.assembly.command_builder import ParameterCommandError, build_parameter_sources, validate_parameter_sources
from app.domains.assembly.constants import NEXTDENOVO_METHOD
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
from app.storage.publish import publish_file, shell_publish_file

LIFECYCLE = MethodLifecycle(
    name="nextdenovo",
    method_name=NEXTDENOVO_METHOD,
    planned_status="nextdenovo_execution_planned",
    frozen_status="nextdenovo_frozen",
    ran_status="nextdenovo_ran",
    draft_assembly_key="nextdenovo_draft_assembly_path",
    substep_key="nextdenovo_assembly",
)
_READ_TYPES = {"ont", "clr", "hifi"}


def build_nextdenovo_graph():
    return build_method_graph(LIFECYCLE, freeze_nextdenovo_attempt, collect_nextdenovo_artifacts)


def freeze_nextdenovo_attempt(state: TopLevelState) -> dict:
    """Freeze a local NextDenovo attempt for the selected primary candidate."""
    output = stage_output(state, "assembly")
    if output.get("status") != LIFECYCLE.planned_status:
        return {}
    if NEXTDENOVO_METHOD not in load_descriptor_registry().methods:
        return fail_update(state, f"assembly descriptors missing: [{NEXTDENOVO_METHOD!r}]")
    params, blocked = _parameters(output)
    if blocked:
        return update_stage_output(state, "assembly", blocked)

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        output,
        stage="assembly",
        method_name=NEXTDENOVO_METHOD,
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
            method_id=NEXTDENOVO_METHOD,
            label="NextDenovo",
        )
    )
    if recommendation_error or approved_attempt is None:
        return fail_update(
            state,
            recommendation_error or "NextDenovo approved parameter attempt is missing",
        )
    parameter_attempt_id = approved_attempt.attempt_id
    fofn, read_paths, error = _fofn(
        state,
        output.get("input_ref"),
        output.get("input_type"),
        attempt_id=parameter_attempt_id,
    )
    if error:
        return fail_update(state, error)
    paths = SessionPaths.from_run_dir(data_dir)
    attempt_scope = f"{parameter_attempt_id:03d}"
    working_dir = paths.work_path(
        "assembly", "attempts", attempt_scope, "nextdenovo"
    )
    nextdenovo_work_dir = working_dir / "nextdenovo_work"
    artifact_dir = paths.artifact_path(
        "assembly", "attempts", attempt_scope, "outputs", "nextdenovo"
    )
    working_dir.mkdir(parents=True, exist_ok=True)
    nextdenovo_work_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    run_cfg = working_dir / "run.cfg"
    script = working_dir / "run_nextdenovo.sh"
    try:
        parameter_sources = build_parameter_sources(
            method_id=NEXTDENOVO_METHOD,
            catalog=catalog,
            state=state,
            assembly_out=output,
            attempt_id=parameter_attempt_id,
            recommended_params=recommended_params,
            execution_context={
                "input_fofn": str(fofn),
                "work_dir": str(nextdenovo_work_dir),
            },
        )
        params = _merge_catalog_sources(parameter_sources, catalog)
    except ParameterCommandError as exc:
        return fail_update(state, f"NextDenovo parameter sources are invalid: {exc}")
    run_cfg.write_text(_run_cfg(nextdenovo_work_dir, fofn, **params), encoding="utf-8")
    candidates = [
        nextdenovo_work_dir / "03.ctg_graph" / "nd.asm.fasta",
        nextdenovo_work_dir / "03.ctg_graph" / "nextdenovo.asm.fasta",
        nextdenovo_work_dir / "nextdenovo.asm.fasta",
    ]
    output_checks = "\n".join(
        f"if [ -f {shlex.quote(str(path))} ]; then {shell_publish_file(path, draft)}; exit 0; fi"
        for path in candidates
    )
    script.write_text(
        "set -e\n"
        f"nextDenovo {shlex.quote(str(run_cfg))}\n"
        f"{output_checks}\n"
        "echo 'expected NextDenovo draft assembly missing' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    argv = ["nextDenovo", str(run_cfg)]
    return freeze_parameterized_attempt(
        state,
        output,
        LIFECYCLE,
        label="NextDenovo",
        approved_attempt=approved_attempt,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=[draft],
        env_spec=env_spec,
        command_payload={
            "argv": argv,
            "command": " ".join(shlex.quote(arg) for arg in argv),
            "script_path": str(script),
            "working_dir": str(working_dir),
            "run_cfg": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
            "draft_assembly_path": str(draft),
            "parameter_sources": parameter_sources,
            "rendered_parameters": params,
        },
        frozen_parameters_payload={
            "parameter_sources": parameter_sources,
            "rendered_parameters": params,
        },
        frozen_attempt_metadata={
            "input_ref": output.get("input_ref"),
            "input_type": output.get("input_type"),
            "input_fofn": str(fofn),
            "read_paths": [str(path) for path in read_paths],
            "read_profile": params["read_type"],
            **params,
            "parameter_sources": parameter_sources,
            "rendered_parameters": params,
            "argv": argv,
            "run_cfg": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
            "draft_assembly_path": str(draft),
            "parameter_attempt_id": parameter_attempt_id,
        },
        stage_updates={
            "nextdenovo_run_cfg_path": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
            LIFECYCLE.draft_assembly_key: str(draft),
        },
    )


def collect_nextdenovo_artifacts(state: TopLevelState) -> dict:
    output = stage_output(state, "assembly")
    attempt = load_current_frozen_attempt(
        state,
        output,
        stage="assembly",
        method_id=NEXTDENOVO_METHOD,
        label="NextDenovo",
    )
    metadata = dict(attempt.metadata or {})
    run_cfg_value = output.get("nextdenovo_run_cfg_path")
    if not run_cfg_value:
        return fail_update(state, "NextDenovo run.cfg path is missing")
    attempt_id = current_parameter_attempt(
        output,
        method_id=NEXTDENOVO_METHOD,
        label="NextDenovo",
    ).attempt_id
    durable_run_cfg = SessionPaths.from_run_dir(absolute_data_dir(state)).artifact_path(
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
        attempt=attempt,
        extra_artifacts=[(durable_run_cfg, "assembly.nextdenovo_run_cfg")],
        bundle_logs={
            "primary_log": str(durable_run_cfg),
            "run_cfg": str(durable_run_cfg),
        },
        bundle_metadata={key: metadata.get(key) for key in ("read_profile", "genome_size", "threads", "read_cutoff")},
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

    session_paths = SessionPaths.from_run_dir(absolute_data_dir(state))
    fofn = session_paths.work_path(
        "assembly", "attempts", f"{attempt_id:03d}", "nextdenovo", "input.fofn"
    )
    fofn.parent.mkdir(parents=True, exist_ok=True)
    fofn.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
    return fofn, paths, None


def _parameters(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    params = dict(output.get("nextdenovo_parameters") or {})
    missing: list[str] = []
    invalid: list[dict[str, Any]] = []
    read_type = params.get("read_type") or dict(output.get("read_type_prior") or {}).get("nextdenovo")
    if not read_type:
        missing.append("nextdenovo_parameters.read_type")
    elif read_type not in _READ_TYPES:
        invalid.append({"param": "nextdenovo_parameters.read_type", "value": read_type, "accepted": sorted(_READ_TYPES)})
    resolved_genome_size = dict(output.get("resolved_genome_size") or {})
    genome_size = positive_int(resolved_genome_size.get("value_bp"), "nextdenovo_parameters.genome_size", missing, invalid)
    threads_value = params.get("threads")
    if threads_value is None:
        threads_value = 32
    threads = positive_int(threads_value, "nextdenovo_parameters.threads", missing, invalid)
    if missing or invalid:
        return {}, blocked_parameters("nextdenovo_parameters", NEXTDENOVO_METHOD, missing, invalid)
    return {"read_type": str(read_type), "genome_size": genome_size, "threads": threads}, None


def _load_catalog():
    try:
        return load_parameter_catalog(NEXTDENOVO_METHOD), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"NextDenovo parameter catalog is not usable: {exc}"


def _merge_catalog_sources(parameter_sources: dict[str, dict[str, Any]], catalog) -> dict[str, Any]:
    validate_parameter_sources(catalog, parameter_sources)
    merged: dict[str, Any] = {}
    for group_name in ("l0_fixed", "l1_readonly", "l2_recommended"):
        merged.update(parameter_sources.get(group_name) or {})
    return {
        "read_type": _required_string(merged, "read_type"),
        "genome_size": _required_int(merged, "genome_size"),
        "threads": _required_int(merged, "threads"),
        "read_cutoff": _required_int(merged, "read_cutoff"),
        "nextgraph_a": _required_int(merged, "nextgraph_a"),
    }


def _required_string(merged: dict[str, Any], key: str) -> str:
    try:
        value = merged[key]
    except KeyError as exc:
        raise ParameterCommandError(f"Missing rendered parameter: {key}") from exc
    if not isinstance(value, str) or not value:
        raise ParameterCommandError(f"{key} must be a non-empty string, got {value!r}")
    return value


def _required_int(merged: dict[str, Any], key: str) -> int:
    try:
        value = merged[key]
    except KeyError as exc:
        raise ParameterCommandError(f"Missing rendered parameter: {key}") from exc
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ParameterCommandError(f"{key} must be an integer, got {value!r}") from exc


def _run_cfg(
    work_dir: Path,
    input_fofn: Path,
    *,
    read_type: str,
    genome_size: int,
    threads: int,
    read_cutoff: int,
    nextgraph_a: int,
) -> str:
    return (
        "[General]\njob_type = local\njob_prefix = nextdenovo\ntask = all\nrewrite = yes\ndeltmp = yes\n"
        f"parallel_jobs = {threads}\ninput_type = raw\nread_type = {read_type}\ninput_fofn = {input_fofn}\nworkdir = {work_dir}\n\n"
        "[correct_option]\n"
        f"read_cutoff = {read_cutoff}\ngenome_size = {genome_size}\nsort_options = -m 1g -t {threads}\n"
        f"minimap2_options_raw = -t {threads}\npa_correction = 3\ncorrection_options = -p {threads}\n\n"
        f"[assemble_option]\nminimap2_options_cns = -t {threads}\nnextgraph_options = -a {nextgraph_a}\n"
    )
