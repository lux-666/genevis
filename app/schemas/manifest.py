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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.assets import AssetType

Platform = Literal["cycloneseq", "ont", "clr", "pacbio_hifi", "illumina", "unknown"]
Layout = Literal["single", "paired", "unknown"]
Format = Literal["fastq", "fasta", "bam", "cram", "hic", "assembly", "unknown"]
Compression = Literal["gzip", "bzip2", "xz", "none", "unknown"]

_ACCEPTED_MANIFEST_ASSET_TYPES: set[AssetType] = {
    "raw_long_reads",
    "clean_long_reads",
    "raw_short_dna_reads",
    "clean_short_dna_reads",
    "raw_rnaseq_reads",
    "raw_isoseq_or_long_rna_reads",
    "hic_reads_registered",
    "hic_alignment_bam",
    "hic_merged_nodups",
    "assembly_fasta_registered",
    "reference_genome_fasta",
    "reference_proteins",
}

_NON_EMPTY_INPUT_ASSET_TYPES: frozenset[AssetType] = frozenset(
    _ACCEPTED_MANIFEST_ASSET_TYPES
)
_SCIENTIFIC_NAME_RE = re.compile(r"^[A-Z][A-Za-z-]+(?: [a-z][A-Za-z.-]+)+$")
_NON_SPECIFIC_EPITHETS = frozenset(
    {"aff", "aff.", "cf", "cf.", "sp", "sp.", "spp", "spp."}
)


def normalize_target_species(value: Any) -> str:
    """Return one normalized NCBI Latin scientific name or fail closed."""

    if not isinstance(value, str):
        raise ValueError(
            "target_species must be an NCBI Latin scientific name such as 'Solea ovata'"
        )
    species = " ".join(value.split())
    tokens = species.split()
    if not _SCIENTIFIC_NAME_RE.fullmatch(species) or any(
        token.lower() in _NON_SPECIFIC_EPITHETS for token in tokens[1:]
    ):
        raise ValueError(
            "target_species must be an NCBI Latin scientific name such as "
            "'Solea ovata'; common names, abbreviations, and unspecified "
            "species are not accepted"
        )
    return species


class InputAssetSpec(BaseModel):
    """One or more regular files declared by the user in the input manifest."""

    model_config = ConfigDict(extra="forbid")

    asset_type: AssetType
    paths: list[str] = Field(
        default_factory=list,
        description="Regular file paths; directories and special files are rejected.",
    )
    sample_id: str | None = None
    platform: Platform = "unknown"
    layout: Layout = "unknown"
    format: Format = "unknown"
    compression: Compression = "unknown"
    estimated_pairs: int | None = None
    # Paired reads are represented explicitly at intake time.  ``paths`` is
    # retained as the flattened runtime view for existing inventory adapters;
    # the order is derived from these pairs, never from file names.
    paired_reads: list["PairedReadSpec"] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_asset_type(self) -> InputAssetSpec:
        if self.asset_type not in _ACCEPTED_MANIFEST_ASSET_TYPES:
            raise ValueError(
                f"asset_type must be one of {_ACCEPTED_MANIFEST_ASSET_TYPES}, "
                f"got {self.asset_type!r}"
            )
        if self.paired_reads:
            if self.asset_type != "hic_reads_registered":
                raise ValueError(
                    "paired_reads is currently supported only for hic_reads_registered"
                )
            flattened = [
                path for pair in self.paired_reads for path in (pair.r1, pair.r2)
            ]
            if self.paths and self.paths != flattened:
                raise ValueError("paths must preserve the explicit paired_reads order")
            self.paths = flattened
            if self.layout not in {"unknown", "paired"}:
                raise ValueError("paired_reads requires layout='paired'")
            self.layout = "paired"
        elif not self.paths:
            raise ValueError("non-paired manifest assets require at least one path")
        if any(not path.strip() for path in self.paths):
            raise ValueError("manifest asset paths must not be empty")
        return self


class PairedReadSpec(BaseModel):
    """One explicit R1/R2 pair in a paired-read asset."""

    model_config = {"extra": "forbid"}

    r1: str = Field(min_length=1)
    r2: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_distinct_paths(self) -> PairedReadSpec:
        if self.r1 == self.r2:
            raise ValueError("paired read R1 and R2 must be different paths")
        return self


class InputManifest(BaseModel):
    """User-supplied manifest describing available inputs for a session."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    sample_id: str | None = None
    assets: list[InputAssetSpec] = Field(default_factory=list)
    user_metadata: dict[str, Any] = Field(default_factory=dict)
    genome_size_estimate: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_target_species(self) -> InputManifest:
        raw_species = self.user_metadata.get("target_species")
        if raw_species is None:
            return self
        try:
            species = normalize_target_species(raw_species)
        except ValueError as exc:
            raise ValueError(f"input_manifest.user_metadata.{exc}") from exc
        self.user_metadata["target_species"] = species
        return self

    @model_validator(mode="after")
    def _detect_ambiguous_long_reads(self) -> InputManifest:
        has_long_reads = any(a.asset_type == "raw_long_reads" for a in self.assets)
        has_long_rna = any(
            a.asset_type == "raw_isoseq_or_long_rna_reads" for a in self.assets
        )
        if has_long_reads and has_long_rna:
            for asset in self.assets:
                if asset.asset_type in (
                    "raw_long_reads",
                    "raw_isoseq_or_long_rna_reads",
                ):
                    if asset.platform == "unknown":
                        asset.metadata.setdefault("ambiguous", True)
        return self

    @model_validator(mode="after")
    def _reject_duplicate_declared_paths(self) -> InputManifest:
        seen: dict[str, tuple[int, int]] = {}
        for entry_ordinal, asset in enumerate(self.assets):
            for path_ordinal, declared in enumerate(asset.paths):
                canonical = str(Path(declared).expanduser().resolve(strict=False))
                previous = seen.get(canonical)
                if previous is not None:
                    raise ValueError(
                        "input manifest declares the same canonical path more "
                        "than once: "
                        f"{canonical} at assets[{previous[0]}].paths[{previous[1]}] "
                        f"and assets[{entry_ordinal}].paths[{path_ordinal}]"
                    )
                seen[canonical] = (entry_ordinal, path_ordinal)
        return self

    @model_validator(mode="after")
    def _require_single_reference_genome(self) -> InputManifest:
        references = [
            asset
            for asset in self.assets
            if asset.asset_type == "reference_genome_fasta"
        ]
        if len(references) > 1:
            raise ValueError(
                "input manifest accepts at most one reference_genome_fasta asset"
            )
        if references and len(references[0].paths) != 1:
            raise ValueError(
                "reference_genome_fasta must declare exactly one FASTA path"
            )
        return self


PlatformResolutionSource = Literal["manifest", "filename_or_metadata", "unknown"]


class AssetPathInference(BaseModel):
    """Per-file facts inferred for one manifest path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path_ordinal: int = Field(ge=0)
    declared_path: str
    canonical_path: str
    exists: bool
    is_regular_file: bool
    size_bytes: int | None = Field(default=None, ge=0)
    compression: Compression
    format: Format
    platform: Platform
    platform_resolution_source: PlatformResolutionSource


class AssetEntryInference(BaseModel):
    """Ordered inference facts bound to one manifest entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_entry_ordinal: int = Field(ge=0)
    asset_type: AssetType
    inferred_layout: Layout
    path_facts: list[AssetPathInference]


def _detect_compression(path: Path) -> Compression:
    suffixes = [s.lower() for s in path.suffixes]
    if ".gz" in suffixes or ".gzip" in suffixes:
        return "gzip"
    if ".bz2" in suffixes or ".bzip2" in suffixes:
        return "bzip2"
    if ".xz" in suffixes:
        return "xz"
    return "none"


def _resolve_compression(path: Path, declared: Compression) -> Compression:
    """Resolve compression with the manifest declaration taking precedence.

    An explicit declaration is needed for valid compressed inputs whose names
    have no compression suffix.  A suffix is therefore only a contradiction
    when it positively identifies a *different* codec; an absent suffix is not
    evidence for ``none``.
    """

    suffix_compression = _detect_compression(path)
    if declared == "unknown":
        return suffix_compression
    if suffix_compression != "none" and suffix_compression != declared:
        raise ValueError(
            "declared compression conflicts with filename suffix: "
            f"declared {declared!r}, suffix implies {suffix_compression!r}"
        )
    return declared


def _detect_format(path: Path) -> Format:
    name = path.name.lower()
    for compression_suffix in (".gzip", ".bzip2", ".gz", ".bz2", ".xz"):
        if name.endswith(compression_suffix):
            name = name[: -len(compression_suffix)]
            break
    if name.endswith((".fastq", ".fq")):
        return "fastq"
    if name.endswith((".fasta", ".fa", ".fna", ".faa")):
        return "fasta"
    if name.endswith(".bam"):
        return "bam"
    if name.endswith(".cram"):
        return "cram"
    if ".hic" in name:
        return "hic"
    if name.endswith(".assembly"):
        return "assembly"
    return "unknown"


def _resolve_format(path: Path, declared: Format) -> Format:
    """Resolve format from an authoritative declaration or a compatible suffix."""

    suffix_format = _detect_format(path)
    if declared == "unknown":
        return suffix_format
    if suffix_format != "unknown" and suffix_format != declared:
        raise ValueError(
            "declared format conflicts with filename suffix: "
            f"declared {declared!r}, suffix implies {suffix_format!r}"
        )
    return declared


def _metadata_indicates_cycloneseq(metadata: dict[str, Any]) -> bool:
    """Return whether user metadata explicitly names BGI CycloneSEQ."""
    values = [
        str(value).lower()
        for value in metadata.values()
        if isinstance(value, (str, int, float))
    ]
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


def _infer_asset_properties(
    spec: InputAssetSpec,
    *,
    manifest_entry_ordinal: int,
) -> AssetEntryInference:
    """Infer lightweight properties from file paths.

    Does not read file contents except for existence and size.
    """
    paths = [Path(p).expanduser() for p in spec.paths]
    path_facts: list[AssetPathInference] = []
    for path_ordinal, path in enumerate(paths):
        exists = path.exists()
        is_regular_file = path.is_file()
        platform = _detect_platform(path, spec.platform, spec.metadata)
        if spec.platform != "unknown":
            platform_source: PlatformResolutionSource = "manifest"
        elif platform != "unknown":
            platform_source = "filename_or_metadata"
        else:
            platform_source = "unknown"
        path_facts.append(
            AssetPathInference(
                path_ordinal=path_ordinal,
                declared_path=spec.paths[path_ordinal],
                canonical_path=str(path.resolve(strict=False)),
                exists=exists,
                is_regular_file=is_regular_file,
                size_bytes=path.stat().st_size if is_regular_file else None,
                compression=_resolve_compression(path, spec.compression),
                format=_resolve_format(path, spec.format),
                platform=platform,
                platform_resolution_source=platform_source,
            )
        )
    return AssetEntryInference(
        manifest_entry_ordinal=manifest_entry_ordinal,
        asset_type=spec.asset_type,
        inferred_layout=(
            spec.layout if spec.layout != "unknown" else _detect_layout(paths)
        ),
        path_facts=path_facts,
    )


def _logical_non_empty_error(fact: AssetPathInference) -> str | None:
    """Return a validation error when a regular input has no readable content.

    Compressed files may have a positive container size while representing an
    empty stream (or while being corrupt).  Probe exactly one decompressed
    byte here, before freezing any control inputs; this deliberately does not
    parse or otherwise consume the declared biological file.
    """

    path = Path(fact.canonical_path)
    if fact.compression == "none":
        return (
            "sequence/assembly inputs must not be empty"
            if fact.size_bytes == 0
            else None
        )
    try:
        if fact.compression == "gzip":
            with gzip.open(path, "rb") as handle:
                non_empty = bool(handle.read(1))
        else:
            return None
    except (OSError, EOFError) as exc:
        return f"compressed input is unreadable ({exc.__class__.__name__})"
    if non_empty:
        return None
    return "sequence/assembly inputs must not be logically empty"


def validate_manifest(
    manifest: InputManifest, *, require_existing_files: bool = True
) -> dict[str, Any]:
    """Validate a manifest and return a report.

    Returns a dict with:
    - valid: bool
    - errors: list[str]
    - warnings: list[str]
    - inferred: ordered list bound to manifest entry and path ordinals
    """
    errors: list[str] = []
    warnings: list[str] = []
    inferred: list[dict[str, Any]] = []

    if not manifest.assets:
        errors.append("manifest must declare at least one asset")

    entry_inferences: list[AssetEntryInference] = []
    for entry_ordinal, spec in enumerate(manifest.assets):
        try:
            entry = _infer_asset_properties(
                spec,
                manifest_entry_ordinal=entry_ordinal,
            )
        except ValueError as exc:
            errors.append(f"{spec.asset_type}: {exc}")
            continue
        entry_inferences.append(entry)
        inferred.append(entry.model_dump(mode="json"))
        missing = [fact.declared_path for fact in entry.path_facts if not fact.exists]
        if require_existing_files and missing:
            errors.append(f"{spec.asset_type}: missing files {missing}")
        non_regular = [
            fact.declared_path
            for fact in entry.path_facts
            if fact.exists and not fact.is_regular_file
        ]
        if non_regular:
            errors.append(
                f"{spec.asset_type}: manifest assets must be regular files: "
                f"{non_regular}"
            )

        if spec.asset_type in _NON_EMPTY_INPUT_ASSET_TYPES:
            unsupported_compression = [
                fact.declared_path
                for fact in entry.path_facts
                if fact.compression in {"bzip2", "xz"}
            ]
            if unsupported_compression:
                errors.append(
                    f"{spec.asset_type}: executable workflows support only gzip "
                    "or uncompressed inputs; unsupported compression for "
                    f"{unsupported_compression}"
                )
            non_empty_errors = {
                fact.declared_path: _logical_non_empty_error(fact)
                for fact in entry.path_facts
                if fact.exists
                and fact.is_regular_file
                and fact.compression not in {"bzip2", "xz"}
            }
            invalid_inputs = {
                path: reason
                for path, reason in non_empty_errors.items()
                if reason is not None
            }
            if invalid_inputs:
                errors.append(
                    f"{spec.asset_type}: invalid sequence/assembly inputs: "
                    f"{invalid_inputs}"
                )

        unknown_formats = [
            fact.declared_path
            for fact in entry.path_facts
            if fact.exists and fact.format == "unknown"
        ]
        if unknown_formats:
            warnings.append(
                f"{spec.asset_type}: could not infer format from {unknown_formats}"
            )

        if spec.asset_type == "raw_long_reads" and any(
            fact.platform == "unknown" for fact in entry.path_facts
        ):
            warnings.append(f"{spec.asset_type}: platform unknown for {spec.paths}")

        if spec.asset_type == "reference_genome_fasta":
            invalid_formats = [
                fact.declared_path
                for fact in entry.path_facts
                if fact.exists and fact.format != "fasta"
            ]
            if invalid_formats:
                errors.append(
                    "reference_genome_fasta: reference genome must be FASTA: "
                    f"{invalid_formats}"
                )

        if spec.metadata.get("ambiguous"):
            warnings.append(
                f"{spec.asset_type}: ambiguous classification against "
                "raw_isoseq_or_long_rna_reads; human review required"
            )

    long_read_entries = [
        entry
        for entry in entry_inferences
        if entry.asset_type in {"raw_long_reads", "clean_long_reads"}
    ]
    for entry in long_read_entries:
        platforms = {fact.platform for fact in entry.path_facts}
        if len(platforms) > 1:
            errors.append(
                f"{entry.asset_type}: one manifest entry resolves to conflicting "
                f"platforms {sorted(platforms)}; split platforms into separate entries"
            )
    if len(long_read_entries) > 1:
        unresolved = [
            entry.manifest_entry_ordinal
            for entry in long_read_entries
            if any(fact.platform == "unknown" for fact in entry.path_facts)
        ]
        if unresolved:
            errors.append(
                "mixed_long_read_platform_unresolved: explicit per-entry platform "
                f"required for manifest assets {unresolved}"
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "inferred": inferred,
    }
