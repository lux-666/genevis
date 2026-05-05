"""Standard asset contracts for the genome workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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
    "chromosome_scaffolded_assembly",
    "repeat_library",
    "repeat_annotation",
    "softmasked_genome",
    "gene_models",
    "predicted_proteins",
    "function_annotation",
]


class AssetRecord(BaseModel):
    """A registered file or result that downstream planners can consume."""

    asset_id: str
    asset_type: AssetType
    path: Path
    sample_id: str | None = None
    platform: str | None = None
    format: str | None = None
    checksum: str | None = None
    checksum_status: Literal["pending", "ready", "skipped"] = "pending"
    allowed_stages: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
