"""Immutable execution evidence backing compact stage checkpoints."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.storage.artifacts import (
    artifact_ref_for_path,
    publish_artifact_record,
    resolve_artifact_ref,
)
from app.schemas.execution import ExecutionResult, ExecutionStatus
from app.schemas.artifacts import ArtifactMeta, ArtifactRef
from app.schemas.descriptors import FrozenAttemptSpec
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout


def frozen_attempt_artifact_type(stage: str) -> str:
    return f"{stage}.frozen_attempt"


def execution_result_artifact_type(stage: str) -> str:
    return f"{stage}.execution_result"


class ExecutionEvidence(BaseModel):
    """Canonical path-free process outcome stored in durable evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1)
    success: bool
    return_code: int
    status: ExecutionStatus
    status_detail: str = ""
    error: str = Field(default="", max_length=2048)
    stdout_ref: ArtifactRef
    stderr_ref: ArtifactRef


def build_execution_evidence(
    state: Mapping[str, Any],
    result: ExecutionResult,
    *,
    error: str = "",
) -> ExecutionEvidence:
    """Convert one runtime result to the strict, indexed durable contract."""

    session_id = str(state.get("session_id") or "")
    data_dir = state.get("data_dir", "")
    return ExecutionEvidence(
        attempt_id=result.attempt_id,
        success=result.success,
        return_code=result.return_code,
        status=result.status,
        status_detail=result.status_detail,
        error=_bounded_error(error),
        stdout_ref=artifact_ref_for_path(
            data_dir,
            session_id,
            result.stdout_path,
            expected_type="command.stdout",
        ),
        stderr_ref=artifact_ref_for_path(
            data_dir,
            session_id,
            result.stderr_path,
            expected_type="command.stderr",
        ),
    )


def validate_execution_evidence(
    state: Mapping[str, Any],
    value: ExecutionEvidence | Mapping[str, Any],
    *,
    expected_attempt_id: str | None = None,
) -> ExecutionEvidence:
    """Validate the strict result body and both indexed log references."""

    evidence = (
        value
        if isinstance(value, ExecutionEvidence)
        else ExecutionEvidence.model_validate(value)
    )
    if expected_attempt_id is not None and evidence.attempt_id != expected_attempt_id:
        raise DataLayoutError("execution evidence does not match the frozen attempt")
    execution_log_paths(state, evidence)
    return evidence


def execution_log_paths(
    state: Mapping[str, Any],
    evidence: ExecutionEvidence,
) -> tuple[Path, Path]:
    """Resolve and integrity-check the durable stdout/stderr references."""

    session_id = str(state.get("session_id") or "")
    data_dir = state.get("data_dir", "")
    if evidence.stdout_ref.artifact_type != "command.stdout":
        raise DataLayoutError("execution stdout ref has the wrong artifact type")
    if evidence.stderr_ref.artifact_type != "command.stderr":
        raise DataLayoutError("execution stderr ref has the wrong artifact type")
    stdout_path = resolve_artifact_ref(data_dir, session_id, evidence.stdout_ref)
    stderr_path = resolve_artifact_ref(data_dir, session_id, evidence.stderr_ref)
    return stdout_path, stderr_path


def persist_frozen_attempt(
    state: Mapping[str, Any],
    attempt: FrozenAttemptSpec,
) -> ArtifactRef:
    """Persist one complete attempt descriptor and return its compact ref."""

    if attempt.session_id != state.get("session_id"):
        raise DataLayoutError("frozen attempt session does not match the run state")
    _require_retained_attempt_work(attempt)
    payload = {
        "schema_version": 2,
        "record_type": "frozen_attempt",
        "stage": attempt.stage,
        "method_name": attempt.method_name,
        "attempt_id": attempt.attempt_id,
        "attempt": attempt.model_dump(mode="json"),
    }
    return _persist_record(
        state,
        stage=attempt.stage,
        attempt_id=attempt.attempt_id,
        method_name=attempt.method_name,
        kind="attempt",
        artifact_type=frozen_attempt_artifact_type(attempt.stage),
        payload=payload,
    )


def load_frozen_attempt(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_stage: str,
    expected_method_name: str | None = None,
) -> FrozenAttemptSpec:
    """Integrity-check and hydrate one attempt descriptor from its ref."""

    payload = _load_record(
        state,
        value,
        expected_type=frozen_attempt_artifact_type(expected_stage),
    )
    if payload.get("record_type") != "frozen_attempt":
        raise DataLayoutError("attempt evidence has the wrong record type")
    attempt = FrozenAttemptSpec.model_validate(payload.get("attempt"))
    if attempt.attempt_id != payload.get("attempt_id"):
        raise DataLayoutError("attempt evidence identity mismatch")
    if payload.get("stage") != expected_stage or payload.get(
        "method_name"
    ) != attempt.method_name:
        raise DataLayoutError("attempt evidence identity mismatch")
    if (
        attempt.session_id != state.get("session_id")
        or attempt.stage != expected_stage
    ):
        raise DataLayoutError("attempt evidence identity mismatch")
    if (
        expected_method_name is not None
        and attempt.method_name != expected_method_name
    ):
        raise DataLayoutError("attempt evidence has the wrong method identity")
    _require_retained_attempt_work(attempt)
    return attempt


def _require_retained_attempt_work(attempt: FrozenAttemptSpec) -> None:
    if not attempt.working_dir.exists() or not attempt.working_dir.is_dir():
        raise DataLayoutError(
            f"frozen attempt working directory is missing: {attempt.working_dir}"
        )
    if not attempt.script_path.exists() or not attempt.script_path.is_file():
        raise DataLayoutError(
            f"frozen attempt work script is missing: {attempt.script_path}"
        )


def persist_execution_result(
    state: Mapping[str, Any],
    *,
    stage: str,
    method_name: str,
    result: ExecutionResult,
    error: str = "",
) -> ArtifactRef:
    """Persist one structured process result and return its compact ref."""

    payload = {
        "schema_version": 2,
        "record_type": "execution_result",
        "stage": stage,
        "method_name": method_name,
        "attempt_id": result.attempt_id,
        "result": build_execution_evidence(
            state,
            result,
            error=error,
        ).model_dump(mode="json"),
    }
    return _persist_record(
        state,
        stage=stage,
        attempt_id=result.attempt_id,
        method_name=method_name,
        kind="execution",
        artifact_type=execution_result_artifact_type(stage),
        payload=payload,
    )


def load_execution_evidence(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_stage: str,
    expected_attempt_id: str | None = None,
    expected_method_name: str | None = None,
) -> ExecutionEvidence:
    """Integrity-check and load one path-free process result artifact."""

    payload = _load_record(
        state,
        value,
        expected_type=execution_result_artifact_type(expected_stage),
    )
    if payload.get("record_type") != "execution_result":
        raise DataLayoutError("execution evidence has the wrong record type")
    result = validate_execution_evidence(
        state,
        payload.get("result") or {},
        expected_attempt_id=expected_attempt_id,
    )
    if (
        result.attempt_id != payload.get("attempt_id")
        or payload.get("stage") != expected_stage
    ):
        raise DataLayoutError("execution evidence identity mismatch")
    if (
        expected_method_name is not None
        and payload.get("method_name") != expected_method_name
    ):
        raise DataLayoutError("execution evidence has the wrong method identity")
    return result


def _bounded_error(value: str, *, limit: int = 2048) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _persist_record(
    state: Mapping[str, Any],
    *,
    stage: str,
    attempt_id: str,
    method_name: str,
    kind: str,
    artifact_type: str,
    payload: Mapping[str, Any],
) -> ArtifactRef:
    session_id = str(state.get("session_id") or "")
    paths = SessionPaths.from_run_dir(state.get("data_dir", ""))
    paths.require_session_id(session_id)
    ensure_run_layout(paths)

    encoded = (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    digest = sha256(encoded).hexdigest()
    session_prefix = f"{session_id}:{stage}:"
    local_attempt_id = (
        attempt_id[len(session_prefix) :]
        if attempt_id.startswith(session_prefix)
        else attempt_id
    )
    safe_attempt_id = (
        re.sub(r"[^A-Za-z0-9_.-]+", "_", local_attempt_id).strip("_") or "attempt"
    )
    temporary = paths.work_path(
        stage,
        "execution_evidence",
        f".{kind}.{uuid4().hex}.json",
    )
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(encoded)
    destination = paths.artifact_path(
        stage,
        "attempts",
        safe_attempt_id,
        f"{kind}.{digest}.json",
    )
    artifact = ArtifactMeta.from_file(
        temporary,
        stage=stage,
        method_name=method_name,
        artifact_type=artifact_type,
        metadata={"attempt_id": attempt_id},
    )
    return publish_artifact_record(
        paths.run_dir,
        session_id,
        artifact,
        destination=destination,
    ).ref


def _load_record(
    state: Mapping[str, Any],
    value: ArtifactRef | Mapping[str, Any],
    *,
    expected_type: str,
) -> dict[str, Any]:
    session_id = str(state.get("session_id") or "")
    ref = value if isinstance(value, ArtifactRef) else ArtifactRef.model_validate(value)
    if ref.artifact_type != expected_type:
        raise DataLayoutError(
            f"unexpected execution evidence type: {ref.artifact_type}"
        )
    path = resolve_artifact_ref(state.get("data_dir", ""), session_id, ref)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataLayoutError(
            f"execution evidence is unreadable: {ref.path_ref}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        raise DataLayoutError(f"execution evidence is not schema v2: {ref.path_ref}")
    return payload
