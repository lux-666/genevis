"""Local runner placeholder."""

from __future__ import annotations

from app.execution.base import BaseRunner, CollectResult, ExecutionResult
from app.schemas.descriptors import FrozenAttemptSpec


class LocalRunner(BaseRunner):
    """Local execution will be implemented after stage contracts are tested."""

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        raise NotImplementedError("LocalRunner execution is planned for Phase 2")

    def collect(self, result: ExecutionResult, spec: FrozenAttemptSpec) -> CollectResult:
        raise NotImplementedError("LocalRunner collection is planned for Phase 2")
