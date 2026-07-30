"""Structured request contract for the internal session executor."""

from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from app.config import default_data_root


class EntryRequest(BaseModel):
    """Validated identity and paths passed to the internal graph executor."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, min_length=1)
    manifest: str = Field(min_length=1)
    data_dir: str = Field(default_factory=default_data_root, min_length=1)

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError(
                "session_id must not contain leading or trailing whitespace"
            )
        if "/" in value or "\\" in value:
            raise ValueError("session_id must not contain path separators")
        if value in {".", ".."}:
            raise ValueError("session_id must be a stable id, not a path segment")
        return value

    @field_validator("manifest", "data_dir")
    @classmethod
    def _validate_non_blank_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("path value must not be blank")
        return value
