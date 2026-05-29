"""Structured run-config contract for Phase 4 local execution.

Lets `gene-agent run --run-config <file>.json` supply the parameters that
previously required a Python one-liner: the set of stages to opt into local
execution, shared assembly facts, and method-specific execution parameters.

The schema rejects unknown stages, unknown fields, and the combination of
requesting local `assembly` execution without the three required Flye
parameters. It does not invent defaults, does not carry descriptor or env
facts, and is not a method catalog.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.schemas.artifacts import FunctionDatabaseProvenance
from app.schemas.shared_parameters import SharedParameters

RequestableStage = Literal[
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review",
    "hic_scaffolding",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation",
]

LocalExecStage = Literal[
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_review",
    "hic_scaffolding",
    "repeat_annotation",
]

FlyeReadType = Literal["nano-raw", "nano-hq", "pacbio-raw", "pacbio-hifi"]
ReadQCLengthFilterMethod = Literal[
    "skip",
    "read_qc.length_filter.seqkit_seq",
    "read_qc.length_filter.chopper",
    "read_qc.length_filter.filtlong",
    "read_qc.length_filter.fastplong",
]
ReviewPolicyMode = Literal["strict", "auto"]
ReviewPolicyStage = Literal["assembly_review", "hic_scaffolding"]


class StageReviewPolicyConfig(BaseModel):
    """Per-stage review policy override."""

    model_config = ConfigDict(extra="forbid")

    mode: ReviewPolicyMode | None = None
    max_blocked_seconds: int | None = Field(default=None, ge=0)


class ReviewPolicyConfig(StageReviewPolicyConfig):
    """Run-level review policy defaults plus optional stage overrides."""

    mode: ReviewPolicyMode = "strict"
    stages: dict[ReviewPolicyStage, StageReviewPolicyConfig] = Field(default_factory=dict)

    def effective_for_stage(self, stage: ReviewPolicyStage) -> dict[str, Any]:
        payload: dict[str, Any] = {"mode": self.mode}
        if self.max_blocked_seconds is not None:
            payload["max_blocked_seconds"] = self.max_blocked_seconds
        override = self.stages.get(stage)
        if override:
            if override.mode is not None:
                payload["mode"] = override.mode
            if override.max_blocked_seconds is not None:
                payload["max_blocked_seconds"] = override.max_blocked_seconds
        return payload


class FlyeRunParameters(BaseModel):
    """Explicit Flye parameters for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    read_type: FlyeReadType | None = None
    estimated_genome_size: int | None = Field(default=None, gt=0)
    threads: int = Field(gt=0)


class AssemblyRunConfig(BaseModel):
    """Run-config block for the assembly stage."""

    model_config = ConfigDict(extra="forbid")

    flye_parameters: FlyeRunParameters | None = None


class ReadQCLengthFilterConfig(BaseModel):
    """Explicit read_qc length-filter choice."""

    model_config = ConfigDict(extra="forbid")

    method: ReadQCLengthFilterMethod = "skip"
    approved: bool = False
    thresholds: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_thresholds_for_executable_filter(self) -> "ReadQCLengthFilterConfig":
        if self.approved and self.method == "skip":
            raise ValueError("read_qc.length_filter: approved must be false when method is 'skip'")
        if self.approved and self.method not in ("skip", "read_qc.length_filter.seqkit_seq"):
            raise ValueError(
                "read_qc.length_filter approved execution is currently supported only for "
                "read_qc.length_filter.seqkit_seq"
            )
        if self.method == "read_qc.length_filter.seqkit_seq":
            min_len = self.thresholds.get("min_len")
            if self.approved and (not isinstance(min_len, int) or min_len <= 0):
                raise ValueError("read_qc.length_filter.thresholds.min_len must be a positive integer when approved")
        return self


class ReadQCRunConfig(BaseModel):
    """Run-config block for read_qc choices."""

    model_config = ConfigDict(extra="forbid")

    length_filter: ReadQCLengthFilterConfig | None = None


class BuscoRunParameters(BaseModel):
    """Explicit BUSCO parameters for assembly_review local execution."""

    model_config = ConfigDict(extra="forbid")

    lineage_path: str
    lineage_name: str
    lineage_version: str
    lineage_source: str
    mode: Literal["genome"] = "genome"
    threads: int = Field(gt=0)

    @model_validator(mode="after")
    def _lineage_path_must_not_be_empty(self) -> "BuscoRunParameters":
        if not self.lineage_path.strip():
            raise ValueError("assembly_review.busco.lineage_path must be non-empty")
        if not self.lineage_name.strip():
            raise ValueError("assembly_review.busco.lineage_name must be non-empty")
        if not self.lineage_version.strip():
            raise ValueError("assembly_review.busco.lineage_version must be non-empty")
        if not self.lineage_source.strip():
            raise ValueError("assembly_review.busco.lineage_source must be non-empty")
        return self


class QuastRunParameters(BaseModel):
    """Explicit QUAST parameters for assembly_review local execution."""

    model_config = ConfigDict(extra="forbid")

    threads: int = Field(gt=0)


class AssemblyReviewRunConfig(BaseModel):
    """Run-config block for the assembly_review stage."""

    model_config = ConfigDict(extra="forbid")

    quast: QuastRunParameters | None = None
    busco: BuscoRunParameters | None = None
    max_reruns: int = Field(default=3, ge=1, le=10)


class FunctionAnnotationRunConfig(BaseModel):
    """Run-config block for user-supplied function database provenance."""

    model_config = ConfigDict(extra="forbid")

    database_provenance: list[FunctionDatabaseProvenance] | None = Field(default=None, min_length=1)


class RunConfig(BaseModel):
    """Phase 4 run-config consumed by `gene-agent run --run-config`."""

    model_config = ConfigDict(extra="forbid")

    local_exec: list[LocalExecStage] = Field(default_factory=list)
    requested_stages: list[RequestableStage] | None = None
    shared_parameters: SharedParameters | None = None
    read_qc: ReadQCRunConfig | None = None
    assembly: AssemblyRunConfig | None = None
    assembly_review: AssemblyReviewRunConfig | None = None
    function_annotation: FunctionAnnotationRunConfig | None = None
    review_policy: ReviewPolicyConfig | None = None

    @model_validator(mode="after")
    def _require_flye_threads_when_assembly_local(self) -> "RunConfig":
        if "assembly" in self.local_exec:
            if not self.assembly or not self.assembly.flye_parameters:
                raise ValueError(
                    "run_config: local_exec includes 'assembly' but "
                    "assembly.flye_parameters is missing"
                )
            if self.assembly.flye_parameters.threads is None:
                raise ValueError(
                    "run_config: local_exec includes 'assembly' but "
                    "assembly.flye_parameters.threads is missing"
                )
        return self

    @model_validator(mode="after")
    def _require_local_exec_for_approved_filter(self) -> "RunConfig":
        if (
            self.read_qc
            and self.read_qc.length_filter
            and self.read_qc.length_filter.approved
            and "read_qc" not in self.local_exec
        ):
            raise ValueError(
                "run_config: read_qc.length_filter.approved is true but "
                "'read_qc' is not in local_exec"
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
