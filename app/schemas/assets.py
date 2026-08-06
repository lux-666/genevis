"""Standard asset contracts for the genome workflow."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifacts import ArtifactRef

AssetType = Literal[
    "raw_long_reads",
    "clean_long_reads",
    "raw_short_dna_reads",
    "clean_short_dna_reads",
    "raw_rnaseq_reads",
    "clean_rnaseq_reads",
    "raw_isoseq_or_long_rna_reads",
    "clean_isoseq_reads",
    "transcript_ccs_reads",
    "reference_proteins",
    "assembly_fasta_registered",
    "draft_assembly",
    "genome_survey_result",
    "hic_reads_registered",
    "hic_alignment_bam",
    "hic_merged_nodups",
    "chromosome_scaffolded_assembly",
]


class AssetRecord(BaseModel):
    """A registered file or result that downstream planners can consume."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    asset_type: AssetType
    path: Path
    sample_id: str | None = None
    platform: str | None = None
    format: str | None = None
    checksum: str | None = None
    checksum_status: Literal["pending", "ready", "skipped"] = "pending"
    size_bytes: int | None = Field(default=None, ge=0)
    verified_at: datetime | None = None
    allowed_stages: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class AssetRegistryDocument(BaseModel):
    """Typed immutable inventory registry."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    assets: list[AssetRecord] = Field(default_factory=list)


class ReadSetMember(BaseModel):
    """One derived read artifact and its source external asset."""

    model_config = ConfigDict(extra="forbid")

    output_ref: ArtifactRef
    source_asset_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    format: str = Field(min_length=1)
    transformation_method: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)


class ReadSetManifest(BaseModel):
    """Immutable one-to-one lineage for a derived read set."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    source_registry_ref: ArtifactRef
    members: list[ReadSetMember] = Field(min_length=1)
