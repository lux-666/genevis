"""Runner abstraction and factory for unified execution control.

This module provides the Runner protocol and factory function that
selects between LocalRunner (real execution) and FakeRunner (test mock)
based on execution_mode.
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


def create_runner(execution_mode: str | None = None) -> Runner:
    """Create appropriate runner based on execution_mode.

    Args:
        execution_mode: One of:
            - "local": Real execution using LocalRunner (production)
            - "test" or "fake": Mock execution using FakeRunner (tests)
            - None or other: Default to LocalRunner

    Returns:
        Runner instance

    Examples:
        # Production execution
        runner = create_runner("local")
        result = runner.run(frozen_spec)

        # Test execution
        runner = create_runner("test")
        result = runner.run(frozen_spec)
        assert isinstance(runner, FakeRunner)
        assert runner.executed_commands[0]["stage"] == "assembly"
    """
    if execution_mode in ("test", "fake"):
        from app.execution.fake_runner import FakeRunner
        return FakeRunner()
    else:
        # Default to LocalRunner for production and "local" mode
        from app.execution.local_runner import LocalRunner
        return LocalRunner()


def is_test_mode(execution_mode: str | None) -> bool:
    """Check if execution_mode indicates test/fake execution.

    Args:
        execution_mode: Execution mode string

    Returns:
        True if test/fake mode, False otherwise
    """
    return execution_mode in ("test", "fake")
