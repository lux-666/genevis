"""Shared freeze/run helpers for stage method attempts.

Stages converge their per-method ``freeze_*`` / ``run_*`` nodes on these two
helpers so that adding a method is "build a script + spec" and "map an outcome
to stage status", not a re-copy of the env/spec/ledger/runner boilerplate.
Collect paths stay stage-specific (artifact types and status advancement
differ per stage).

These helpers take plain ``data_dir`` / ``input_manifest_ref`` values rather
than the graph ``TopLevelState`` so the execution layer does not depend on the
graph schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.storage.ledger import Ledger
from app.storage.artifacts import register_artifact_records
from app.execution.env_resolver import EnvSpec
from app.execution.local_runner import LocalRunner
from app.execution.runner import Runner
from app.schemas.execution import ExecutionResult
from app.schemas.artifacts import ArtifactMeta
from app.schemas.descriptors import FrozenAttemptSpec
from app.storage.publish import publish_execution_logs


def build_frozen_attempt(
    *,
    attempt_id: str,
    session_id: str,
    stage: str,
    method_name: str,
    script_path: Path,
    working_dir: Path,
    expected_outputs: list[Path],
    env: dict[str, Any] | None,
    env_spec: EnvSpec | None,
    metadata: dict[str, Any] | None = None,
    interpreter: str = "/bin/bash",
) -> FrozenAttemptSpec:
    """Assemble a FrozenAttemptSpec with conda fields merged from ``env_spec``."""
    attempt_kwargs: dict[str, Any] = {
        "attempt_id": attempt_id,
        "session_id": session_id,
        "stage": stage,
        "method_name": method_name,
        "script_path": script_path,
        "working_dir": working_dir,
        "interpreter": interpreter,
        "expected_outputs": expected_outputs,
        "env": {str(k): str(v) for k, v in (env or {}).items()},
        "metadata": metadata or {},
    }
    if env_spec is not None:
        attempt_kwargs.update(
            conda_bin=env_spec.conda_bin,
            conda_env=env_spec.conda_env,
            conda_prefix=env_spec.conda_prefix,
        )
    return FrozenAttemptSpec(**attempt_kwargs)


@dataclass
class RunOutcome:
    """Result of executing a frozen attempt through the shared run path."""

    success: bool
    result: ExecutionResult
    error: str = ""


def run_frozen_attempt(
    attempt: FrozenAttemptSpec,
    *,
    data_dir: Path | str,
    input_manifest_ref: str = "",
    fail_label: str,
    cleanup_outputs: bool = True,
    runner: Runner | None = None,
    ledger: Ledger | None = None,
) -> RunOutcome:
    """Run a frozen attempt with shared ledger bookkeeping.

    On failure, optionally removes partial expected outputs (``cleanup_outputs``)
    and records an ``attempt_failed`` ledger event. The caller maps the returned
    outcome onto its own stage status and any stage-specific post-processing
    (for example rerun events). Pass ``ledger`` to reuse a
    caller-owned ledger, or ``runner`` to substitute the executor (defaults to
    ``LocalRunner``). Command logs are published and indexed here; stage collect
    paths register only their business outputs.
    """
    ledger = ledger or Ledger(data_dir)
    ledger.create_session(attempt.session_id, input_manifest_ref)
    ledger.record_attempt_started(attempt.session_id, attempt)

    result = publish_execution_logs(
        data_dir, attempt, (runner or LocalRunner()).run(attempt)
    )
    register_artifact_records(
        data_dir,
        attempt.session_id,
        [
            ArtifactMeta.from_file(
                path,
                stage=attempt.stage,
                method_name=attempt.method_name,
                artifact_type=artifact_type,
                metadata={"attempt_id": attempt.attempt_id},
            )
            for path, artifact_type in (
                (result.stdout_path, "command.stdout"),
                (result.stderr_path, "command.stderr"),
            )
        ],
    )
    if not result.success:
        if cleanup_outputs:
            for path in attempt.expected_outputs:
                if path.exists() and path.is_file():
                    path.unlink()
        stderr = (
            result.stderr_path.read_text(encoding="utf-8")
            if result.stderr_path.exists()
            else ""
        )
        error = (
            stderr.strip()
            or f"{fail_label} failed with return code {result.return_code}"
        )
        ledger.record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=result.return_code,
            error=error,
        )
        return RunOutcome(success=False, result=result, error=error)

    return RunOutcome(success=True, result=result)
