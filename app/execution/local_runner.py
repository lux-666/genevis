"""Local subprocess runner for frozen attempts."""

from __future__ import annotations

import os
import shlex
import subprocess

from app.schemas.execution import ExecutionResult
from app.schemas.descriptors import FrozenAttemptSpec


class LocalRunner:
    """Run a frozen attempt in its declared working directory.

    When the frozen attempt declares `conda_env` or `conda_prefix`, the command
    is dispatched through `conda run` so the method sees the configured
    environment's tools regardless of the outer shell. When neither is set the
    runner keeps the PATH-driven shell path used by stage scripts and
    fake-binary tests.
    """

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        spec.working_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.working_dir / "log"
        stderr_path = spec.working_dir / "err"

        env = os.environ.copy()
        env.update(spec.env)

        use_conda = bool(spec.conda_env or spec.conda_prefix)
        script_name = spec.script_path.name
        if use_conda:
            conda_bin = spec.conda_bin or "conda"
            target = ["-p", spec.conda_prefix] if spec.conda_prefix else ["-n", spec.conda_env]
            command: list[str] | str = [
                conda_bin,
                "run",
                "--live-stream",
                "--no-capture-output",
                *target,
                spec.interpreter,
                script_name,
            ]
            shell = False
        else:
            parts: list[str] = []
            if spec.env_setup:
                parts.append(spec.env_setup)
            parts.append(f"{shlex.quote(spec.interpreter)} {shlex.quote(script_name)}")
            command = " && ".join(parts)
            shell = True

        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w",
            encoding="utf-8",
        ) as stderr:
            completed = subprocess.run(
                command,
                cwd=spec.working_dir,
                env=env,
                shell=shell,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )

        success = completed.returncode == 0
        return ExecutionResult(
            attempt_id=spec.attempt_id,
            success=success,
            return_code=completed.returncode,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_paths=spec.expected_outputs,
            status="ok" if success else "failed",
        )
