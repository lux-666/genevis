"""Flye-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from app.configuration import load_descriptor_registry
from app.domains.assembly.constants import FLYE_METHOD
from app.domains.read_inputs import AssemblyInputError, resolve_assembly_read_paths
from app.execution.env_dispatch import resolve_method_env
from app.execution.attempt_owners import ParameterAttemptOwner
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
)
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ArtifactMeta,
    AssemblyCandidateBundle,
)
from app.schemas.state import TopLevelState
from app.stages.assembly.command_builder import (
    ParameterCommandError,
    build_command,
    build_parameter_sources,
)
from app.stages.assembly.method_application.support import (
    blocked_parameters,
    collect_method_artifacts,
    fail_update,
    freeze_parameterized_attempt,
    load_current_approved_recommendation,
    positive_int,
)
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.base import (
    absolute_data_dir,
    ordered_unique,
    stage_output,
    update_stage_output,
)
from app.stages.resource_resolution import resolve_stage_resource, update_resource_ref
from app.stages.resource_specs import ASSEMBLY_CANDIDATES_SPEC
from app.storage.artifacts import publish_artifact_record, register_artifact_records
from app.storage.ledger import Ledger
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
RERUN_LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly",
    name="flye rerun",
    method_name=FLYE_METHOD,
    frozen_status="flye_rerun_frozen",
    ran_status="flye_rerun_ran",
    cleanup_outputs=False,
    process_failure="collect",
)


def build_flye_graph():
    return build_method_graph(LIFECYCLE, freeze_flye_attempt, collect_flye_artifacts)


def build_flye_rerun_graph():
    """Build the direct lifecycle for an operator-requested Flye rerun."""

    return build_method_graph(
        RERUN_LIFECYCLE,
        freeze_flye_rerun_attempt,
        collect_flye_rerun_artifacts,
    )


def freeze_flye_rerun_attempt(state: TopLevelState) -> dict:
    """Freeze the checkpointed Assembly Review rerun request."""

    output = stage_output(state, "assembly")
    if output.get("status") != "flye_rerun_planned":
        return {}
    request = output.get("rerun_request")
    if not isinstance(request, dict):
        return fail_update(state, "Flye rerun request is missing")
    params = request.get("parameters")
    if not isinstance(params, dict):
        return fail_update(state, "Flye rerun parameters are missing")
    try:
        read_paths = resolve_assembly_read_paths(
            output.get("input_ref"),
            output.get("input_type"),
        )
    except AssemblyInputError as exc:
        return fail_update(state, f"Flye rerun {exc}")

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, blocked = resolve_method_env(
        output,
        stage="assembly",
        method_name=FLYE_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_stage_output(state, "assembly", blocked)

    rerun_index = int(request["rerun_index"])
    paths = SessionPaths.from_run_dir(data_dir)
    working_dir = paths.work_path("assembly", f"flye_rerun_{rerun_index}")
    output_dir = working_dir / "flye_out"
    artifact_dir = paths.artifact_path("assembly", f"flye_rerun_{rerun_index}")
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    script = working_dir / "run_flye.sh"
    read_flag = FLYE_READ_TYPE_FLAGS[str(params["read_type"])]
    reads_arg = " ".join(shlex.quote(str(path)) for path in read_paths)
    script.write_text(
        "set -e\n"
        f"flye {read_flag} {reads_arg}"
        f" --out-dir {shlex.quote(str(output_dir))}"
        f" --genome-size {shlex.quote(str(params['estimated_genome_size']))}"
        f" --threads {int(params['threads'])}\n"
        f"test -f {shlex.quote(str(output_dir / 'assembly.fasta'))}\n"
        f"{shell_publish_file(output_dir / 'assembly.fasta', draft)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:assembly:flye:rerun_{rerun_index}",
        session_id=session_id,
        stage="assembly",
        method_name=FLYE_METHOD,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=[draft],
        env=output.get("env"),
        env_spec=env_spec,
        metadata={
            "input_ref": output.get("input_ref"),
            "input_type": output.get("input_type"),
            "read_paths": [str(path) for path in read_paths],
            "read_profile": params["read_type"],
            "estimated_genome_size": params["estimated_genome_size"],
            "threads": params["threads"],
            "flye_output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "rerun_index": rerun_index,
            "candidate_id": request["candidate_id"],
            "source_candidate_id": request.get("source_candidate_id"),
            "rerun_reason": request.get("rerun_reason"),
        },
    )
    return freeze_method_attempts(state, RERUN_LIFECYCLE, [attempt])


def collect_flye_rerun_artifacts(state: TopLevelState) -> dict:
    """Materialize the rerun candidate bundle and return Assembly to draft_ready."""

    output = stage_output(state, "assembly")
    if output.get("status") != RERUN_LIFECYCLE.ran_status:
        return {}
    executions = load_method_executions(state, RERUN_LIFECYCLE)
    if len(executions) != 1:
        return fail_update(state, "Flye rerun requires exactly one execution attempt")
    attempt = executions[0].attempt
    execution = executions[0].evidence
    metadata = dict(attempt.metadata)
    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    ledger = Ledger(data_dir)
    candidate_id = str(metadata["candidate_id"])
    rerun_index = int(metadata["rerun_index"])
    if not execution.success:
        ledger.record_event(
            session_id,
            "rerun_failed",
            {
                "candidate_id": candidate_id,
                "error": execution.error,
                "rerun_index": rerun_index,
            },
        )
        return fail_update(state, f"rerun Flye execution failed: {execution.error}")

    draft = Path(str(metadata["draft_assembly_path"]))
    if not draft.exists():
        raise FileNotFoundError(f"rerun Flye draft assembly missing: {draft}")

    draft_artifact = ArtifactMeta.from_file(
        draft,
        stage="assembly",
        method_name=FLYE_METHOD,
        artifact_type="assembly.draft_assembly",
        metadata={
            "attempt_id": attempt.attempt_id,
            "candidate_id": candidate_id,
            "rerun_index": rerun_index,
        },
    )
    register_artifact_records(data_dir, session_id, [draft_artifact])
    draft_payload = draft_artifact.model_dump(mode="json")
    ledger.record_attempt_succeeded(session_id, attempt, artifacts=[draft_payload])
    ledger.record_artifact_registered(
        session_id,
        draft_payload,
        attempt_id=attempt.attempt_id,
    )

    cursor = output["execution_attempts"][0]
    bundle = AssemblyCandidateBundle(
        session_id=session_id,
        candidate_id=candidate_id,
        method_name=FLYE_METHOD,
        status="executed",
        input_ref=str(output.get("input_ref")),
        input_type=str(output.get("input_type")),
        genome_size_prior=output.get("genome_size_prior"),
        expected_outputs={"draft_assembly_fasta": str(draft)},
        logs={
            "stdout": execution.stdout_ref.path_ref,
            "stderr": execution.stderr_ref.path_ref,
        },
        metadata={
            "execution_enabled": True,
            "attempt_id": attempt.attempt_id,
            "draft_assembly_artifact_ref": str(draft),
            "draft_assembly_ref_status": "materialized",
            "read_profile": metadata.get("read_profile"),
            "estimated_genome_size": metadata.get("estimated_genome_size"),
            "threads": metadata.get("threads"),
            "rerun_index": rerun_index,
            "source_candidate_id": metadata.get("source_candidate_id"),
            "rerun_reason": metadata.get("rerun_reason"),
            "execution_ref": cursor["execution_ref"],
            "frozen_attempt_ref": cursor["frozen_attempt_ref"],
        },
    )
    paths = SessionPaths.from_run_dir(data_dir)
    bundle_source = Path(attempt.working_dir) / f"flye_rerun_{rerun_index}_candidate.json"
    bundle_source.write_text(
        json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    bundle_artifact = ArtifactMeta.from_file(
        bundle_source,
        stage="assembly",
        method_name=FLYE_METHOD,
        artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
        metadata={
            "candidate_id": candidate_id,
            "execution_enabled": True,
            "rerun_index": rerun_index,
        },
    )
    bundle_path = publish_artifact_record(
        data_dir,
        session_id,
        bundle_artifact,
        destination=paths.artifact_path(
            "assembly",
            "candidates",
            f"flye_rerun_{rerun_index}_candidate.json",
        ),
    ).path
    ledger.record_artifact_registered(
        session_id,
        bundle_artifact.model_dump(mode="json"),
        attempt_id=attempt.attempt_id,
    )
    bundle_ref = str(bundle_path)
    selected_refs = ordered_unique(
        [*list(output.get("selected_candidate_refs") or []), bundle_ref]
    )
    ledger.record_event(
        session_id,
        "rerun_completed",
        {
            "candidate_id": candidate_id,
            "rerun_index": rerun_index,
            "parameters": {
                "read_type": metadata.get("read_profile"),
                "estimated_genome_size": metadata.get("estimated_genome_size"),
                "threads": metadata.get("threads"),
            },
            "draft_assembly_ref": str(draft),
            "bundle_ref": bundle_ref,
            "frozen_attempt_ref": cursor["frozen_attempt_ref"],
        },
    )
    update = update_stage_output(
        state,
        "assembly",
        {
            "status": "draft_ready",
            "rerun_request": None,
            "selected_candidate_refs": selected_refs,
            "candidate_assembly_refs": ordered_unique(
                [*list(output.get("candidate_assembly_refs") or []), str(draft)]
            ),
            "executed_candidate_refs": ordered_unique(
                [*list(output.get("executed_candidate_refs") or []), bundle_ref]
            ),
        },
    )
    return {
        **update,
        "resource_refs": rerun_resource_refs(state, selected_refs),
    }


def rerun_resource_refs(state: TopLevelState, candidate_refs: list[str]) -> dict:
    """Publish materialized rerun candidates through the central resource table."""

    current = resolve_stage_resource(state, resource_spec=ASSEMBLY_CANDIDATES_SPEC)
    paths: list[str] = []
    for candidate_ref in candidate_refs:
        path = Path(candidate_ref)
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        draft = (payload.get("expected_outputs") or {}).get("draft_assembly_fasta")
        if draft:
            paths.append(str(draft))
    metadata = dict(current.metadata)
    metadata["candidate_bundle_refs"] = list(candidate_refs)
    return update_resource_ref(
        state,
        "assembly",
        paths or current.paths,
        "assembly",
        metadata,
    )


def freeze_flye_attempt(state: TopLevelState) -> dict:
    """Freeze a local Flye attempt for the selected primary candidate."""
    output = stage_output(state, "assembly")
    if output.get("status") != "flye_execution_planned":
        return {}
    if FLYE_METHOD not in load_descriptor_registry().methods:
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
        },
    )


def collect_flye_artifacts(state: TopLevelState) -> dict:
    output = stage_output(state, "assembly")
    [execution] = load_method_executions(state, LIFECYCLE)
    attempt = execution.attempt
    metadata = dict(attempt.metadata or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        execution=execution,
        substep_name="flye_assembly",
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
