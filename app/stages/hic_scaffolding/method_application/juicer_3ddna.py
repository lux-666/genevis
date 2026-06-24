"""Juicer plus 3D-DNA adapter for the shared Hi-C method lifecycle."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from app.execution.env_dispatch import resolve_method_env
from app.execution.method_runner import build_frozen_attempt
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import TopLevelState
from app.storage.publish import shell_publish_variable
from app.stages.base import absolute_data_dir
from app.stages.hic_scaffolding.method_application.lifecycle import (
    HiCMethodLifecycle,
    build_method_graph,
    collect_method_artifacts,
    hic_output,
    update_hic_output,
)

METHOD = "hic_scaffolding.candidate.juicer_3ddna_juicebox"
LIFECYCLE = HiCMethodLifecycle(
    name="juicer_3ddna",
    method_name=METHOD,
    frozen_status="juicer_3ddna_frozen",
    ran_status="juicer_3ddna_ran",
    frozen_attempt_key="juicer_3ddna_frozen_attempt",
    execution_result_key="juicer_3ddna_execution_result",
)


def build_juicer_3ddna_graph():
    return build_method_graph(LIFECYCLE, freeze_juicer_3ddna_attempt, collect_juicer_3ddna_artifacts)


def freeze_juicer_3ddna_attempt(state: TopLevelState) -> dict:
    """Freeze either Juicer -> 3D-DNA or direct 3D-DNA from merged_nodups."""
    output = hic_output(state)
    if not is_enabled(output):
        return {}
    if METHOD not in DescriptorRegistry.load(Path("descriptors/registry.yaml")).methods:
        return update_hic_output(state, {"status": "failed", "reason": f"hic_scaffolding descriptors missing: [{METHOD!r}]"})

    assembly = Path(str(output.get("pre_hic_assembly_ref", "")))
    if not assembly.exists():
        return _blocked(state, output, "pre_hic_assembly_not_materialized_for_3ddna")
    parameters, blocked = _parameters(state, output)
    if blocked:
        return update_hic_output(state, blocked)

    data_dir = absolute_data_dir(state)
    session_id = state["session_id"]
    env_spec, env_blocked = resolve_method_env(
        output,
        stage="hic_scaffolding",
        method_name=METHOD,
        session_id=session_id,
        data_dir=data_dir,
        input_manifest_ref=state.get("input_manifest_ref", ""),
    )
    if env_blocked is not None:
        return update_hic_output(state, env_blocked)
    if parameters["input_mode"] == "raw_reads":
        generator, generator_blocked = _resolve_site_generator(
            env_spec=env_spec,
            stage_env=output.get("env"),
            juicer_command=parameters["juicer_parameters"]["command"],
        )
        if generator_blocked is not None:
            return update_hic_output(state, generator_blocked)
        parameters["juicer_parameters"].update(generator)

    working_dir = data_dir / "work" / "hic_scaffolding" / session_id / "juicer_3ddna"
    artifact_dir = data_dir / "artifacts" / "hic_scaffolding" / session_id / "juicer_3ddna"
    juicer_dir = working_dir / "juicer"
    three_dna_dir = working_dir / "3ddna"
    input_assembly = three_dna_dir / "input_assembly.fasta"
    generated_mnd = juicer_dir / "aligned" / "merged_nodups.txt"
    scaffold_fasta = artifact_dir / "chromosome_scaffolded_assembly.fasta"
    contact_map = artifact_dir / "contact_map.hic"
    assembly_file = artifact_dir / "3ddna.assembly"
    working_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    script = working_dir / "run_juicer_3ddna.sh"
    script.write_text(
        _script(
            assembly=assembly,
            params=parameters,
            juicer_dir=juicer_dir,
            three_dna_dir=three_dna_dir,
            input_assembly=input_assembly,
            generated_mnd=generated_mnd,
            scaffold_fasta=scaffold_fasta,
            contact_map=contact_map,
            assembly_file=assembly_file,
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    merged_nodups = generated_mnd if parameters["input_mode"] == "raw_reads" else Path(parameters["merged_nodups_path"])
    attempt = build_frozen_attempt(
        attempt_id=f"{session_id}:hic_scaffolding:juicer_3ddna",
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
            "input_mode": parameters["input_mode"],
            "hic_read_paths": parameters.get("hic_read_paths", []),
            "merged_nodups_path": str(merged_nodups),
            "chromosome_count": parameters["chromosome_count"],
            "juicer_parameters": parameters.get("juicer_parameters", {}),
            "juicer_site_generator": parameters.get("juicer_parameters", {}).get("site_generator_path"),
            "three_dna_command": parameters["three_dna_command"],
            "three_dna_extra_args": parameters["three_dna_extra_args"],
            "scaffold_fasta": str(scaffold_fasta),
            "contact_map": str(contact_map),
            "assembly_file": str(assembly_file),
        },
    )
    return update_hic_output(state, {"status": LIFECYCLE.frozen_status, LIFECYCLE.frozen_attempt_key: attempt.model_dump(mode="json")})


def collect_juicer_3ddna_artifacts(state: TopLevelState) -> dict:
    output = hic_output(state)
    attempt = dict(output.get(LIFECYCLE.frozen_attempt_key) or {})
    metadata = dict(attempt.get("metadata") or {})
    return collect_method_artifacts(
        state,
        LIFECYCLE,
        extra_artifacts=[("contact_map", "hic_scaffolding.contact_map"), ("assembly_file", "hic_scaffolding.3ddna_assembly")],
        execution_metadata={
            "input_mode": metadata.get("input_mode"),
            "merged_nodups_path": metadata.get("merged_nodups_path"),
            "chromosome_count": metadata.get("chromosome_count"),
            "juicer_parameters": metadata.get("juicer_parameters", {}),
        },
    )


def is_enabled(output: dict[str, Any]) -> bool:
    methods = output.get("selected_methods") or [output.get("primary_method") or output.get("selected_method_name")]
    return output.get("execution_mode") == "local" and METHOD in methods


def _parameters(state: TopLevelState, output: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    raw = dict(output.get("juicer_3ddna_parameters") or {})
    requested_mode = output.get("input_mode", raw.get("input_mode", "auto"))
    if requested_mode not in {"auto", "raw_reads", "merged_nodups"}:
        return {}, _parameter_block("juicer_3ddna_parameters.input_mode", requested_mode, "must be auto, raw_reads, or merged_nodups")
    merged_nodups = _merged_nodups_path(state, output, raw)
    read_paths = [Path(str(path)) for path in output.get("hic_read_paths") or []]
    if requested_mode == "auto":
        mode = "merged_nodups" if merged_nodups else "raw_reads"
    else:
        mode = requested_mode

    chromosome_count = _positive_int(
        output.get("chromosome_count", raw.get("chromosome_count")),
        "chromosome_count",
    )
    if chromosome_count is None:
        return {}, _missing_block("chromosome_count")
    three_dna_command = str(raw.get("three_dna_command") or "run-asm-pipeline.sh")
    extra_args, error = _string_list(raw.get("three_dna_extra_args", []), "juicer_3ddna_parameters.three_dna_extra_args")
    if error:
        return {}, error
    if mode == "merged_nodups":
        if not merged_nodups:
            return {}, _missing_block("merged_nodups_ref or hic_merged_nodups asset")
        return {
            "input_mode": mode,
            "merged_nodups_path": str(merged_nodups),
            "chromosome_count": chromosome_count,
            "three_dna_command": three_dna_command,
            "three_dna_extra_args": extra_args,
        }, None

    if len(read_paths) < 2:
        return {}, _missing_block("at least two registered Hi-C read paths for Juicer")
    missing_paths = [str(path) for path in read_paths if not path.exists()]
    if missing_paths:
        return {}, _parameter_block("hic_read_paths", missing_paths, "registered Hi-C read paths do not exist")
    enzyme = str(raw.get("restriction_enzyme") or "").strip()
    chrom_sizes = Path(str(raw.get("chrom_sizes_ref") or ""))
    threads = _positive_int(raw.get("threads"), "juicer_3ddna_parameters.threads")
    if not enzyme:
        return {}, _missing_block("juicer_3ddna_parameters.restriction_enzyme")
    if not chrom_sizes.exists():
        return {}, _missing_block("juicer_3ddna_parameters.chrom_sizes_ref")
    if threads is None:
        return {}, _missing_block("juicer_3ddna_parameters.threads")
    juicer_extra_args, error = _string_list(raw.get("juicer_extra_args", []), "juicer_3ddna_parameters.juicer_extra_args")
    if error:
        return {}, error
    return {
        "input_mode": mode,
        "hic_read_paths": [str(path) for path in read_paths],
        "chromosome_count": chromosome_count,
        "three_dna_command": three_dna_command,
        "three_dna_extra_args": extra_args,
        "juicer_parameters": {
            "command": str(raw.get("juicer_command") or "juicer.sh"),
            "restriction_enzyme": enzyme,
            "chrom_sizes_ref": str(chrom_sizes),
            "threads": threads,
            "extra_args": juicer_extra_args,
        },
    }, None


def _script(
    *,
    assembly: Path,
    params: dict[str, Any],
    juicer_dir: Path,
    three_dna_dir: Path,
    input_assembly: Path,
    generated_mnd: Path,
    scaffold_fasta: Path,
    contact_map: Path,
    assembly_file: Path,
) -> str:
    lines = ["set -e", f"mkdir -p {shlex.quote(str(three_dna_dir))} {shlex.quote(str(scaffold_fasta.parent))}"]
    if params["input_mode"] == "raw_reads":
        fastq_dir = juicer_dir / "fastq"
        site_dir = juicer_dir / "restriction_sites"
        juicer_reference = juicer_dir / "reference.fasta"
        jp = params["juicer_parameters"]
        site_genome_name = juicer_reference.stem
        site_file = site_dir / f"{site_genome_name}_{jp['restriction_enzyme']}.txt"
        lines.append(
            f"mkdir -p {shlex.quote(str(fastq_dir))} {shlex.quote(str(generated_mnd.parent))} {shlex.quote(str(site_dir))}"
        )
        for index, read_path in enumerate(params["hic_read_paths"], start=1):
            link = fastq_dir / f"{index}_{Path(read_path).name}"
            lines.append(f"ln -sfn {shlex.quote(read_path)} {shlex.quote(str(link))}")
        lines.append(f"cp {shlex.quote(str(assembly))} {shlex.quote(str(juicer_reference))}")
        generator_command = [
            *jp["site_generator_command"],
            jp["site_generator_path"],
            jp["restriction_enzyme"],
            site_genome_name,
            str(juicer_reference),
        ]
        lines.append(f"(cd {shlex.quote(str(site_dir))} && " + " ".join(shlex.quote(str(value)) for value in generator_command) + ")")
        lines.append(f"site_file={shlex.quote(str(site_file))}")
        lines.append('test -s "$site_file"')
        lines.append(f"bwa index {shlex.quote(str(juicer_reference))}")
        command = [
            jp["command"], "-d", str(juicer_dir), "-s", jp["restriction_enzyme"], "-z", str(juicer_reference),
            "-p", jp["chrom_sizes_ref"], "-y", "${site_file}", "-t", str(jp["threads"]), *jp["extra_args"],
        ]
        lines.append(" ".join("\"$site_file\"" if value == "${site_file}" else shlex.quote(str(value)) for value in command))
        lines.append(f"test -f {shlex.quote(str(generated_mnd))}")
        merged_nodups = generated_mnd
    else:
        merged_nodups = Path(params["merged_nodups_path"])
        lines.append(f"test -f {shlex.quote(str(merged_nodups))}")
    lines.extend(
        [
            f"cp {shlex.quote(str(assembly))} {shlex.quote(str(input_assembly))}",
            f"cd {shlex.quote(str(three_dna_dir))}",
            " ".join(
                shlex.quote(str(value))
                for value in [params["three_dna_command"], *params["three_dna_extra_args"], str(input_assembly), str(merged_nodups)]
            ),
            "for candidate in input_assembly.FINAL.fasta input_assembly.final.fasta input_assembly.FINAL.fa ./*.FINAL.fasta; do",
            "  if test -f \"$candidate\"; then " + shell_publish_variable("candidate", scaffold_fasta) + "; break; fi",
            "done",
            f"test -f {shlex.quote(str(scaffold_fasta))}",
            "for candidate in input_assembly.FINAL.assembly input_assembly.assembly ./*.assembly; do",
            "  if test -f \"$candidate\"; then " + shell_publish_variable("candidate", assembly_file) + "; break; fi",
            "done",
            "for candidate in ./*.hic; do",
            "  if test -f \"$candidate\"; then " + shell_publish_variable("candidate", contact_map) + "; break; fi",
            "done",
        ]
    )
    return "\n".join(lines) + "\n"


def _merged_nodups_path(state: TopLevelState, output: dict[str, Any], params: dict[str, Any]) -> Path | None:
    for value in [params.get("merged_nodups_ref"), output.get("merged_nodups_ref"), output.get("hic_merged_nodups_ref")]:
        if value and Path(str(value)).exists():
            return Path(str(value))
    registry_ref = state.get("asset_registry_ref")
    if not registry_ref or not Path(registry_ref).exists():
        return None
    registry = json.loads(Path(registry_ref).read_text(encoding="utf-8"))
    for asset in registry.get("assets", []):
        if asset.get("asset_type") in {"hic_merged_nodups", "merged_nodups_txt", "juicer_merged_nodups"}:
            path = Path(str(asset.get("path") or ""))
            if path.exists():
                return path
    return None


def _resolve_site_generator(
    *,
    env_spec: Any,
    stage_env: Any,
    juicer_command: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Find Juicer's generator in the execution environment before freezing.

    Juicer itself defaults to ``/aidenlab/restriction_sites`` when no ``-y``
    file is supplied.  We never use that fallback: discover the auxiliary
    generator from the selected conda environment (or an explicit PATH test
    override), then make a fresh site file inside this attempt's work tree.
    """
    probe = r'''
set -eu
juicer_command="$1"
juicer_path="$(command -v "$juicer_command")" || {
  echo "Juicer command is unavailable while resolving its site generator: $juicer_command" >&2
  exit 127
}
for root in "${CONDA_PREFIX:-}" "$(dirname "$juicer_path")" "$(dirname "$(dirname "$juicer_path")")" "$(dirname "$(dirname "$(dirname "$juicer_path")")")"; do
  test -n "$root" && test -d "$root" || continue
  generator="$(find "$root" -type f -name generate_site_positions.py -print -quit 2>/dev/null || true)"
  test -n "$generator" || continue
  if test -x "$generator"; then
    printf '%s\t%s\n' "$generator" direct
    exit 0
  fi
  if python_path="$(command -v python3 2>/dev/null || command -v python 2>/dev/null)"; then
    printf '%s\t%s\n' "$generator" "$python_path"
    exit 0
  fi
  echo "site generator is not executable and Python is unavailable: $generator" >&2
  exit 127
done
echo "generate_site_positions.py was not found in CONDA_PREFIX or near Juicer: $juicer_path" >&2
exit 127
'''
    if env_spec is not None:
        command = [*env_spec.conda_run_prefix(), "sh", "-c", probe, "site-generator-preflight", juicer_command]
        env = None
        env_target = env_spec.display_target()
    else:
        command = ["sh", "-c", probe, "site-generator-preflight", juicer_command]
        env = os.environ.copy()
        if isinstance(stage_env, dict):
            env.update({str(key): str(value) for key, value in stage_env.items()})
        env_target = "explicit PATH override"
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {}, {
            "status": "missing_tool",
            "reason": detail or f"generate_site_positions.py is unavailable in {env_target}",
            "env_target": env_target,
            "missing_tools": ["generate_site_positions.py"],
            "method_name": METHOD,
        }
    generator_path, separator, interpreter = result.stdout.strip().partition("\t")
    if not generator_path or not separator or not interpreter:
        return {}, {
            "status": "missing_tool",
            "reason": f"invalid generate_site_positions.py preflight output in {env_target}",
            "env_target": env_target,
            "missing_tools": ["generate_site_positions.py"],
            "method_name": METHOD,
        }
    return {
        "site_generator_path": generator_path,
        "site_generator_command": [] if interpreter == "direct" else [interpreter],
    }, None


def _positive_int(value: Any, _name: str) -> int | None:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted > 0 else None


def _string_list(value: Any, name: str) -> tuple[list[str], dict[str, Any] | None]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return [], _parameter_block(name, value, "must be a list of non-empty command arguments")
    return value, None


def _missing_block(field: str) -> dict[str, Any]:
    return {"status": "blocked_param", "missing_params": [field], "invalid_params": [], "method_name": METHOD}


def _parameter_block(field: str, value: Any, reason: str) -> dict[str, Any]:
    return {"status": "blocked_param", "missing_params": [], "invalid_params": [{"param": field, "value": value, "reason": reason}], "method_name": METHOD}


def _blocked(state: TopLevelState, output: dict[str, Any], note: str) -> dict:
    return update_hic_output(state, {"status": "blocked_human", "notes": list(dict.fromkeys([*(output.get("notes") or []), note]))})
