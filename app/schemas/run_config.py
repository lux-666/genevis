"""Structured run-config contract for Phase 4 local execution.

Lets `gene-agent run --run-config <file>.json` supply the parameters that
previously required a Python one-liner: the set of stages to opt into local
execution and the validated Flye parameters for `assembly`.

The schema rejects unknown stages, unknown fields, and the combination of
requesting local `assembly` execution without the three required Flye
parameters. It does not invent defaults, does not carry descriptor or env
facts, and is not a method catalog.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

LocalExecStage = Literal[
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review",
    "hic_scaffolding",
    "repeat_annotation",
]

FlyeReadType = Literal["nano-raw", "nano-hq", "pacbio-raw"]


class FlyeRunParameters(BaseModel):
    """Explicit Flye parameters required when assembly runs locally."""

    model_config = ConfigDict(extra="forbid")

    read_type: FlyeReadType
    estimated_genome_size: int = Field(gt=0)
    threads: int = Field(gt=0)


class AssemblyRunConfig(BaseModel):
    """Run-config block for the assembly stage."""

    model_config = ConfigDict(extra="forbid")

    flye_parameters: FlyeRunParameters | None = None


class RunConfig(BaseModel):
    """Phase 4 run-config consumed by `gene-agent run --run-config`."""

    model_config = ConfigDict(extra="forbid")

    local_exec: list[LocalExecStage] = Field(default_factory=list)
    assembly: AssemblyRunConfig | None = None

    @model_validator(mode="after")
    def _require_flye_params_when_assembly_local(self) -> "RunConfig":
        if "assembly" in self.local_exec:
            if not self.assembly or not self.assembly.flye_parameters:
                raise ValueError(
                    "run_config: local_exec includes 'assembly' but "
                    "assembly.flye_parameters is missing"
                )
        return self


def load_run_config(path: str | Path) -> RunConfig:
    """Load and validate a run-config JSON file.

    Raises ``FileNotFoundError`` when the file is missing, ``ValueError``
    when the path is not a readable JSON file or when validation fails.
    """

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"run-config not found: {config_path}")
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"run-config is not readable: {config_path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run-config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("run-config must be a JSON object at the top level")
    try:
        return RunConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"run-config failed validation: {exc}") from exc
