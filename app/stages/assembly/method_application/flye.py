"""Flye-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt
from app.storage.publish import shell_publish_file
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.assembly.constants import FLYE_METHOD, FLYE_READ_TYPE_FLAGS
from app.stages.assembly.method_application.lifecycle import (
    MethodLifecycle,
    assembly_output,
    blocked_parameters,
    build_method_graph,
    collect_method_artifacts,
    fail_update,
    positive_int,
    update_assembly_output,
)
from app.stages.base import absolute_data_dir


LIFECYCLE = MethodLifecycle(
    name="flye",
    method_name=FLYE_METHOD,
    planned_status="flye_execution_planned",
    frozen_status="flye_frozen",
    ran_status="flye_ran",
    frozen_attempt_key="flye_frozen_attempt",
    execution_result_key="flye_execution_result",
    draft_assembly_key="flye_draft_assembly_path",
    substep_key="flye_assembly",
)


def build_flye_graph():
    return build_method_graph(LIFECYCLE, freeze_flye_attempt, collect_flye_artifacts)


def freeze_flye_attempt(state: TopLevelState) -> dict:
    """Freeze a local Flye attempt for the selected primary candidate."""
    output = assembly_output(state)
    if output.get("status") != LIFECYCLE.planned_status:
        return {}
    if FLYE_METHOD not in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods:
        return fail_update(state, f"assembly descriptors missing: [{FLYE_METHOD!r}]")

    params, blocked = _parameters(output)
    if blocked:
        return update_assembly_output(state, blocked)
    read_paths, error = _read_paths(output.get("input_ref"), output.get("input_type"))
    if error:
        return fail_update(state, error)

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, env_blocked = resolve_method_env(
        output,
        stage="assembly",
        method_name=FLYE_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if env_blocked is not None:
        return update_assembly_output(state, env_blocked)

    working_dir = data_dir / "work" / "assembly" / session_id / "flye"
    output_dir = working_dir / "flye_out"
    artifact_dir = data_dir / "artifacts" / "assembly" / session_id / "flye"
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    script = working_dir / "run_flye.sh"
    reads = " ".join(shlex.quote(str(path)) for path in read_paths)
    script.write_text(
        "set -e\n"
        f"flye {FLYE_READ_TYPE_FLAGS[params['read_type']]} {reads}"
        f" --out-dir {shlex.quote(str(output_dir))}"
        f" --genome-size {shlex.quote(str(params['estimated_genome_size']))}"
        f" --threads {params['threads']}\n"
        f"test -f {shlex.quote(str(output_dir / 'assembly.fasta'))}\n"
        f"{shell_publish_file(output_dir / 'assembly.fasta', draft)}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:assembly:flye",
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
        },
    )
    return update_assembly_output(
        state,
        {
            "status": LIFECYCLE.frozen_status,
            LIFECYCLE.frozen_attempt_key: attempt.model_dump(mode="json"),
            "flye_output_dir": str(output_dir),
            LIFECYCLE.draft_assembly_key: str(draft),
        },
    )


def collect_flye_artifacts(state: TopLevelState) -> dict:
    output = assembly_output(state)
    attempt_data = output.get(LIFECYCLE.frozen_attempt_key) or {}
    metadata = dict(attempt_data.get("metadata") or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        extra_artifacts=[(Path(output.get("flye_output_dir", "")) / "flye.log", "assembly.flye_log")],
        bundle_logs={},
        bundle_metadata={
            "read_profile": metadata.get("read_profile"),
            "estimated_genome_size": metadata.get("estimated_genome_size"),
            "threads": metadata.get("threads"),
        },
    )


def _read_paths(input_ref: Any, input_type: Any) -> tuple[list[Path], str | None]:
    if not input_ref:
        return [], "Flye local execution requires assembly input_ref"
    if input_type == "long_reads.fofn":
        fofn = Path(str(input_ref))
        if not fofn.exists():
            return [], f"Flye local execution FOFN not found: {fofn}"
        paths = [Path(line.strip()) for line in fofn.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif input_type == "clean_long_reads":
        paths = [Path(str(input_ref))]
    else:
        return [], f"Flye local execution does not support input_type: {input_type}"
    if not paths:
        return [], "Flye local execution requires at least one read path"
    missing = [str(path) for path in paths if not path.exists()]
    return ([], f"Flye local execution read paths not found: {missing}") if missing else (paths, None)


def _parameters(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    params = dict(output.get("flye_parameters") or {})
    missing: list[str] = []
    invalid: list[dict[str, Any]] = []
    read_type = params.get("read_type") or dict(output.get("read_type_prior") or {}).get("flye")
    if not read_type:
        missing.append("flye_parameters.read_type")
    elif read_type not in FLYE_READ_TYPE_FLAGS:
        invalid.append({"param": "flye_parameters.read_type", "value": read_type, "accepted": sorted(FLYE_READ_TYPE_FLAGS)})
    genome_size = positive_int(
        params.get("estimated_genome_size", output.get("genome_size_prior")),
        "flye_parameters.estimated_genome_size",
        missing,
        invalid,
    )
    threads = positive_int(params.get("threads"), "flye_parameters.threads", missing, invalid)
    if missing or invalid:
        return {}, blocked_parameters("flye_parameters", FLYE_METHOD, missing, invalid)
    return {"read_type": read_type, "estimated_genome_size": genome_size, "threads": threads}, None
