"""Materialize user source paths into the internal input manifest contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.schemas.entry import EntryRequest
from app.schemas.manifest import InputManifest, _detect_format, _detect_platform
from app.storage.materialization import materialize_run_layout
from app.storage.paths import SessionPaths, ensure_run_layout

_MANIFEST_SUFFIXES = {".yaml", ".yml", ".json"}
_SUPPORTED_SOURCE_SUFFIXES = {
    ".fa",
    ".fasta",
    ".fna",
    ".fa.gz",
    ".fasta.gz",
    ".fna.gz",
    ".fq",
    ".fastq",
    ".fq.gz",
    ".fastq.gz",
    ".bam",
    ".cram",
    ".hic",
}


def materialize_entry_manifest(request: EntryRequest) -> EntryRequest:
    """Return an EntryRequest whose manifest points to a real manifest file."""

    if request.manifest:
        if request.genome_size_estimate is not None:
            if not request.session_id:
                raise ValueError(
                    "session_id is required to materialize a genome-size override"
                )
            manifest_path = materialize_manifest_genome_size(
                manifest_path=Path(request.manifest),
                data_dir=request.data_dir,
                session_id=request.session_id,
                genome_size_estimate=request.genome_size_estimate,
            )
            return request.model_copy(update={"manifest": str(manifest_path)})
        return request
    if not request.session_id:
        raise ValueError("session_id is required to materialize a manifest")

    source_paths = _expand_source_paths(request.source_paths)
    if len(source_paths) == 1 and source_paths[0].suffix.lower() in _MANIFEST_SUFFIXES:
        return request.model_copy(update={"manifest": str(source_paths[0])})

    assets = _build_asset_specs(source_paths, request)
    paths = SessionPaths(request.data_dir, request.session_id)
    ensure_run_layout(paths)
    manifest_path = paths.manifest_path
    payload: dict[str, Any] = {
        "session_id": request.session_id,
        "assets": assets,
        "target_assembly_level": request.target_assembly_level,
    }
    if request.sample_id:
        payload["sample_id"] = request.sample_id
    if request.target_species:
        payload["user_metadata"] = {"target_species": request.target_species}
    if request.genome_size_estimate is not None:
        payload["genome_size_estimate"] = request.genome_size_estimate
    manifest_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return request.model_copy(update={"manifest": str(manifest_path)})


def materialize_manifest_genome_size(
    *,
    manifest_path: Path,
    data_dir: str,
    session_id: str,
    genome_size_estimate: int | None,
) -> Path:
    """Write one session-local manifest containing the confirmed provided size."""

    paths = SessionPaths(data_dir, session_id)
    materialize_run_layout(
        paths,
        manifest_path=manifest_path,
        run_config_path=None,
    )
    payload = yaml.safe_load(paths.manifest_path.read_text(encoding="utf-8")) or {}
    manifest = InputManifest.model_validate(payload)
    if genome_size_estimate is not None:
        manifest = manifest.model_copy(
            update={"genome_size_estimate": genome_size_estimate}
        )
    paths.manifest_path.write_text(
        yaml.safe_dump(
            manifest.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return paths.manifest_path


def _expand_source_paths(source_paths: list[str]) -> list[Path]:
    expanded: list[Path] = []
    for raw_path in source_paths:
        path = Path(raw_path).expanduser().resolve(strict=False)
        if path.is_dir():
            expanded.extend(_scan_source_dir(path))
        else:
            if (
                path.suffix.lower() not in _MANIFEST_SUFFIXES
                and not _is_supported_data_source(path)
            ):
                raise ValueError(f"unsupported source file type: {path}")
            expanded.append(path)
    if not expanded:
        raise ValueError("source_paths did not contain any supported input files")
    return expanded


def _scan_source_dir(path: Path) -> list[Path]:
    files = [
        candidate
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file() and _is_supported_data_source(candidate)
    ]
    if not files:
        raise ValueError(f"source directory contains no supported input files: {path}")
    return files


def _is_supported_data_source(path: Path) -> bool:
    return _compound_suffix(path) in _SUPPORTED_SOURCE_SUFFIXES


def _build_asset_specs(
    source_paths: list[Path], request: EntryRequest
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[Path]] = {}
    for path in source_paths:
        asset_type = request.asset_type_hint or _infer_asset_type(path)
        platform = (
            request.platform
            if request.platform != "unknown"
            else _detect_platform(path, "unknown", {})
        )
        grouped.setdefault((asset_type, _detect_format(path), platform), []).append(
            path
        )

    assets = []
    for (asset_type, file_format, asset_platform), paths in grouped.items():
        spec: dict[str, Any] = {
            "asset_type": asset_type,
            "paths": [str(path) for path in paths],
            "format": file_format,
            "platform": asset_platform,
            "metadata": {"manifest_materialized_from": "ask"},
        }
        if request.sample_id:
            spec["sample_id"] = request.sample_id
        assets.append(spec)
    return assets


def _infer_asset_type(path: Path) -> str:
    """
    Infer asset type from file path when LLM doesn't provide asset_type_hint.

    Conservative strategy: when ambiguous (e.g., .fa can be reads or assembly),
    prefer raw_long_reads to avoid skipping QC stages. Users can clarify later.
    """
    suffix = _compound_suffix(path)
    name = path.name.lower()

    # .fa/.fasta can be either reads (PacBio/Nanopore) or assembly
    # Heuristic: check filename for assembly indicators
    if suffix in {".fa", ".fasta", ".fna", ".fa.gz", ".fasta.gz", ".fna.gz"}:
        # Strong assembly indicators in filename
        if any(
            kw in name
            for kw in ["assembly", "genome", "contig", "scaffold", "chromosome"]
        ):
            return "assembly_fasta_registered"
        # Otherwise, conservatively treat as raw reads (safe default for
        # workflows starting from assembly)
        return "raw_long_reads"

    if suffix in {".bam", ".cram"} and ("hic" in name or "hi-c" in name):
        return "hic_alignment_bam"
    if suffix in {".fq", ".fastq", ".fq.gz", ".fastq.gz", ".bam", ".cram"}:
        return "raw_long_reads"
    if suffix == ".hic":
        return "hic_reads_registered"
    return "raw_long_reads"


def _compound_suffix(path: Path) -> str:
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if len(suffixes) >= 2 and suffixes[-1] == ".gz":
        return "".join(suffixes[-2:])
    return path.suffix.lower()
