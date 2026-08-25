"""Execution result data models.

Direct outcomes of running a frozen attempt, shared by the local runner and
the method-runner helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.integrity import SHA256_PATTERN
from app.schemas.artifacts import ArtifactRef

ExecutionFailureStatus = Literal["failed", "blocked_env", "missing_tool"]
ExecutionStatus = Literal["ok", "failed", "blocked_env", "missing_tool"]
MethodExecutionAttemptStatus = Literal["frozen", "executed", "execution_failed"]


class AssetInputDependency(BaseModel):
    """One registry-owned external file consumed by a frozen attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["asset"] = "asset"
    asset_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)


class ArtifactInputDependency(BaseModel):
    """One run-owned immutable artifact consumed by a frozen attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["artifact"] = "artifact"
    artifact_ref: ArtifactRef
    path: str = Field(min_length=1)


class FileInputDependency(BaseModel):
    """One unregistered external file consumed by a frozen attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["file"] = "file"
    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN)


class DirectoryInputDependency(BaseModel):
    """One external directory consumed by a frozen attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["directory"] = "directory"
    path: str = Field(min_length=1)
    file_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)
    tree_sha256: str = Field(pattern=SHA256_PATTERN)


InputDependency: TypeAlias = Annotated[
    (
        AssetInputDependency
        | ArtifactInputDependency
        | FileInputDependency
        | DirectoryInputDependency
    ),
    Field(discriminator="kind"),
]


class ExecutionResult(BaseModel):
    """Direct process result from a frozen attempt.

    `status` distinguishes real process outcomes (ok / failed) from structured
    preflight block states (blocked_env / missing_tool) so callers can emit the
    right ledger and state vocabulary without guessing from return codes.
    """

    attempt_id: str
    success: bool
    return_code: int
    stdout_path: Path
    stderr_path: Path
    output_paths: list[Path] = Field(default_factory=list)
    status: ExecutionStatus = "ok"
    status_detail: str = ""


class MethodExecutionAttempt(BaseModel):
    """Compact recovery cursor for a method without parameter recommendation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    attempt_id: str = Field(min_length=1)
    method_id: str = Field(min_length=1)
    status: MethodExecutionAttemptStatus
    frozen_attempt_ref: ArtifactRef
    execution_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_refs_for_status(self) -> "MethodExecutionAttempt":
        if not self.frozen_attempt_ref.artifact_type.endswith(".frozen_attempt"):
            raise ValueError(
                "frozen_attempt_ref must reference frozen-attempt evidence"
            )
        if self.status == "frozen" and self.execution_ref is not None:
            raise ValueError("a frozen method attempt cannot own execution evidence")
        if self.status != "frozen" and self.execution_ref is None:
            raise ValueError("a completed method attempt requires execution evidence")
        return self
