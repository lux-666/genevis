"""Shared request parameters reused across workflow stages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NormalizedReadType = Literal["ont_raw", "ont_hq", "pacbio_clr", "pacbio_hifi"]

PLATFORM_TO_SHARED_READ_TYPE = {
    "cycloneseq": "ont_raw",
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
    "ont_raw": {
        "flye": "nano-raw",
        "nextdenovo": "ont",
        "canu": "nanopore",
        "hifiasm": "not_preferred",
    },
    "ont_hq": {
        "flye": "nano-hq",
        "nextdenovo": "ont",
        "canu": "nanopore",
        "hifiasm": "not_preferred",
    },
    "pacbio_clr": {
        "flye": "pacbio-raw",
        "nextdenovo": "clr",
        "canu": "pacbio",
        "hifiasm": "not_preferred",
    },
    "pacbio_hifi": {
        "flye": "pacbio-hifi",
        "nextdenovo": "hifi",
        "canu": "pacbio-hifi",
        "hifiasm": "hifi",
    },
}


class SharedParameters(BaseModel):
    """Cross-method biological facts collected once by planning."""

    model_config = ConfigDict(extra="forbid")

    read_type: NormalizedReadType | None = None
    estimated_genome_size: int | None = Field(default=None, gt=0)
