"""Local subprocess runner for frozen attempts."""

from __future__ import annotations

import os
import subprocess

from app.integrity import sha256_file
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import ExecutionResult


class LocalRunner:
    """Run a frozen attempt in its declared working directory.

    When the frozen attempt declares `conda_env` or `conda_prefix`, the command
    is dispatched through `conda run` so the method sees the configured
    environment's tools regardless of the outer shell. When neither is set the
    runner keeps the PATH-driven shell path used by stage scripts and
    fake-binary tests.
    """

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        if sha256_file(spec.script_path) != spec.script_sha256:
            raise ValueError(
                f"frozen attempt script changed after freeze: {spec.script_path}"
            )
        spec.working_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.working_dir / "log"
        stderr_path = spec.working_dir / "err"

        env = os.environ.copy()
        env.update(spec.env)

        use_conda = bool(spec.conda_env or spec.conda_prefix)
        script_path = str(spec.script_path)
        if use_conda:
            conda_bin = spec.conda_bin or "conda"
            target = (
                ["-p", spec.conda_prefix]
                if spec.conda_prefix
                else ["-n", spec.conda_env]
            )
            command: list[str] | str = [
                conda_bin,
                "run",
                "--live-stream",
                "--no-capture-output",
                *target,
                spec.interpreter,
                script_path,
            ]
            shell = False
        else:
            if spec.env_setup:
                raise ValueError(
                    "FrozenAttemptSpec.env_setup is unsupported for direct "
                    "execution; use spec.env or a dedicated setup script"
                )
            command: list[str] | str = [spec.interpreter, script_path]
            shell = False

        with (
            stdout_path.open("w", encoding="utf-8") as stdout,
            stderr_path.open(
                "w",
                encoding="utf-8",
            ) as stderr,
        ):
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
