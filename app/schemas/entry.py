"""Structured entry request contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.manifest import Platform
from app.storage.paths import default_data_root
from app.schemas.run_config import RequestableStage

EntryAssetTypeHint = Literal[
    "raw_long_reads",
    "clean_long_reads",
    "raw_short_dna_reads",
    "clean_short_dna_reads",
    "raw_rnaseq_reads",
    "raw_isoseq_or_long_rna_reads",
    "hic_reads_registered",
    "hic_alignment_bam",
    "assembly_fasta_registered",
]


class EntryRequest(BaseModel):
    """Structured request produced before entering the graph."""

    model_config = ConfigDict(extra="forbid")

    command: Literal["run"] = "run"
    session_id: str | None = Field(default=None, min_length=1)
    manifest: str | None = Field(default=None, min_length=1)
    source_paths: list[str] = Field(default_factory=list)
    data_dir: str = Field(default_factory=default_data_root, min_length=1)
    sample_id: str | None = Field(default=None, min_length=1)
    platform: Platform = "unknown"
    asset_type_hint: EntryAssetTypeHint | None = None
    target_assembly_level: Literal["contig", "scaffold", "chromosome"] = "contig"
    genome_size_estimate: int | None = Field(default=None, gt=0)
    requested_stages: list[RequestableStage] | None = None

    @model_validator(mode="after")
    def _validate_entry_source(self):
        if not self.manifest and not self.source_paths:
            raise ValueError("manifest or source_paths is required")
        if self.manifest and self.source_paths:
            raise ValueError("manifest and source_paths are mutually exclusive")
        return self

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError("session_id must not contain leading or trailing whitespace")
        if "/" in value or "\\" in value:
            raise ValueError("session_id must not contain path separators")
        if value in {".", ".."}:
            raise ValueError("session_id must be a stable id, not a path segment")
        return value

    @field_validator("manifest", "data_dir")
    @classmethod
    def _validate_non_blank_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("path value must not be blank")
        return value

    @field_validator("source_paths")
    @classmethod
    def _validate_source_paths(cls, value: list[str]) -> list[str]:
        if any(not path.strip() for path in value):
            raise ValueError("source_paths must not contain blank paths")
        return value

    def to_cli_argv(self) -> list[str]:
        """Convert this request to the existing structured run CLI contract."""

        if not self.manifest:
            raise ValueError("manifest is required before converting to run argv")
        argv = ["run", "--manifest", self.manifest, "--data-dir", self.data_dir]
        if self.session_id is not None:
            argv.extend(["--session-id", self.session_id])
        return argv
