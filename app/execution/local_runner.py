"""Local subprocess runner for frozen attempts."""

from __future__ import annotations

import os
import subprocess

from app.execution.base import BaseRunner, CollectResult, ExecutionResult
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec


class LocalRunner(BaseRunner):
    """Run a frozen attempt in its declared working directory."""

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        spec.working_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = spec.working_dir / "log"
        stderr_path = spec.working_dir / "err"

        env = os.environ.copy()
        env.update(spec.env)
        script_name = spec.script_path.name
        command = f"{spec.env_setup + ' && ' if spec.env_setup else ''}{spec.interpreter} {script_name}"
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
            "w",
            encoding="utf-8",
        ) as stderr:
            completed = subprocess.run(
                command,
                cwd=spec.working_dir,
                env=env,
                shell=True,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )

        return ExecutionResult(
            attempt_id=spec.attempt_id,
            success=completed.returncode == 0,
            return_code=completed.returncode,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_paths=spec.expected_outputs,
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
