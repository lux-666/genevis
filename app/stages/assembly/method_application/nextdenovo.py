"""NextDenovo-specific adapter for the shared assembly method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt
from app.storage.publish import shell_publish_file
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.assembly.constants import NEXTDENOVO_METHOD
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
    name="nextdenovo",
    method_name=NEXTDENOVO_METHOD,
    planned_status="nextdenovo_execution_planned",
    frozen_status="nextdenovo_frozen",
    ran_status="nextdenovo_ran",
    frozen_attempt_key="nextdenovo_frozen_attempt",
    execution_result_key="nextdenovo_execution_result",
    draft_assembly_key="nextdenovo_draft_assembly_path",
    substep_key="nextdenovo_assembly",
)
_READ_TYPES = {"ont", "clr", "hifi"}


def build_nextdenovo_graph():
    return build_method_graph(LIFECYCLE, freeze_nextdenovo_attempt, collect_nextdenovo_artifacts)


def freeze_nextdenovo_attempt(state: TopLevelState) -> dict:
    """Freeze a local NextDenovo attempt for the selected primary candidate."""
    output = assembly_output(state)
    if output.get("status") != LIFECYCLE.planned_status:
        return {}
    if NEXTDENOVO_METHOD not in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods:
        return fail_update(state, f"assembly descriptors missing: [{NEXTDENOVO_METHOD!r}]")
    params, blocked = _parameters(output)
    if blocked:
        return update_assembly_output(state, blocked)
    fofn, read_paths, error = _fofn(state, output.get("input_ref"), output.get("input_type"))
    if error:
        return fail_update(state, error)

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
        return update_assembly_output(state, env_blocked)

    working_dir = data_dir / "work" / "assembly" / session_id / "nextdenovo"
    nextdenovo_work_dir = working_dir / "nextdenovo_work"
    artifact_dir = data_dir / "artifacts" / "assembly" / session_id / "nextdenovo"
    working_dir.mkdir(parents=True, exist_ok=True)
    nextdenovo_work_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    draft = artifact_dir / "draft_assembly.fasta"
    run_cfg = working_dir / "run.cfg"
    script = working_dir / "run_nextdenovo.sh"
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
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:assembly:nextdenovo",
        session_id=session_id,
        stage="assembly",
        method_name=NEXTDENOVO_METHOD,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=[draft],
        env=output.get("env"),
        env_spec=env_spec,
        metadata={
            "input_ref": output.get("input_ref"),
            "input_type": output.get("input_type"),
            "input_fofn": str(fofn),
            "read_paths": [str(path) for path in read_paths],
            "read_profile": params["read_type"],
            **params,
            "run_cfg": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
            "draft_assembly_path": str(draft),
        },
    )
    return update_assembly_output(
        state,
        {
            "status": LIFECYCLE.frozen_status,
            LIFECYCLE.frozen_attempt_key: attempt.model_dump(mode="json"),
            "nextdenovo_run_cfg_path": str(run_cfg),
            "nextdenovo_work_dir": str(nextdenovo_work_dir),
            LIFECYCLE.draft_assembly_key: str(draft),
        },
    )


def collect_nextdenovo_artifacts(state: TopLevelState) -> dict:
    output = assembly_output(state)
    attempt_data = output.get(LIFECYCLE.frozen_attempt_key) or {}
    metadata = dict(attempt_data.get("metadata") or {})
    run_cfg = str(output.get("nextdenovo_run_cfg_path", ""))
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        extra_artifacts=[(Path(run_cfg), "assembly.nextdenovo_run_cfg")],
        bundle_logs={"run_cfg": run_cfg},
        bundle_metadata={key: metadata.get(key) for key in ("read_profile", "genome_size", "threads", "read_cutoff")},
    )


def _fofn(state: TopLevelState, input_ref: Any, input_type: Any) -> tuple[Path | None, list[Path], str | None]:
    if not input_ref:
        return None, [], "NextDenovo local execution requires assembly input_ref"
    if input_type == "long_reads.fofn":
        fofn = Path(str(input_ref))
        if not fofn.exists():
            return None, [], f"NextDenovo local execution FOFN not found: {fofn}"
        paths = [Path(line.strip()) for line in fofn.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif input_type == "clean_long_reads":
        paths = [Path(str(input_ref))]
        fofn = absolute_data_dir(state) / "work" / "assembly" / state["session_id"] / "nextdenovo" / "input.fofn"
        fofn.parent.mkdir(parents=True, exist_ok=True)
        fofn.write_text(str(paths[0]) + "\n", encoding="utf-8")
    else:
        return None, [], f"NextDenovo local execution does not support input_type: {input_type}"
    if not paths:
        return None, [], "NextDenovo local execution requires at least one read path"
    missing = [str(path) for path in paths if not path.exists()]
    return (None, [], f"NextDenovo local execution read paths not found: {missing}") if missing else (fofn, paths, None)


def _parameters(output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    params = dict(output.get("nextdenovo_parameters") or {})
    missing: list[str] = []
    invalid: list[dict[str, Any]] = []
    read_type = params.get("read_type") or dict(output.get("read_type_prior") or {}).get("nextdenovo")
    if not read_type:
        missing.append("nextdenovo_parameters.read_type")
    elif read_type not in _READ_TYPES:
        invalid.append({"param": "nextdenovo_parameters.read_type", "value": read_type, "accepted": sorted(_READ_TYPES)})
    genome_size = positive_int(params.get("genome_size", output.get("genome_size_prior")), "nextdenovo_parameters.genome_size", missing, invalid)
    threads = positive_int(params.get("threads"), "nextdenovo_parameters.threads", missing, invalid)
    read_cutoff = positive_int(params.get("read_cutoff", 1000), "nextdenovo_parameters.read_cutoff", missing, invalid)
    if missing or invalid:
        return {}, blocked_parameters("nextdenovo_parameters", NEXTDENOVO_METHOD, missing, invalid)
    return {"read_type": str(read_type), "genome_size": genome_size, "threads": threads, "read_cutoff": read_cutoff}, None


def _run_cfg(work_dir: Path, input_fofn: Path, *, read_type: str, genome_size: int, threads: int, read_cutoff: int) -> str:
    return (
        "[General]\njob_type = local\njob_prefix = nextdenovo\ntask = all\nrewrite = yes\ndeltmp = yes\n"
        f"parallel_jobs = {threads}\ninput_type = raw\nread_type = {read_type}\ninput_fofn = {input_fofn}\nworkdir = {work_dir}\n\n"
        "[correct_option]\n"
        f"read_cutoff = {read_cutoff}\ngenome_size = {genome_size}\nsort_options = -m 1g -t {threads}\n"
        f"minimap2_options_raw = -t {threads}\npa_correction = 3\ncorrection_options = -p {threads}\n\n"
        f"[assemble_option]\nminimap2_options_cns = -t {threads}\nnextgraph_options = -a 1\n"
    )
