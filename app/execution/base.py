"""Runner contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec


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


class CollectResult(BaseModel):
    """Collected artifacts after a successful attempt."""

    attempt_id: str
    artifacts: list[ArtifactMeta]


class BaseRunner(ABC):
    """Local subprocess runner interface.

    In-agent remote execution is intentionally unsupported; server runs
    happen by invoking the same CLI on the server checkout after `git pull`
    (see ``docs/deployment/mac_dev_server_pull.md``).
    """

    @abstractmethod
    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        """Run a frozen attempt."""

    @abstractmethod
    def collect(self, result: ExecutionResult, spec: FrozenAttemptSpec) -> CollectResult:
        """Collect declared outputs for a frozen attempt."""
