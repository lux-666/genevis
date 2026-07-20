"""Executor protocol shared by real and test attempt runners.

``execution_mode`` decides whether a stage freezes/runs an attempt.  Executor
choice is dependency injection: production defaults to ``LocalRunner`` while
tests may pass ``FakeRunner`` to the shared run helper.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import ExecutionResult


@runtime_checkable
class Runner(Protocol):
    """Protocol for execution runners.

    All runners must implement the run() method that takes a FrozenAttemptSpec
    and returns an ExecutionResult.
    """

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        """Execute a frozen attempt specification.

        Args:
            spec: Complete specification of what to execute

        Returns:
            ExecutionResult with status, return code, and output paths
        """
        ...
