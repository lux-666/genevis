"""Internal assembly selector contract and normalization helpers."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domains.assembly.constants import (
    FLYE_METHOD,
    HIFIASM_METHOD,
    HIFIASM_ONT_METHOD,
    NEXTDENOVO_METHOD,
)

NormalizedReadType = Literal[
    "cyclone",
    "ont_raw",
    "ont_hq",
    "pacbio_clr",
    "pacbio_hifi",
]

SelectedAssemblyRoute = Literal["cyclone", "ont", "clr", "hifi"]
RecommendedAssemblyRoute = Literal[SelectedAssemblyRoute, "insufficient_data"]

PLATFORM_TO_SHARED_READ_TYPE: dict[str, NormalizedReadType] = {
    "cyclone": "cyclone",
    "cycloneseq": "cyclone",
    "ont": "ont_raw",
    "clr": "pacbio_clr",
    "pacbio_hifi": "pacbio_hifi",
}

PLATFORMS_BY_SHARED_READ_TYPE: dict[NormalizedReadType, frozenset[str]] = {
    "cyclone": frozenset({"cycloneseq"}),
    "ont_raw": frozenset({"ont"}),
    "ont_hq": frozenset({"ont"}),
    "pacbio_clr": frozenset({"clr"}),
    "pacbio_hifi": frozenset({"pacbio_hifi"}),
}

FLYE_TO_SHARED_READ_TYPE: dict[str, NormalizedReadType] = {
    "nano-raw": "ont_raw",
    "nano-hq": "ont_hq",
    "pacbio-raw": "pacbio_clr",
    "pacbio-hifi": "pacbio_hifi",
}

SHARED_TO_METHOD_READ_TYPE: dict[NormalizedReadType, dict[str, str]] = {
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

AssemblySelectionSource = Literal["run_config", "recommendation", "human", "timeout"]

METHOD_ALIASES = {
    "flye": FLYE_METHOD,
    "nextdenovo": NEXTDENOVO_METHOD,
    "hifiasm": HIFIASM_METHOD,
    "hifiasm_ont": HIFIASM_ONT_METHOD,
    FLYE_METHOD: FLYE_METHOD,
    NEXTDENOVO_METHOD: NEXTDENOVO_METHOD,
    HIFIASM_METHOD: HIFIASM_METHOD,
    HIFIASM_ONT_METHOD: HIFIASM_ONT_METHOD,
}

ROUTE_TO_READ_TYPE: dict[SelectedAssemblyRoute, NormalizedReadType] = {
    "cyclone": "cyclone",
    "ont": "ont_raw",
    "hifi": "pacbio_hifi",
    "clr": "pacbio_clr",
}

DEFAULT_PRIMARY_BY_READ_TYPE: dict[NormalizedReadType, str] = {
    "cyclone": HIFIASM_ONT_METHOD,
    "ont_raw": HIFIASM_ONT_METHOD,
    "ont_hq": HIFIASM_ONT_METHOD,
    "pacbio_clr": FLYE_METHOD,
    "pacbio_hifi": HIFIASM_METHOD,
}

logger = logging.getLogger(__name__)


class AssemblySelection(BaseModel):
    """Single internal assembly decision consumed by downstream planning."""

    model_config = ConfigDict(extra="forbid")

    read_type: NormalizedReadType
    primary_method: str
    source: AssemblySelectionSource
    rationale: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_methods(self) -> "AssemblySelection":
        self.primary_method = normalize_method_id(self.primary_method)
        if not is_method_read_type_compatible(self.primary_method, self.read_type):
            raise ValueError(
                "assembly_selection primary_method "
                f"{self.primary_method!r} is incompatible "
                f"with read_type {self.read_type!r}"
            )
        return self


def validate_assembly_selection(value: object) -> AssemblySelection:
    """Validate one strict single-method assembly selection."""

    return AssemblySelection.model_validate(value)


def normalize_method_id(value: str) -> str:
    method = METHOD_ALIASES.get(str(value))
    if not method:
        raise ValueError(f"unknown assembly method: {value}")
    return method


def normalize_read_type(
    value: Any, *, platform: Any = None
) -> NormalizedReadType | None:
    if value in SHARED_TO_METHOD_READ_TYPE:
        return cast(NormalizedReadType, value)
    if value in ROUTE_TO_READ_TYPE:
        return ROUTE_TO_READ_TYPE[cast(SelectedAssemblyRoute, value)]
    if value in FLYE_TO_SHARED_READ_TYPE:
        return FLYE_TO_SHARED_READ_TYPE[str(value)]
    if value in {"clr", "pacbio_clr"}:
        return "pacbio_clr"
    if platform:
        return PLATFORM_TO_SHARED_READ_TYPE.get(str(platform))
    return None


def resolve_read_type(
    *candidates: tuple[str, Any] | tuple[str, Any, Any],
    context: str | None = None,
) -> NormalizedReadType | None:
    """Resolve the first valid shared read type from ordered named candidates."""

    read_type, _source = resolve_read_type_with_source(*candidates, context=context)
    return read_type


def resolve_read_type_with_source(
    *candidates: tuple[str, Any] | tuple[str, Any, Any],
    context: str | None = None,
) -> tuple[NormalizedReadType | None, str | None]:
    """Resolve the first valid shared read type and return its source label."""

    for candidate in candidates:
        source, raw_value, platform = _read_type_candidate_parts(candidate)
        if raw_value is None and platform is None:
            continue
        read_type = normalize_read_type(raw_value, platform=platform)
        if read_type is None:
            logger.debug(
                "ignored unrecognized assembly read_type candidate",
                extra={
                    "context": context,
                    "source": source,
                    "value": raw_value,
                    "platform": platform,
                },
            )
            continue
        logger.debug(
            "resolved assembly read_type candidate",
            extra={
                "context": context,
                "source": source,
                "value": raw_value,
                "platform": platform,
                "read_type": read_type,
            },
        )
        return read_type, source
    return None, None


def route_from_read_type(read_type: str) -> SelectedAssemblyRoute:
    if read_type == "pacbio_hifi":
        return "hifi"
    if read_type in {"ont_raw", "ont_hq"}:
        return "ont"
    if read_type == "pacbio_clr":
        return "clr"
    if read_type == "cyclone":
        return "cyclone"
    raise ValueError(f"unknown assembly read_type: {read_type}")


def _read_type_candidate_parts(
    candidate: tuple[str, Any] | tuple[str, Any, Any],
) -> tuple[str, Any, Any]:
    if len(candidate) == 2:
        source, raw_value = candidate
        return source, raw_value, None
    if len(candidate) == 3:
        source, raw_value, platform = candidate
        return source, raw_value, platform
    raise ValueError(
        "read-type candidates must be (source, value) or (source, value, platform)"
    )


def method_read_type(method: str, read_type: NormalizedReadType) -> str | None:
    method_id = normalize_method_id(method)
    method_key = method_id.rsplit(".", 1)[-1]
    value = SHARED_TO_METHOD_READ_TYPE.get(read_type, {}).get(method_key)
    if value == "not_preferred":
        return None
    return value


def is_method_read_type_compatible(method: str, read_type: NormalizedReadType) -> bool:
    return method_read_type(method, read_type) is not None


def compatible_methods(read_type: NormalizedReadType) -> list[str]:
    return [
        method
        for method in (
            FLYE_METHOD,
            NEXTDENOVO_METHOD,
            HIFIASM_METHOD,
            HIFIASM_ONT_METHOD,
        )
        if is_method_read_type_compatible(method, read_type)
    ]


def default_primary_method(read_type: NormalizedReadType) -> str:
    return DEFAULT_PRIMARY_BY_READ_TYPE[read_type]
