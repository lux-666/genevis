"""YaHS adapter using the shared Hi-C method lifecycle."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt
from app.schemas.artifacts import HIC_SCAFFOLD_AGP
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir
from app.stages.hic_scaffolding.method_application.lifecycle import (
    HiCMethodLifecycle,
    build_method_graph,
    collect_method_artifacts,
    hic_output,
    update_hic_output,
)

METHOD = "hic_scaffolding.candidate.yahs"
LIFECYCLE = HiCMethodLifecycle(
    name="yahs",
    method_name=METHOD,
    frozen_status="yahs_frozen",
    ran_status="yahs_ran",
    frozen_attempt_key="yahs_frozen_attempt",
    execution_result_key="yahs_execution_result",
)


def build_yahs_graph():
    return build_method_graph(LIFECYCLE, freeze_yahs_attempt, collect_yahs_artifacts)


def freeze_yahs_attempt(state: TopLevelState) -> dict:
    """Freeze a YaHS command when the selected method has a BAM input."""
    output = hic_output(state)
    if not _enabled(output):
        return {}
    if METHOD not in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods:
        return update_hic_output(state, {"status": "failed", "reason": f"hic_scaffolding descriptors missing: [{METHOD!r}]"})

    assembly = Path(str(output.get("pre_hic_assembly_ref", "")))
    if not assembly.exists():
        return update_hic_output(state, {"status": "blocked_human", "notes": _notes(output, "pre_hic_assembly_not_materialized_for_yahs")})
    alignment, error = _alignment_path(output)
    if error:
        return update_hic_output(state, {"status": "blocked_human", "notes": _notes(output, error)})

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    env_spec, blocked = resolve_method_env(
        output,
        stage="hic_scaffolding",
        method_name=METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if blocked is not None:
        return update_hic_output(state, blocked)

    working_dir = data_dir / "work" / "hic_scaffolding" / session_id / "yahs"
    artifact_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id / "yahs"
    out_prefix = working_dir / "yahs_out" / "yahs"
    scaffold_fasta = artifact_dir / "chromosome_scaffolded_assembly.fasta"
    scaffold_agp = artifact_dir / "chromosome_scaffolded_assembly.agp"
    yahs_fasta = Path(str(out_prefix) + "_scaffolds_final.fa")
    yahs_agp = Path(str(out_prefix) + "_scaffolds_final.agp")
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    script = working_dir / "run_yahs.sh"
    script.write_text(
        "set -e\n"
        f"mkdir -p {shlex.quote(str(out_prefix.parent))} {shlex.quote(str(artifact_dir))}\n"
        f"yahs -o {shlex.quote(str(out_prefix))} {shlex.quote(str(assembly))} {shlex.quote(str(alignment))}\n"
        f"test -f {shlex.quote(str(yahs_fasta))}\n"
        f"cp {shlex.quote(str(yahs_fasta))} {shlex.quote(str(scaffold_fasta))}\n"
        f"if test -f {shlex.quote(str(yahs_agp))}; then cp {shlex.quote(str(yahs_agp))} {shlex.quote(str(scaffold_agp))}; fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:hic_scaffolding:yahs",
        session_id=session_id,
        stage="hic_scaffolding",
        method_name=METHOD,
        script_path=script,
        working_dir=working_dir,
        expected_outputs=[scaffold_fasta],
        env=output.get("env"),
        env_spec=env_spec,
        metadata={
            "artifact_dir": str(artifact_dir),
            "pre_hic_assembly_ref": str(assembly),
            "hic_alignment_ref": str(alignment),
            "scaffold_fasta": str(scaffold_fasta),
            "scaffold_agp": str(scaffold_agp),
            "out_prefix": str(out_prefix),
        },
    )
    return update_hic_output(state, {"status": LIFECYCLE.frozen_status, LIFECYCLE.frozen_attempt_key: attempt.model_dump(mode="json")})


def collect_yahs_artifacts(state: TopLevelState) -> dict:
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        extra_artifacts=[("scaffold_agp", HIC_SCAFFOLD_AGP)],
        execution_metadata={"input_mode": "hic_alignment_bam"},
    )


def is_enabled(output: dict[str, Any]) -> bool:
    return _enabled(output)


def _enabled(output: dict[str, Any]) -> bool:
    methods = output.get("selected_methods") or [output.get("primary_method") or output.get("selected_method_name")]
    return output.get("execution_mode") == "local" and METHOD in methods


def _alignment_path(output: dict[str, Any]) -> tuple[Path | None, str | None]:
    for key in ["hic_alignment_bam_ref", "hic_alignment_ref"]:
        value = output.get(key)
        if value:
            path = Path(str(value))
            return (path, None) if path.exists() else (None, "alignment_required_before_yahs")
    for value in output.get("hic_alignment_paths") or []:
        path = Path(str(value))
        if path.exists():
            return path, None
    return None, "alignment_required_before_yahs"


def _notes(output: dict[str, Any], note: str) -> list[str]:
    return list(dict.fromkeys([*(output.get("notes") or []), note]))
