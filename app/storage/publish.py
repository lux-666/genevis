"""Publish immutable durable snapshots from mutable tool workspaces."""

from __future__ import annotations

import os
import shlex
import shutil
from errno import EEXIST, EXDEV
from pathlib import Path
from uuid import uuid4

from app.integrity import sha256_file
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import ExecutionResult
from app.storage.paths import SessionPaths, ensure_run_layout


def publish_file(source: str | Path, destination: str | Path) -> Path:
    """Atomically move a completed work file into an immutable artifact.

    Published paths never overwrite a distinct snapshot.  A replay that
    recreates byte-identical work output reuses the existing artifact and
    removes the duplicate source.  Within one filesystem an atomic hard-link
    plus unlink consumes no extra storage and cannot replace an existing path.
    A cross-device move falls back to copy, verifies bytes, then removes the
    source.
    """
    source_path = Path(source).expanduser().resolve(strict=False)
    destination_path = Path(destination).expanduser().resolve(strict=False)
    if source_path == destination_path:
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        return destination_path
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        return _reuse_existing_snapshot(source_path, destination_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    try:
        # Link-without-replace makes the immutability check race-safe.  On the
        # same filesystem it has the storage cost of a rename after unlinking
        # the mutable work name.
        os.link(source_path, destination_path)
        source_path.unlink()
        return destination_path
    except OSError as exc:
        if exc.errno == EEXIST or destination_path.exists():
            return _reuse_existing_snapshot(source_path, destination_path)
        if exc.errno != EXDEV:
            raise
    temporary = destination_path.with_name(
        f".{destination_path.name}.{uuid4().hex}.partial"
    )
    try:
        shutil.copy2(source_path, temporary)
        if sha256_file(source_path) != sha256_file(temporary):
            raise OSError(f"published artifact verification failed: {source_path}")
        try:
            os.link(temporary, destination_path)
        except FileExistsError:
            if sha256_file(temporary) != sha256_file(destination_path):
                raise FileExistsError(
                    "published artifact conflicts with existing snapshot: "
                    f"{destination_path}"
                )
        temporary.unlink()
        source_path.unlink()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination_path


def _reuse_existing_snapshot(source_path: Path, destination_path: Path) -> Path:
    if not destination_path.is_file():
        raise FileExistsError(
            f"published artifact destination is not a file: {destination_path}"
        )
    if source_path.exists():
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        if sha256_file(source_path) != sha256_file(destination_path):
            raise FileExistsError(
                "published artifact conflicts with existing snapshot: "
                f"{destination_path}"
            )
        source_path.unlink()
    return destination_path


def shell_publish_file(source: str | Path, destination: str | Path) -> str:
    """Return a replay-safe shell command that atomically moves a snapshot."""
    return _shell_publish(shlex.quote(str(source)), destination)


def shell_publish_variable(variable: str, destination: str | Path) -> str:
    """Return a replay-safe atomic move for a shell variable source."""
    if not variable.isascii() or not variable.isidentifier():
        raise ValueError("source must be a shell variable name")
    return _shell_publish(f'"${variable}"', destination)


def _shell_publish(source_q: str, destination: str | Path) -> str:
    destination_q = shlex.quote(str(destination))
    conflict = shlex.quote(
        f"published artifact conflicts with existing snapshot: {destination}"
    )
    return (
        f"if test -e {destination_q}; then "
        f"if test -e {source_q}; then cmp -s {source_q} {destination_q} "
        f"&& rm -f {source_q} || {{ printf '%s\\n' {conflict} >&2; false; }}; fi; "
        f"else mv {source_q} {destination_q}; fi"
    )


def publish_execution_logs(
    data_dir: str | Path,
    attempt: FrozenAttemptSpec,
    result: ExecutionResult,
) -> ExecutionResult:
    """Return a result whose log references are durable artifact snapshots."""
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(attempt.session_id)
    ensure_run_layout(paths)
    workflow_step_id = (attempt.metadata or {}).get("workflow_step_id")
    if not isinstance(workflow_step_id, str) or not workflow_step_id:
        raise ValueError("execution attempt has no workflow_step_id")
    logs_dir = paths.workflow_step_artifact_path(
        workflow_step_id,
        attempt.stage,
        "logs",
    )
    safe_attempt_id = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in attempt.attempt_id
    )
    invocation_id = uuid4().hex
    stdout = publish_file(
        result.stdout_path,
        logs_dir / f"{safe_attempt_id}.{invocation_id}.stdout.log",
    )
    stderr = publish_file(
        result.stderr_path,
        logs_dir / f"{safe_attempt_id}.{invocation_id}.stderr.log",
    )
    return result.model_copy(update={"stdout_path": stdout, "stderr_path": stderr})
