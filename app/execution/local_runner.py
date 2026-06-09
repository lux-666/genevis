"""Local subprocess runner for frozen attempts."""

from __future__ import annotations

import os
import shlex
import subprocess

from app.execution.results import CollectResult, ExecutionResult
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec


class LocalRunner:
    """Run a frozen attempt in its declared working directory.

    When the frozen attempt declares `conda_env` or `conda_prefix`, the command
    is dispatched through `conda run` so the method sees the configured
    environment's tools regardless of the outer shell. When neither is set the
    runner keeps the legacy PATH-driven shell path used by existing stage
    scripts and fake-binary tests.
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

    def collect(self, result: ExecutionResult, spec: FrozenAttemptSpec) -> CollectResult:
        if not result.success:
            raise RuntimeError(f"attempt {spec.attempt_id} failed with code {result.return_code}")

        artifacts: list[ArtifactMeta] = []
        for path in spec.expected_outputs:
            if not path.exists():
                raise FileNotFoundError(f"expected output missing: {path}")
            artifacts.append(
                ArtifactMeta.from_file(
                    path,
                    stage=spec.stage,
                    method_name=spec.method_name,
                    artifact_type=f"{spec.stage}.output",
                    metadata={"attempt_id": spec.attempt_id},
                )
            )

        for path, artifact_type in [
            (result.stdout_path, "command.stdout"),
            (result.stderr_path, "command.stderr"),
        ]:
            if path.exists():
                artifacts.append(
                    ArtifactMeta.from_file(
                        path,
                        stage=spec.stage,
                        method_name=spec.method_name,
                        artifact_type=artifact_type,
                        metadata={"attempt_id": spec.attempt_id},
                    )
                )

        return CollectResult(attempt_id=spec.attempt_id, artifacts=artifacts)
