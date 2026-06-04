"""Input manifest schema and validation for the inventory stage.

The manifest is the user's declaration of what files are available for a
session.  Inventory reads it, validates lightweight metadata, and registers
assets without transforming the files.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.assets import AssetType

Platform = Literal["cycloneseq", "ont", "clr", "pacbio_hifi", "illumina", "unknown"]
Layout = Literal["single", "paired", "unknown"]
Format = Literal["fastq", "fasta", "bam", "cram", "hic", "assembly", "gff3", "gff", "gtf", "unknown"]
Compression = Literal["gzip", "bzip2", "xz", "none", "unknown"]

_ACCEPTED_MANIFEST_ASSET_TYPES: set[AssetType] = {
    "raw_long_reads",
    "raw_short_dna_reads",
    "raw_rnaseq_reads",
    "raw_isoseq_or_long_rna_reads",
    "hic_reads_registered",
    "hic_alignment_bam",
    "assembly_fasta_registered",
    "repeat_library",
    "reference_proteins",
    "gene_models",
    "predicted_proteins",
    "transcripts_fasta",
}


class InputAssetSpec(BaseModel):
    """One file or directory declared by the user in the input manifest."""

    asset_type: AssetType
    paths: list[str] = Field(default_factory=list)
    sample_id: str | None = None
    platform: Platform = "unknown"
    layout: Layout = "unknown"
    format: Format = "unknown"
    compression: Compression = "unknown"
    estimated_pairs: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_asset_type(self):
        if self.asset_type not in _ACCEPTED_MANIFEST_ASSET_TYPES:
            raise ValueError(f"asset_type must be one of {_ACCEPTED_MANIFEST_ASSET_TYPES}, got {self.asset_type!r}")
        return self


class InputManifest(BaseModel):
    """User-supplied manifest describing available inputs for a session."""

    session_id: str
    sample_id: str | None = None
    assets: list[InputAssetSpec] = Field(default_factory=list)
    user_metadata: dict[str, Any] = Field(default_factory=dict)
    target_assembly_level: Literal["contig", "scaffold", "chromosome"] = "contig"
    genome_size_estimate: int | None = None

    @model_validator(mode="after")
    def _detect_ambiguous_long_reads(self):
        has_long_reads = any(a.asset_type == "raw_long_reads" for a in self.assets)
        has_long_rna = any(a.asset_type == "raw_isoseq_or_long_rna_reads" for a in self.assets)
        if has_long_reads and has_long_rna:
            for asset in self.assets:
                if asset.asset_type in ("raw_long_reads", "raw_isoseq_or_long_rna_reads"):
                    if asset.platform == "unknown":
                        asset.metadata.setdefault("ambiguous", True)
        return self


def _detect_compression(path: Path) -> Compression:
    suffixes = [s.lower() for s in path.suffixes]
    if ".gz" in suffixes or ".gzip" in suffixes:
        return "gzip"
    if ".bz2" in suffixes or ".bzip2" in suffixes:
        return "bzip2"
    if ".xz" in suffixes:
        return "xz"
    return "none"


def _detect_format(path: Path) -> Format:
    name = path.name.lower()
    if name.endswith(".fastq") or name.endswith(".fq") or name.endswith(".fastq.gz") or name.endswith(".fq.gz"):
        return "fastq"
    if (
        name.endswith(".fasta")
        or name.endswith(".fa")
        or name.endswith(".fna")
        or name.endswith(".faa")
        or name.endswith(".fasta.gz")
        or name.endswith(".fa.gz")
        or name.endswith(".fna.gz")
        or name.endswith(".faa.gz")
    ):
        return "fasta"
    if name.endswith(".gff3") or name.endswith(".gff3.gz"):
        return "gff3"
    if name.endswith(".gff") or name.endswith(".gff.gz"):
        return "gff"
    if name.endswith(".gtf") or name.endswith(".gtf.gz"):
        return "gtf"
    if name.endswith(".bam"):
        return "bam"
    if name.endswith(".cram"):
        return "cram"
    if ".hic" in name:
        return "hic"
    if name.endswith(".assembly"):
        return "assembly"
    return "unknown"


def _metadata_indicates_cycloneseq(metadata: dict[str, Any]) -> bool:
    """Return whether user metadata explicitly names BGI CycloneSEQ."""
    values = [str(value).lower() for value in metadata.values() if isinstance(value, (str, int, float))]
    return any("cycloneseq" in value or "cyclone seq" in value for value in values)


def _detect_platform(
    path: Path,
    declared: Platform,
    metadata: dict[str, Any] | None = None,
) -> Platform:
    if declared != "unknown":
        return declared
    name = path.name.lower()
    if "cycloneseq" in name or "cyclone_seq" in name or "cyclone-seq" in name:
        return "cycloneseq"
    if metadata and _metadata_indicates_cycloneseq(metadata):
        return "cycloneseq"
    if "ont" in name or "nanopore" in name or "minion" in name:
        return "ont"
    if "hifi" in name or "ccs" in name:
        return "pacbio_hifi"
    if "clr" in name or "pacbio" in name or "sequel" in name:
        return "clr"
    if "illumina" in name or "nova" in name or "hiseq" in name:
        return "illumina"
    return "unknown"


def _detect_layout(paths: list[Path]) -> Layout:
    if len(paths) >= 2:
        names = [p.name for p in paths]
        r1 = any(re.search(r"(^|[_\-.])(R1|1)([_\-.]|$)", n) for n in names)
        r2 = any(re.search(r"(^|[_\-.])(R2|2)([_\-.]|$)", n) for n in names)
        if r1 and r2:
            return "paired"
    return "single"


def _infer_asset_properties(spec: InputAssetSpec) -> dict[str, Any]:
    """Infer lightweight properties from file paths.

    Does not read file contents except for existence and size.
    """
    paths = [Path(p) for p in spec.paths]
    props: dict[str, Any] = {
        "paths_exist": [],
        "paths_missing": [],
        "sizes_bytes": {},
        "detected_compression": {},
        "detected_format": {},
    }
    for p in paths:
        if p.exists():
            props["paths_exist"].append(str(p))
            props["sizes_bytes"][str(p)] = p.stat().st_size
            props["detected_compression"][str(p)] = _detect_compression(p)
            props["detected_format"][str(p)] = _detect_format(p)
        else:
            props["paths_missing"].append(str(p))

    if paths:
        first = paths[0]
        props["inferred_compression"] = _detect_compression(first)
        props["inferred_format"] = _detect_format(first)
        props["inferred_platform"] = _detect_platform(first, spec.platform, spec.metadata)
        props["inferred_layout"] = _detect_layout(paths)
    return props


def validate_manifest(manifest: InputManifest, *, require_existing_files: bool = True) -> dict[str, Any]:
    """Validate a manifest and return a report.

    Returns a dict with:
    - valid: bool
    - errors: list[str]
    - warnings: list[str]
    - inferred: dict mapping asset_type to inferred properties
    """
    errors: list[str] = []
    warnings: list[str] = []
    inferred: dict[str, Any] = {}

    if not manifest.assets:
        errors.append("manifest must declare at least one asset")

    _adoption_types = {"gene_models", "predicted_proteins", "transcripts_fasta"}
    seen_adoption: dict[str, int] = {}
    for spec in manifest.assets:
        if spec.asset_type in _adoption_types:
            seen_adoption[spec.asset_type] = seen_adoption.get(spec.asset_type, 0) + 1
    for atype, count in seen_adoption.items():
        if count > 1:
            errors.append(f"{atype}: adoption assets must appear exactly once, got {count} entries")

    for spec in manifest.assets:
        props = _infer_asset_properties(spec)
        inferred[spec.asset_type] = props

        if require_existing_files and props["paths_missing"]:
            errors.append(f"{spec.asset_type}: missing files {props['paths_missing']}")

        if props["paths_exist"] and props["inferred_format"] == "unknown":
            warnings.append(f"{spec.asset_type}: could not infer format from {spec.paths}")

        inferred_format = props.get("inferred_format")
        if spec.asset_type == "gene_models" and inferred_format not in {"gff3", "gff", "gtf"}:
            errors.append(f"{spec.asset_type}: expected GFF/GFF3/GTF, got {inferred_format}")
        if spec.asset_type in {"predicted_proteins", "transcripts_fasta"} and inferred_format != "fasta":
            errors.append(f"{spec.asset_type}: expected FASTA, got {inferred_format}")
        if spec.asset_type in {"gene_models", "predicted_proteins", "transcripts_fasta"} and len(spec.paths) > 1:
            errors.append(f"{spec.asset_type}: adoption assets must have exactly one path, got {len(spec.paths)}")

        if spec.asset_type == "raw_long_reads" and props["inferred_platform"] == "unknown":
            warnings.append(f"{spec.asset_type}: platform unknown for {spec.paths}")

        if spec.metadata.get("ambiguous"):
            warnings.append(
                f"{spec.asset_type}: ambiguous classification against "
                "raw_isoseq_or_long_rna_reads; human review required"
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "inferred": inferred,
    }
