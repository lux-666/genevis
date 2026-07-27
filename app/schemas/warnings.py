"""Structured warnings with stage metadata."""

from __future__ import annotations

from pydantic import BaseModel


class PlanWarning(BaseModel):
    """Structured warning with routing metadata."""

    message: str
    stage: str  # explicit stage association
    category: str = "general"  # e.g., "parameter", "dependency", "inference"

    def __str__(self) -> str:
        return self.message
