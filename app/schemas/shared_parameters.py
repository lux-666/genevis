"""Shared request parameters reused across workflow stages."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domains.assembly.selection import NormalizedReadType
from app.schemas.manifest import Platform


class SharedParameters(BaseModel):
    """Cross-method biological facts collected once by planning."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform | None = None
    read_type: NormalizedReadType | None = None
    estimated_genome_size: int | None = Field(default=None, gt=0)
