"""Hifiasm ONT-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.assembly.command_builder import ParameterCommandError, build_command, build_parameter_sources
from app.stages.assembly.constants import HIFIASM_ONT_METHOD, HIFIASM_ONT_READ_TYPE_FLAGS
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
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.parameter_profile import build_full_parameter_profile
from app.stages.base import absolute_data_dir


LIFECYCLE = MethodLifecycle(
    name="hifiasm_ont",
    method_name=HIFIASM_ONT_METHOD,
    planned_status="hifiasm_ont_execution_planned",
    frozen_status="hifiasm_ont_frozen",
    ran_status="hifiasm_ont_ran",
    frozen_attempt_key="hifiasm_ont_frozen_attempt",
    execution_result_key="hifiasm_ont_execution_result",
    draft_assembly_key="hifiasm_ont_draft_assembly_path",
    substep_key="hifiasm_ont_assembly",
)


def build_hifiasm_ont_graph():
    return build_method_graph(LIFECYCLE, freeze_hifiasm_ont_attempt, collect_hifiasm_ont_artifacts)


# APPEND_MARKER


def freeze_hifiasm_ont_attempt(state: TopLevelState) -> dict:
    """Freeze a local hifiasm ONT attempt for the selected primary candidate."""
    output = assembly_output(state)
    if output.get("status") != LIFECYCLE.planned_status:
        return {}
    if HIFIASM_ONT_METHOD not in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods:
        return fail_update(state, f"assembly descriptors missing: [{HIFIASM_ONT_METHOD!r}]")

    params, blocked = _parameters(output)
    if blocked:
        return update_assembly_output(state, blocked)
    read_paths, error = _read_paths(output.get("input_ref"), output.get("input_type"))
    if error:
        return fail_update(state, error)

    session_id = state["session_id"]
    data_dir = absolute_data_dir(state)
    env_spec, _packages, env_blocked = resolve_method_env(
        output,
        stage="assembly",
        method_name=HIFIASM_ONT_METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if env_blocked is not None:
        return update_assembly_output(state, env_blocked)

    working_dir = data_dir / "work" / "assembly" / session_id / "hifiasm_ont"
    output_dir = working_dir / "hifiasm_ont_out"
    artifact_dir = data_dir / "artifacts" / "assembly" / session_id / "hifiasm_ont"
    working_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    prefix = output_dir / "hifiasm_ont"
    gfa = output_dir / "hifiasm_ont.bp.p_ctg.gfa"
    fallback_gfa = output_dir / "hifiasm_ont.p_ctg.gfa"
    script = working_dir / "run_hifiasm_ont.sh"
    catalog, catalog_error = _load_catalog()
    if catalog_error:
        return fail_update(state, catalog_error)
    recommended_params, recommended_error = _recommended_params(output, "hifiasm_ont_recommended_params")
    if recommended_error:
        return fail_update(state, recommended_error)

    full_profile = build_full_parameter_profile(state, assembly_out=output)

    try:
        parameter_sources = build_parameter_sources(
            method_id=HIFIASM_ONT_METHOD,
            catalog=catalog,
            state=state,
            assembly_out=output,
            attempt_id=int(output.get("current_parameter_attempt_id") or 0),
            recommended_params=recommended_params,
            execution_context={
                "output_prefix": str(prefix),
                "input_reads": [str(path) for path in read_paths],
            },
        )
        argv = build_command(method_id=HIFIASM_ONT_METHOD, catalog=catalog, parameter_sources=parameter_sources)
    except ParameterCommandError as exc:
        return fail_update(state, f"Hifiasm ONT parameter sources are invalid: {exc}")
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
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:assembly:hifiasm_ont",
        session_id=session_id,
        stage="assembly",
        method_name=HIFIASM_ONT_METHOD,
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
            "threads": params["threads"],
            "parameter_sources": parameter_sources,
            "argv": argv,
            "hifiasm_ont_output_dir": str(output_dir),
            "draft_assembly_path": str(draft),
            "full_parameter_profile": full_profile,
        },
    )
    return update_assembly_output(
        state,
        {
            "status": LIFECYCLE.frozen_status,
            LIFECYCLE.frozen_attempt_key: attempt.model_dump(mode="json"),
            "hifiasm_ont_output_dir": str(output_dir),
            LIFECYCLE.draft_assembly_key: str(draft),
        },
    )


def collect_hifiasm_ont_artifacts(state: TopLevelState) -> dict:
    output = assembly_output(state)
    attempt_data = output.get(LIFECYCLE.frozen_attempt_key) or {}
    metadata = dict(attempt_data.get("metadata") or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        extra_artifacts=[],
        bundle_logs={},
        bundle_metadata={
            "read_profile": metadata.get("read_profile"),
            "threads": metadata.get("threads"),
        },
    )


def _read_paths(input_ref: Any, input_type: Any) -> tuple[list[Path], str | None]:
    if not input_ref:
        return [], "Hifiasm ONT local execution requires assembly input_ref"
    if input_type == "long_reads.fofn":
        fofn = Path(str(input_ref))
        if not fofn.exists():
            return [], f"Hifiasm ONT local execution FOFN not found: {fofn}"
        paths = [Path(line.strip()) for line in fofn.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif input_type == "clean_long_reads":
        paths = [Path(str(input_ref))]
    else:
        return [], f"Hifiasm ONT local execution does not support input_type: {input_type}"
    if not paths:
        return [], "Hifiasm ONT local execution requires at least one read path"
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        return [], f"Hifiasm ONT local execution read paths not found: {missing}"

    # Validate FASTQ format requirement for ONT mode
    non_fastq = [str(path) for path in paths if not (str(path).endswith(('.fastq', '.fq', '.fastq.gz', '.fq.gz')))]
    if non_fastq:
        return [], f"Hifiasm ONT mode requires FASTQ format (not FASTA). Non-FASTQ files: {non_fastq}"

    return paths, None


def _parameters(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    params = dict(output.get("hifiasm_ont_parameters") or {})
    missing: list[str] = []
    invalid: list[dict[str, Any]] = []
    read_type = params.get("read_type") or dict(output.get("read_type_prior") or {}).get("hifiasm_ont")
    if not read_type:
        missing.append("hifiasm_ont_parameters.read_type")
    elif read_type not in HIFIASM_ONT_READ_TYPE_FLAGS:
        invalid.append({"param": "hifiasm_ont_parameters.read_type", "value": read_type, "accepted": sorted(HIFIASM_ONT_READ_TYPE_FLAGS)})
    threads = positive_int(params.get("threads"), "hifiasm_ont_parameters.threads", missing, invalid)
    if missing or invalid:
        return {}, blocked_parameters("hifiasm_ont_parameters", HIFIASM_ONT_METHOD, missing, invalid)
    return {"read_type": str(read_type), "threads": threads}, None


def _load_catalog():
    try:
        return load_parameter_catalog(HIFIASM_ONT_METHOD), None
    except (FileNotFoundError, ValueError) as exc:
        return None, f"Hifiasm ONT parameter catalog is not usable: {exc}"


def _recommended_params(output: dict[str, Any], method_key: str) -> tuple[dict[str, Any], str | None]:
    raw = output.get(method_key)
    if raw is None:
        raw = output.get("recommended_params")
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, f"Hifiasm ONT recommended parameters must be a mapping, got {type(raw).__name__}"
    return dict(raw), None
