"""Shared attempt construction and process execution primitives.

The graph-level freeze, run, and collect lifecycle lives in
``app.execution.lifecycle``.  This module owns only immutable attempt
construction plus Runner, log publication, and attempt-ledger bookkeeping.

These helpers take plain ``data_dir`` / ``input_manifest_ref`` values rather
than the graph ``TopLevelState`` so the execution layer does not depend on the
graph schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.execution.env_resolver import EnvSpec
from app.execution.local_runner import LocalRunner
from app.execution.runner import Runner
from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import ExecutionResult, InputDependency
from app.storage.artifacts import register_artifact_records
from app.storage.ledger import Ledger, LedgerConflictError, LedgerFormatError
from app.storage.paths import DataLayoutError
from app.storage.publish import publish_execution_logs

# Execution errors can originate in third-party runners and are copied into
# checkpoint state by the lifecycle.  Keep the control-plane diagnostic small;
# complete command stderr remains in the published log artifact.
FAILURE_REASON_MAX_CHARS = 2048
EXPECTED_RUN_ERRORS = (
    OSError,
    DataLayoutError,
    LedgerFormatError,
    LedgerConflictError,
)
# EXPECTED_RUN_ERRORS guards only declared infrastructure failures from ledger
# writes, log publication, and artifact indexing.  The runner invocation itself
# is guarded by OSError alone, and an ordinary ValueError from any run component
# is a contract bug that must stay visible.


class RunnerResultIdentityError(ValueError):
    """Raised before publication when a Runner reports another attempt."""


def bounded_failure_reason(
    value: object,
    *,
    limit: int = FAILURE_REASON_MAX_CHARS,
) -> str:
    """Return a compact, JSON-safe diagnostic for control-plane state."""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


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
    input_dependencies: list[InputDependency | dict[str, Any]],
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
        "input_dependencies": list(input_dependencies),
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
    boundary_failure: bool = False


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

    Expected storage and runner-environment errors become bounded failure
    outcomes.  Runner contract bugs (any non-OSError exception escaping the
    runner) propagate so programming errors stay visible.
    """
    result: ExecutionResult | None = None
    logs_published = False

    try:
        ledger = ledger or Ledger(data_dir)
        ledger.create_session(attempt.session_id, input_manifest_ref)
        ledger.record_attempt_started(attempt.session_id, attempt)
    except EXPECTED_RUN_ERRORS as exc:
        # The storage boundary failed before the process could start.
        return _bounded_execution_failure(
            data_dir=data_dir,
            ledger=ledger,
            attempt=attempt,
            result=None,
            fail_label=fail_label,
            exc=exc,
            logs_published=False,
        )

    try:
        result = (runner or LocalRunner()).run(attempt)
    except OSError as exc:
        # A missing interpreter, permission failure, or spawn error is an
        # expected execution failure.  Any other runner exception (for
        # example a ValueError contract bug) propagates to the caller.
        return _bounded_execution_failure(
            data_dir=data_dir,
            ledger=ledger,
            attempt=attempt,
            result=None,
            fail_label=fail_label,
            exc=exc,
            logs_published=False,
        )

    if result.attempt_id != attempt.attempt_id:
        if cleanup_outputs:
            _cleanup_expected_outputs(attempt)
        raise RunnerResultIdentityError(
            f"{fail_label} runner result attempt_id mismatch: "
            f"expected {attempt.attempt_id!r}, got {result.attempt_id!r}"
        )

    log_refs: list[ArtifactRef] = []
    try:
        result = publish_execution_logs(data_dir, attempt, result)
        logs_published = True
        log_refs = _register_execution_logs(data_dir, attempt, result)
    except EXPECTED_RUN_ERRORS as exc:
        return _bounded_execution_failure(
            data_dir=data_dir,
            ledger=ledger,
            attempt=attempt,
            result=result,
            fail_label=fail_label,
            exc=exc,
            logs_published=logs_published,
        )

    if not result.success:
        boundary_errors: list[str] = []
        if cleanup_outputs and (cleanup_error := _cleanup_expected_outputs(attempt)):
            boundary_errors.append(cleanup_error)
        try:
            stderr = (
                result.stderr_path.read_text(encoding="utf-8")
                if result.stderr_path.exists()
                else ""
            )
        except OSError as exc:
            stderr = f"unable to read stderr artifact: {exc}"
            boundary_errors.append(stderr)
        error = bounded_failure_reason(
            stderr.strip()
            or f"{fail_label} failed with return code {result.return_code}"
        )
        ledger_error = _record_failed_attempt(
            ledger,
            attempt,
            result=result,
            error=error,
            log_refs=log_refs,
        )
        if ledger_error:
            boundary_errors.append(ledger_error)
        if boundary_errors:
            error = bounded_failure_reason("; ".join([error, *boundary_errors]))
        return RunOutcome(
            success=False,
            result=result,
            error=error,
            boundary_failure=bool(boundary_errors),
        )

    return RunOutcome(success=True, result=result)


def _bounded_execution_failure(
    *,
    data_dir: Path | str,
    ledger: Ledger | None,
    attempt: FrozenAttemptSpec,
    result: ExecutionResult | None,
    fail_label: str,
    exc: BaseException,
    logs_published: bool,
) -> RunOutcome:
    """Materialize a bounded failure outcome for an expected boundary error.

    A result and logs are materialized when possible so the lifecycle can
    still persist normal execution evidence; if the storage boundary itself
    is broken, the lifecycle records the bounded stage failure and stops the
    batch.  ``ledger`` is ``None`` when the failure happened before a ledger
    could be opened.
    """
    detail = bounded_failure_reason(f"{fail_label} execution failed: {exc}")
    result = _ensure_failure_result(attempt, result, detail)
    log_refs: list[ArtifactRef] = []
    try:
        if not logs_published:
            result = publish_execution_logs(data_dir, attempt, result)
        log_refs = _register_execution_logs(data_dir, attempt, result)
    except EXPECTED_RUN_ERRORS as artifact_exc:
        detail = bounded_failure_reason(
            f"{detail}; execution logs could not be published: {artifact_exc}"
        )
    if ledger is not None:
        ledger_error = _record_failed_attempt(
            ledger,
            attempt,
            result=result,
            error=detail,
            log_refs=log_refs,
        )
        if ledger_error:
            detail = bounded_failure_reason(f"{detail}; {ledger_error}")
    return RunOutcome(
        success=False,
        result=result,
        error=detail,
        boundary_failure=True,
    )


def _register_execution_logs(
    data_dir: Path | str,
    attempt: FrozenAttemptSpec,
    result: ExecutionResult,
) -> list[ArtifactRef]:
    """Index published stdout/stderr snapshots and return their refs."""
    return register_artifact_records(
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


def _ensure_failure_result(
    attempt: FrozenAttemptSpec,
    result: ExecutionResult | None,
    detail: str,
) -> ExecutionResult:
    """Return a result with work-local log files even when a runner raised."""
    working_dir = attempt.working_dir
    try:
        working_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # The caller will still receive a bounded failure.  Evidence
        # persistence will fail closed if the work directory is unavailable.
        pass

    stdout_path = (
        Path(result.stdout_path) if result is not None else working_dir / "log"
    )
    stderr_path = (
        Path(result.stderr_path) if result is not None else working_dir / "err"
    )
    stdout_path = _ensure_log_file(stdout_path, working_dir / "log", "")
    stderr_path = _ensure_log_file(stderr_path, working_dir / "err", detail)
    if result is None:
        return ExecutionResult(
            attempt_id=attempt.attempt_id,
            success=False,
            return_code=1,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            output_paths=list(attempt.expected_outputs),
            status="failed",
            status_detail=detail,
        )
    return result.model_copy(
        update={
            "success": False,
            "status": "failed",
            "status_detail": bounded_failure_reason(result.status_detail or detail),
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        }
    )


def _ensure_log_file(path: Path, fallback: Path, content: str) -> Path:
    """Ensure a log path points to a readable file, best effort."""
    candidate = path
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if not candidate.is_file():
            candidate.write_text(content, encoding="utf-8")
        return candidate
    except OSError:
        try:
            fallback.parent.mkdir(parents=True, exist_ok=True)
            if not fallback.is_file():
                fallback.write_text(content, encoding="utf-8")
            return fallback
        except OSError:
            return candidate


def _cleanup_expected_outputs(attempt: FrozenAttemptSpec) -> str | None:
    """Remove partial declared outputs and return a bounded storage error."""

    try:
        for path in attempt.expected_outputs:
            if path.exists() and path.is_file():
                path.unlink()
    except OSError as exc:
        return bounded_failure_reason(f"output cleanup failed: {exc}")
    return None


def _record_failed_attempt(
    ledger: Ledger,
    attempt: FrozenAttemptSpec,
    *,
    result: ExecutionResult,
    error: str,
    log_refs: list[ArtifactRef],
) -> str | None:
    """Record failure and report whether the ledger boundary failed."""
    try:
        ledger.record_attempt_failed(
            attempt.session_id,
            attempt,
            return_code=result.return_code,
            error=bounded_failure_reason(error),
            artifacts=[ref.model_dump(mode="json") for ref in log_refs],
        )
    except EXPECTED_RUN_ERRORS as exc:
        return bounded_failure_reason(f"attempt failure ledger write failed: {exc}")
    return None
