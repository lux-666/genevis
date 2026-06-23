"""Publish immutable durable snapshots from mutable tool workspaces."""

from __future__ import annotations

import shlex
import os
import shutil
from errno import EXDEV
from uuid import uuid4
from pathlib import Path

from app.execution.results import ExecutionResult
from app.schemas.descriptors import FrozenAttemptSpec
from app.storage.paths import is_session_run_dir


def publish_file(source: str | Path, destination: str | Path) -> Path:
    """Atomically move a completed work file into an immutable artifact.

    Published paths never overwrite a distinct snapshot.  A replay that
    recreates byte-identical work output reuses the existing artifact and
    removes the duplicate source.  Within one run root this is an atomic
    rename and consumes no extra storage.  A cross-device move falls back to
    copy, verifies bytes, then removes the source.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        if source_path.exists():
            if _sha256(source_path) != _sha256(destination_path):
                raise FileExistsError(f"published artifact conflicts with existing snapshot: {destination_path}")
            source_path.unlink()
        return destination_path
    try:
        os.replace(source_path, destination_path)
        return destination_path
    except OSError as exc:
        if exc.errno != EXDEV:
            raise
    temporary = destination_path.with_name(f".{destination_path.name}.{uuid4().hex}.partial")
    try:
        shutil.copy2(source_path, temporary)
        if _sha256(source_path) != _sha256(temporary):
            raise OSError(f"published artifact verification failed: {source_path}")
        os.replace(temporary, destination_path)
        source_path.unlink()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination_path


def shell_publish_file(source: str | Path, destination: str | Path) -> str:
    """Return a replay-safe shell command that atomically moves a snapshot."""
    source_q = shlex.quote(str(source))
    destination_q = shlex.quote(str(destination))
    return (
        f"if test -e {destination_q}; then "
        f"if test -e {source_q}; then cmp -s {source_q} {destination_q} "
        f"&& rm -f {source_q} || {{ echo 'published artifact conflicts with existing snapshot: {destination_q}' >&2; false; }}; fi; "
        f"else mv {source_q} {destination_q}; fi"
    )


def shell_publish_variable(variable: str, destination: str | Path) -> str:
    """Return a replay-safe atomic move for a shell variable source."""
    destination_q = shlex.quote(str(destination))
    source_q = f'"${variable}"'
    return (
        f"if test -e {destination_q}; then "
        f"if test -e {source_q}; then cmp -s {source_q} {destination_q} "
        f"&& rm -f {source_q} || {{ echo 'published artifact conflicts with existing snapshot: {destination_q}' >&2; false; }}; fi; "
        f"else mv {source_q} {destination_q}; fi"
    )


def publish_execution_logs(
    data_dir: str | Path,
    attempt: FrozenAttemptSpec,
    result: ExecutionResult,
) -> ExecutionResult:
    """Return a result whose log references are durable artifact snapshots."""
    if not is_session_run_dir(data_dir, attempt.session_id):
        return result
    run_dir = Path(data_dir)
    logs_dir = run_dir / "artifacts" / attempt.stage / attempt.session_id / "logs"
    safe_attempt_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in attempt.attempt_id)
    invocation_id = uuid4().hex
    stdout = publish_file(result.stdout_path, logs_dir / f"{safe_attempt_id}.{invocation_id}.stdout.log")
    stderr = publish_file(result.stderr_path, logs_dir / f"{safe_attempt_id}.{invocation_id}.stderr.log")
    return result.model_copy(update={"stdout_path": stdout, "stderr_path": stderr})


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
