"""Execution result data models.

Direct outcomes of running a frozen attempt, shared by the local runner and
the method-runner helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


ExecutionFailureStatus = Literal["failed", "blocked_env", "missing_tool"]
ExecutionStatus = Literal["ok", "failed", "blocked_env", "missing_tool"]


class ExecutionResult(BaseModel):
    """Direct process result from a frozen attempt.

    `status` distinguishes real process outcomes (ok / failed) from structured
    preflight block states (blocked_env / missing_tool) so callers can emit the
    right ledger and state vocabulary without guessing from return codes.
    """

    attempt_id: str
    success: bool
    return_code: int
    stdout_path: Path
    stderr_path: Path
    output_paths: list[Path] = Field(default_factory=list)
    status: ExecutionStatus = "ok"
    status_detail: str = ""
