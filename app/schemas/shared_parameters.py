"""Shared request parameters reused across workflow stages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.manifest import Platform

NormalizedReadType = Literal["cyclone", "ont_raw", "ont_hq", "pacbio_clr", "pacbio_hifi"]

PLATFORM_TO_SHARED_READ_TYPE = {
    "cyclone": "cyclone",
    "cycloneseq": "cyclone",
    "ont": "ont_raw",
    "clr": "pacbio_clr",
    "pacbio_hifi": "pacbio_hifi",
}

FLYE_TO_SHARED_READ_TYPE = {
    "nano-raw": "ont_raw",
    "nano-hq": "ont_hq",
    "pacbio-raw": "pacbio_clr",
    "pacbio-hifi": "pacbio_hifi",
}

SHARED_TO_METHOD_READ_TYPE = {
    "cyclone": {
        "flye": "nano-raw",
        "nextdenovo": "ont",
        "hifiasm_ont": "ont",
    },
    "ont_raw": {
        "flye": "nano-raw",
        "nextdenovo": "ont",
        "hifiasm_ont": "ont",
    },
    "ont_hq": {
        "flye": "nano-hq",
        "nextdenovo": "ont",
        "hifiasm_ont": "ont",
    },
    "pacbio_clr": {
        "flye": "pacbio-raw",
        "nextdenovo": "clr",
        "hifiasm": "not_preferred",
    },
    "pacbio_hifi": {
        "flye": "pacbio-hifi",
        "nextdenovo": "hifi",
        "hifiasm": "hifi",
    },
}


class SharedParameters(BaseModel):
    """Cross-method biological facts collected once by planning."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform | None = None
    read_type: NormalizedReadType | None = None
    estimated_genome_size: int | None = Field(default=None, gt=0)
