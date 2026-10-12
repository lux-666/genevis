"""Public Assembly input-role contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HifiasmInputConfig(BaseModel):
    """Explicit companion and trio roles for a HiFi Hifiasm run."""

    model_config = ConfigDict(extra="forbid")

    ultralong_ont_reads_ref: list[str] | None = Field(default=None, min_length=1)
    hic_reads_ref: list[str] | None = Field(default=None, min_length=1)
    trio_maternal_reads_ref: list[str] | None = Field(default=None, min_length=1)
    trio_paternal_reads_ref: list[str] | None = Field(default=None, min_length=1)

    @field_validator(
        "ultralong_ont_reads_ref",
        "hic_reads_ref",
        "trio_maternal_reads_ref",
        "trio_paternal_reads_ref",
        mode="before",
    )
    @classmethod
    def _normalize_refs(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        return value

    @field_validator(
        "ultralong_ont_reads_ref",
        "hic_reads_ref",
        "trio_maternal_reads_ref",
        "trio_paternal_reads_ref",
    )
    @classmethod
    def _validate_refs(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [path.strip() for path in value]
        if any(not path for path in normalized):
            raise ValueError("Assembly input references must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Assembly input references must be unique")
        return normalized

    @model_validator(mode="after")
    def _require_complete_trio(self) -> "HifiasmInputConfig":
        maternal = self.trio_maternal_reads_ref
        paternal = self.trio_paternal_reads_ref
        if (maternal is None) != (paternal is None):
            raise ValueError(
                "hifiasm_inputs requires both trio_maternal_reads_ref and "
                "trio_paternal_reads_ref"
            )
        if maternal is not None and paternal is not None:
            if set(maternal).intersection(paternal):
                raise ValueError("trio parent read references must be different")
            if self.hic_reads_ref is not None:
                raise ValueError(
                    "Select one Assembly phasing mode: Trio or Hi-C. "
                    "Hi-C scaffolding can follow the Trio assembly as its own stage."
                )
        return self

    @property
    def trio_enabled(self) -> bool:
        return self.trio_maternal_reads_ref is not None
