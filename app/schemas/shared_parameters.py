"""Shared request parameters reused across workflow stages."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domains.assembly.selection import NormalizedReadType
from app.schemas.manifest import Platform


def normalize_dna_motif(value: str, *, field_name: str = "telomere_motif") -> str:
    """Normalize one simple DNA repeat motif used by sequence scanners."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a DNA motif")
    motif = value.strip().upper()
    if len(motif) < 2 or any(base not in "ACGT" for base in motif):
        raise ValueError(f"{field_name} must be a DNA motif of at least two bases")
    return motif


class SpeciesFacts(BaseModel):
    """Caller-supplied biological identity used by parameter recommenders."""

    model_config = ConfigDict(extra="forbid")

    scientific_name: str
    taxon_id: int | None = Field(default=None, gt=0)
    clade: str | None = None
    ploidy: int | None = Field(default=None, gt=0)
    expected_chromosome_count: int | None = Field(default=None, gt=0)
    telomere_motif: str | None = None

    @field_validator("scientific_name", "clade")
    @classmethod
    def _non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @field_validator("telomere_motif")
    @classmethod
    def _dna_motif(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_dna_motif(value)


class SharedParameters(BaseModel):
    """Cross-method biological facts collected once by planning."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform | None = None
    read_type: NormalizedReadType | None = None
    estimated_genome_size: int | None = Field(default=None, gt=0)
    species_facts: SpeciesFacts | None = None
