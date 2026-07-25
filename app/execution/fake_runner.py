"""Fake runner for testing without real command execution.

FakeRunner records commands but doesn't execute them, allowing tests
to verify command construction without requiring actual tools.
"""

from __future__ import annotations

from typing import Any

from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import ExecutionResult


class FakeRunner:
    """Test runner that records commands without executing.

    This runner is used in tests to verify that stages build correct
    commands and freeze correct parameters, without requiring the actual
    tools to be installed.

    Usage in tests:
        fake_runner = FakeRunner()
        result = stage.run(state, config, runner=fake_runner)
        assert fake_runner.executed_commands[0] == ["hifiasm", "-t", "8", ...]
    """

    def __init__(self) -> None:
        self.executed_commands: list[dict[str, Any]] = []
        """List of all executed command specs (for inspection in tests)."""

        self.return_values: dict[str, ExecutionResult] = {}
        """Pre-configured return values for specific commands."""

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        """Record command spec and return mock success result.

        Args:
            spec: Frozen attempt specification

        Returns:
            ExecutionResult with success status (unless pre-configured otherwise)
        """
        # Ensure working directory exists (some stages check this)
        spec.working_dir.mkdir(parents=True, exist_ok=True)

        # Record the command for test inspection
        self.executed_commands.append(
            {
                "attempt_id": spec.attempt_id,
                "stage": spec.stage,
                "method_name": spec.method_name,
                "script_path": spec.script_path,
                "working_dir": spec.working_dir,
                "interpreter": spec.interpreter,
                "expected_outputs": spec.expected_outputs,
                "env": spec.env,
                "conda_env": spec.conda_env,
                "conda_prefix": spec.conda_prefix,
            }
        )

        # Check if there's a pre-configured result for this command
        key = f"{spec.stage}:{spec.method_name}"
        if key in self.return_values:
            return self.return_values[key]

        # Create fake output files (some stages check for their existence)
        for output_path in spec.expected_outputs:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if not output_path.exists():
                output_path.write_text("[fake output from FakeRunner]\n")

        # Create fake log files
        stdout_path = spec.working_dir / "log"
        stderr_path = spec.working_dir / "err"
        stdout_path.write_text("[fake stdout]\n")
        stderr_path.write_text("")

        # Return default success result
        return ExecutionResult(
            attempt_id=spec.attempt_id,
            success=True,
            return_code=0,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_paths=spec.expected_outputs,
            status="ok",
        )

    def expect_command(
        self,
        stage: str,
        method: str,
        result: ExecutionResult,
    ) -> None:
        """Pre-configure expected command result.

        Use this to simulate failures or specific output patterns:

        Example:
            fake_runner.expect_command(
                stage="assembly",
                method="hifiasm",
                result=ExecutionResult(
                    attempt_id="test",
                    success=False,
                    return_code=1,
                    status="failed",
                    ...
                )
            )
        """
        key = f"{stage}:{method}"
        self.return_values[key] = result

    def get_command_by_stage(self, stage: str) -> dict[str, Any] | None:
        """Get the first command executed for a given stage."""
        for cmd in self.executed_commands:
            if cmd["stage"] == stage:
                return cmd
        return None

    def get_all_commands_for_stage(self, stage: str) -> list[dict[str, Any]]:
        """Get all commands executed for a given stage."""
        return [cmd for cmd in self.executed_commands if cmd["stage"] == stage]

    def reset(self) -> None:
        """Clear all recorded commands and return values."""
        self.executed_commands.clear()
        self.return_values.clear()
