"""Runner contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec


class ExecutionResult(BaseModel):
    """Direct process result from a frozen attempt."""

    attempt_id: str
    success: bool
    return_code: int
    stdout_path: Path
    stderr_path: Path
    output_paths: list[Path] = Field(default_factory=list)


class CollectResult(BaseModel):
    """Collected artifacts after a successful attempt."""

    attempt_id: str
    artifacts: list[ArtifactMeta]


class BaseRunner(ABC):
    """Common local/remote runner interface."""

    @abstractmethod
    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        """Run a frozen attempt."""

    @abstractmethod
    def collect(self, result: ExecutionResult, spec: FrozenAttemptSpec) -> CollectResult:
        """Collect declared outputs for a frozen attempt."""
