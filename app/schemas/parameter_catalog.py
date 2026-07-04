"""Three-level assembly parameter catalog schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


ParameterLevel = Literal[0, 1, 2]
ParameterValueType = Literal["int", "float", "string", "boolean", "path", "path_list"]


class ParameterSpec(BaseModel):
    """One CLI parameter and its LLM permission boundary."""

    model_config = ConfigDict(extra="forbid")

    cli: str | None
    level: ParameterLevel
    source: str
    value_type: ParameterValueType
    allowed_values: list[Any] | None = None
    min: int | float | None = None
    max: int | float | None = None
    default: Any = None
    fixed_value: Any = None
    source_priority: list[str] | None = None
    description: str | None = None
    notes: str | None = None


class MethodParameterCatalog(BaseModel):
    """Catalog for one executable assembly method."""

    model_config = ConfigDict(extra="forbid")

    method_id: str
    tool: str
    read_type: str
    description: str | None = None
    parameters: dict[str, ParameterSpec]
