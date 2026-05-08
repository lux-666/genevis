"""Structured entry request contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EntryRequest(BaseModel):
    """Structured request produced before entering the graph."""

    model_config = ConfigDict(extra="forbid")

    command: Literal["run"] = "run"
    session_id: str | None = Field(default=None, min_length=1)
    manifest: str = Field(min_length=1)
    data_dir: str = Field(default="data", min_length=1)

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError("session_id must not contain leading or trailing whitespace")
        if "/" in value or "\\" in value:
            raise ValueError("session_id must not contain path separators")
        if value in {".", ".."}:
            raise ValueError("session_id must be a stable id, not a path segment")
        return value

    @field_validator("manifest", "data_dir")
    @classmethod
    def _validate_non_blank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path value must not be blank")
        return value

    def to_cli_argv(self) -> list[str]:
        """Convert this request to the existing structured run CLI contract."""

        argv = ["run", "--manifest", self.manifest, "--data-dir", self.data_dir]
        if self.session_id is not None:
            argv.extend(["--session-id", self.session_id])
        return argv
