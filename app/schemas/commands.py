"""Command schemas for future CLI/API control endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ControlCommand(BaseModel):
    """External lifecycle command."""

    session_id: str
    action: Literal["pause", "resume", "retry", "abort", "review"]
    reason: str | None = None
