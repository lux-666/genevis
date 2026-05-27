"""Interactive TUI for gene-agent: guides users through assembly or annotation workflows.

Entry point: `gene-agent tui`

Two modes:
  1. Assembly from raw reads → full pipeline (read_qc → ... → function_annotation)
  2. Annotation from existing assembly → skip assembly stages, start at annotation
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any


def run_tui(data_dir: str) -> int:
    """Top-level TUI entry. Returns CLI exit code."""
    try:
        return _tui_loop(data_dir)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1


def _tui_loop(data_dir: str) -> int:
    print("\n=== gene-agent interactive ===\n")
    print("Select workflow mode:")
    print("  1. Assembly from raw reads (full pipeline)")
    print("  2. Annotation from existing assembly")
    choice = _prompt_choice("mode", ["assembly", "annotation"])
    if choice is None:
        return 1

    if choice == "assembly":
        return _assembly_workflow(data_dir)
    else:
        return _annotation_workflow(data_dir)


def _assembly_workflow(data_dir: str) -> int:
    print("\n--- Assembly from raw reads ---\n")

    reads_path = _prompt_path("Path to raw long reads (FASTA/FASTQ)")
    if not reads_path:
        return 1

    platform = _prompt_choice("Sequencing platform", [
        "ont_raw", "ont_hq", "pacbio_clr", "pacbio_hifi", "cycloneseq",
    ])
    if not platform:
        return 1

    genome_size = _prompt_input("Estimated genome size (e.g. 500m, 2.5g, 5000000)")
    if not genome_size:
        return 1
    genome_size_int = _parse_genome_size(genome_size)
    if genome_size_int is None:
        print(f"error: invalid genome size: {genome_size!r}", file=sys.stderr)
        return 1

    threads = _prompt_input("Threads for assembly/QC [4]") or "4"
    try:
        threads_int = int(threads)
    except ValueError:
        print(f"error: invalid threads: {threads!r}", file=sys.stderr)
        return 1

    repeat_lib = _prompt_path("Path to repeat library (optional, Enter to skip)", required=False)

    review_mode = _prompt_choice("Review policy", ["strict", "auto"])
    if not review_mode:
        return 1

    # Map platform to shared read_type
    read_type = _platform_to_read_type(platform)

    # Build manifest
    manifest = _build_manifest(
        reads_path=reads_path,
        platform=platform,
        repeat_lib=repeat_lib,
        genome_size=genome_size_int,
    )

    # Build run-config
    local_exec = ["read_qc", "assembly", "assembly_review", "repeat_annotation"]
    run_config = _build_run_config(
        local_exec=local_exec,
        read_type=read_type,
        genome_size=genome_size_int,
        threads=threads_int,
        review_mode=review_mode,
    )

    return _execute_with_gate_loop(manifest, run_config, data_dir, local_exec)


def _annotation_workflow(data_dir: str) -> int:
    print("\n--- Annotation from existing assembly ---\n")

    assembly_path = _prompt_path("Path to assembly FASTA")
    if not assembly_path:
        return 1

    repeat_lib = _prompt_path("Path to repeat library (optional, Enter to skip)", required=False)

    review_mode = _prompt_choice("Review policy for assembly_review pass-through", ["auto", "strict"])
    if not review_mode:
        return 1

    manifest = _build_annotation_manifest(
        assembly_path=assembly_path,
        repeat_lib=repeat_lib,
    )

    local_exec = ["assembly_review", "repeat_annotation"]
    run_config = _build_run_config(
        local_exec=local_exec,
        read_type="ont_raw",
        genome_size=5_000_000,
        threads=4,
        review_mode=review_mode,
    )

    return _execute_with_gate_loop(manifest, run_config, data_dir, local_exec)


def _execute_with_gate_loop(
    manifest: dict,
    run_config: dict,
    data_dir: str,
    local_exec: list[str],
) -> int:
    """Write temp manifest/config, run the graph, loop on human gates."""
    from app.entrypoints.run import _parse_args, _resolve_data_dir, generate_session_id

    data_dir = _resolve_data_dir(data_dir)
    session_id = generate_session_id(data_dir)

    manifest["session_id"] = session_id
    manifest_dir = Path(data_dir) / "manifests" / session_id
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.yaml"

    import yaml
    manifest_path.write_text(
        yaml.dump(manifest, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )

    config_path = manifest_dir / "run_config.json"
    config_path.write_text(
        json.dumps(run_config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nsession_id:  {session_id}")
    print(f"manifest:    {manifest_path}")
    print(f"run_config:  {config_path}")
    print(f"data_dir:    {data_dir}")
    print(f"local_exec:  {local_exec}")

    confirm = _prompt_input("\nProceed? [Y/n]") or "y"
    if confirm.lower() not in ("y", "yes", ""):
        print("Cancelled.")
        return 1

    print("\n--- Starting workflow ---\n")

    # Build argv for _run_session
    argv = [
        "run",
        "--manifest", str(manifest_path),
        "--run-config", str(config_path),
        "--session-id", session_id,
        "--data-dir", data_dir,
        "--checkpointer", "sqlite",
    ]
    for stage in local_exec:
        argv.extend(["--local-exec", stage])

    args = _parse_args(argv)

    from app.entrypoints.run import _run_session_core
    return _run_session_core(args, interactive_gate=True)


# --- Helpers ---

def _prompt_choice(label: str, options: list[str]) -> str | None:
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    try:
        raw = input(f"select [{1}-{len(options)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return options[0]
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        if raw in options:
            return raw
    print(f"invalid: {raw!r}", file=sys.stderr)
    return None


def _prompt_path(label: str, *, required: bool = True) -> str | None:
    try:
        raw = input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None if required else None
    path = Path(raw).expanduser().resolve(strict=False)
    if required and not path.exists():
        print(f"warning: path does not exist: {path}", file=sys.stderr)
    return str(path)


def _prompt_input(label: str) -> str | None:
    try:
        return input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _parse_genome_size(raw: str) -> int | None:
    raw = raw.strip().lower()
    multipliers = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
    if raw and raw[-1] in multipliers:
        try:
            return int(float(raw[:-1]) * multipliers[raw[-1]])
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return None


def _platform_to_read_type(platform: str) -> str:
    mapping = {
        "ont_raw": "ont_raw",
        "ont_hq": "ont_hq",
        "pacbio_clr": "pacbio_clr",
        "pacbio_hifi": "pacbio_hifi",
        "cycloneseq": "ont_raw",
    }
    return mapping.get(platform, "ont_raw")


def _build_manifest(
    reads_path: str,
    platform: str,
    repeat_lib: str | None,
    genome_size: int,
) -> dict:
    manifest_platform = platform if platform != "cycloneseq" else "ont"
    assets = [
        {
            "asset_type": "raw_long_reads",
            "paths": [reads_path],
            "format": "fasta" if reads_path.endswith((".fa", ".fasta", ".fna")) else "fastq",
            "platform": manifest_platform,
        }
    ]
    if repeat_lib:
        assets.append({
            "asset_type": "repeat_library",
            "paths": [repeat_lib],
            "format": "fasta",
            "platform": "unknown",
        })
    return {
        "session_id": "__placeholder__",
        "genome_size_estimate": genome_size,
        "assets": assets,
    }


def _build_annotation_manifest(
    assembly_path: str,
    repeat_lib: str | None,
) -> dict:
    assets = [
        {
            "asset_type": "assembly_fasta_registered",
            "paths": [assembly_path],
            "format": "fasta",
            "platform": "unknown",
        }
    ]
    if repeat_lib:
        assets.append({
            "asset_type": "repeat_library",
            "paths": [repeat_lib],
            "format": "fasta",
            "platform": "unknown",
        })
    return {
        "session_id": "__placeholder__",
        "assets": assets,
    }


def _build_run_config(
    local_exec: list[str],
    read_type: str,
    genome_size: int,
    threads: int,
    review_mode: str,
) -> dict:
    config: dict[str, Any] = {
        "local_exec": local_exec,
        "shared_parameters": {
            "read_type": read_type,
            "estimated_genome_size": genome_size,
        },
        "review_policy": {"mode": review_mode},
    }
    if "assembly" in local_exec:
        config["assembly"] = {
            "flye_parameters": {"threads": threads}
        }
    if "assembly_review" in local_exec:
        config["assembly_review"] = {
            "quast": {"threads": threads}
        }
    config["function_annotation"] = {
        "database_provenance": [
            {
                "method_name": "function_annotation.candidate.interproscan",
                "database_name": "InterPro",
                "database_version": "operator_supplied",
                "snapshot_date": "2026-01-01",
                "database_source_ref": "/path/to/interpro",
                "descriptor_version": "operator_supplied",
                "input_proteins_ref": "/path/to/proteins.faa",
                "command": "operator supplied",
            }
        ]
    }
    return config
