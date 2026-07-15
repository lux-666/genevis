"""Shared selection result contracts for knowledge retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SelectionResult:
    """Selected knowledge records plus audit metadata."""

    selected_cases: list[dict[str, Any]]
    metadata: dict[str, Any]
    trace: list[dict[str, Any]] = field(default_factory=list)


class CaseSelector(Protocol):
    def select(
        self,
        cases: list[dict[str, Any]],
        *,
        method_id: str,
        task: str,
        prompt_profile: dict[str, Any],
    ) -> SelectionResult:
        """Select a prompt-sized evidence subset."""
