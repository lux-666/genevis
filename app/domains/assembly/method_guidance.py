"""Validated, model-facing mechanism guidance for assembly parameters.

Guidance records explain relationships that an LLM may consider.  They do not
implement triggers, choose values, or make a recommendation on their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_GUIDANCE_PATH: Final = (
    _PROJECT_ROOT
    / "descriptors"
    / "knowledge_packs"
    / "assembly"
    / "parameter_recommender"
    / "guidance.yaml"
)
KNOWN_GUIDANCE_METHODS: Final = frozenset({"hifiasm", "hifiasm_ont"})


class MethodGuidance(BaseModel):
    """One non-executable mechanism card available to the recommender."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guidance_id: str = Field(min_length=1)
    methods: tuple[str, ...] = Field(min_length=1)
    parameters: tuple[str, ...] = Field(min_length=1)
    relevant_metrics: tuple[str, ...] = Field(min_length=1)
    guidance: str = Field(min_length=1)
    caveats: tuple[str, ...] = ()

    @field_validator("guidance_id", "guidance")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("methods", "parameters", "relevant_metrics", "caveats")
    @classmethod
    def _unique_nonblank_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError("items must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("items must be unique")
        return cleaned

    @field_validator("methods")
    @classmethod
    def _known_methods(cls, methods: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(methods) - KNOWN_GUIDANCE_METHODS)
        if unknown:
            raise ValueError(f"unknown method id(s): {', '.join(unknown)}")
        return methods

    def model_card(self) -> dict[str, Any]:
        """Return the stable JSON-shaped card shown to the model."""

        return self.model_dump(mode="json")


class _GuidanceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    guidance: tuple[MethodGuidance, ...] = Field(min_length=1)


@dataclass(frozen=True)
class MethodGuidanceRegistry:
    """Immutable guidance collection with deterministic model projections."""

    records: tuple[MethodGuidance, ...]
    by_id: Mapping[str, MethodGuidance]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_GUIDANCE_PATH) -> "MethodGuidanceRegistry":
        """Load and validate a guidance document."""

        guidance_path = Path(path)
        if not guidance_path.exists():
            raise FileNotFoundError(f"Method guidance not found: {guidance_path}")
        raw: Any = yaml.safe_load(guidance_path.read_text(encoding="utf-8")) or {}
        document = _GuidanceDocument.model_validate(raw)
        records = tuple(sorted(document.guidance, key=lambda record: record.guidance_id))
        ids = [record.guidance_id for record in records]
        duplicates = sorted({guidance_id for guidance_id in ids if ids.count(guidance_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate guidance_id(s): {', '.join(duplicates)}")
        return cls(
            records=records,
            by_id=MappingProxyType({record.guidance_id: record for record in records}),
        )

    def model_cards_for_method(self, method_id: str) -> list[dict[str, Any]]:
        """Return guidance cards for one method, ordered by stable ID."""

        _validate_method_id(method_id)
        return [record.model_card() for record in self.records if method_id in record.methods]

    def require(self, guidance_id: str) -> MethodGuidance:
        """Resolve a cited ID or fail closed."""

        try:
            return self.by_id[guidance_id]
        except KeyError as exc:
            raise ValueError(f"Unknown guidance_id: {guidance_id}") from exc


def load_method_guidance(path: str | Path = DEFAULT_GUIDANCE_PATH) -> MethodGuidanceRegistry:
    """Load the structured method-guidance registry."""

    return MethodGuidanceRegistry.load(path)


def _validate_method_id(method_id: str) -> None:
    if method_id not in KNOWN_GUIDANCE_METHODS:
        raise ValueError(f"Unknown method id: {method_id}")
