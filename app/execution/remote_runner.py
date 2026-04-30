"""Remote SSH runner placeholder."""

from __future__ import annotations

from app.execution.base import BaseRunner, CollectResult, ExecutionResult
from app.schemas.descriptors import FrozenAttemptSpec


class RemoteRunner(BaseRunner):
    """Remote execution will reuse a persistent SSH control socket."""

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        raise NotImplementedError("RemoteRunner execution is planned for Phase 3")

    def collect(self, result: ExecutionResult, spec: FrozenAttemptSpec) -> CollectResult:
        raise NotImplementedError("RemoteRunner collection is planned for Phase 3")
