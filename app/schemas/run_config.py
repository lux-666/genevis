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
    "gene_annotation",
    "function_annotation",
]

FlyeReadType = Literal["nano-raw", "nano-hq", "pacbio-raw", "pacbio-hifi"]
NextDenovoReadType = Literal["ont", "clr", "hifi"]
AssemblyMethod = Literal["flye", "nextdenovo", "canu", "hifiasm"]
ReadQCLengthFilterMethod = Literal[
    "skip",
    "read_qc.length_filter.seqkit_seq",
    "read_qc.length_filter.chopper",
    "read_qc.length_filter.filtlong",
    "read_qc.length_filter.fastplong",
]
ReviewPolicyMode = Literal["strict", "auto"]
ReviewPolicyStage = Literal["assembly", "assembly_review", "hic_scaffolding"]


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


class NextDenovoRunParameters(BaseModel):
    """Explicit NextDenovo parameters for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    read_type: NextDenovoReadType | None = None
    genome_size: int | None = Field(default=None, gt=0)
    threads: int = Field(gt=0)
    read_cutoff: int = Field(default=1000, gt=0)


class AssemblyRunConfig(BaseModel):
    """Run-config block for the assembly stage."""

    model_config = ConfigDict(extra="forbid")

    assembly_method: AssemblyMethod | None = None
    flye_parameters: FlyeRunParameters | None = None
    nextdenovo_parameters: NextDenovoRunParameters | None = None
    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    target_level: Literal["draft", "near_t2t", "strict_t2t"] = "draft"
    assembly_route: Literal["cyclone", "ont", "hifi", "auto"] = "auto"


def validate_local_assembly_execution(assembly: AssemblyRunConfig | None) -> None:
    """Validate the method/parameter contract after all local-exec inputs merge.

    Canu and hifiasm remain selectable planning candidates until their method
    adapters are implemented; they intentionally do not require executable
    method parameters yet.
    """
    if assembly is None:
        raise ValueError(
            "run_config: local_exec includes 'assembly' but "
            "assembly.flye_parameters or assembly.nextdenovo_parameters is missing"
        )

    method = assembly.assembly_method
    if method == "flye":
        if not assembly.flye_parameters:
            raise ValueError("run_config: local assembly method 'flye' requires assembly.flye_parameters")
        return
    if method == "nextdenovo":
        if not assembly.nextdenovo_parameters:
            raise ValueError("run_config: local assembly method 'nextdenovo' requires assembly.nextdenovo_parameters")
        return
    if method in {"canu", "hifiasm"}:
        if assembly.flye_parameters or assembly.nextdenovo_parameters:
            raise ValueError(
                f"run_config: local assembly method '{method}' is planning-only; "
                "do not provide Flye or NextDenovo execution parameters"
            )
        return

    provided = [
        name
        for name, value in [
            ("flye", assembly.flye_parameters),
            ("nextdenovo", assembly.nextdenovo_parameters),
        ]
        if value is not None
    ]
    if not provided:
        raise ValueError(
            "run_config: local_exec includes 'assembly' but "
            "assembly.flye_parameters or assembly.nextdenovo_parameters is missing"
        )
    if len(provided) > 1:
        raise ValueError(
            "run_config: local assembly has parameters for multiple methods; set assembly.assembly_method"
        )


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


class GenomeSurveyRunParameters(BaseModel):
    """Explicit genome_survey parameters that cannot be inferred from read_qc."""

    model_config = ConfigDict(extra="forbid")

    kmer_size: int = Field(gt=0)
    hash_size: str
    threads: int = Field(default=4, gt=0)
    read_length: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _hash_size_must_not_be_empty(self) -> "GenomeSurveyRunParameters":
        if not self.hash_size.strip():
            raise ValueError("genome_survey.survey_parameters.hash_size must be non-empty")
        return self


class GenomeSurveyRunConfig(BaseModel):
    """Run-config block for the genome_survey stage."""

    model_config = ConfigDict(extra="forbid")

    survey_parameters: GenomeSurveyRunParameters | None = None


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


class Juicer3DDNARunParameters(BaseModel):
    """Tool-specific Juicer + 3D-DNA settings, separate from input assets."""

    model_config = ConfigDict(extra="forbid")

    restriction_enzyme: str | None = None
    chrom_sizes_ref: str | None = None
    threads: int | None = Field(default=None, gt=0)
    juicer_command: str = "juicer.sh"
    juicer_extra_args: list[str] = Field(default_factory=list)
    three_dna_command: str = "run-asm-pipeline.sh"
    three_dna_extra_args: list[str] = Field(default_factory=list)


HiCMethod = Literal["juicer_3ddna_juicebox", "yahs"]
HiCInputMode = Literal["auto", "raw_reads", "merged_nodups"]


class HiCScaffoldingPlanCandidate(BaseModel):
    """A possibly incomplete Hi-C method candidate produced during planning.

    This is deliberately not accepted by ``RunConfig``.  It records what the
    TUI still needs to ask before a candidate becomes execution-ready.
    """

    model_config = ConfigDict(extra="forbid")

    method: HiCMethod
    input_mode: HiCInputMode | None = None
    chromosome_count: int | None = Field(default=None, gt=0)
    merged_nodups_ref: str | None = None
    hic_alignment_ref: str | None = None
    juicer_3ddna_parameters: Juicer3DDNARunParameters | None = None
    status: Literal["needs_clarification", "ready_to_execute"] = "needs_clarification"
    missing_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_readiness(self) -> "HiCScaffoldingPlanCandidate":
        missing: list[str] = []
        if self.method == "juicer_3ddna_juicebox":
            if self.input_mode is None:
                missing.append("input_mode")
            if self.chromosome_count is None:
                missing.append("chromosome_count")
            if self.input_mode == "merged_nodups" and not self.merged_nodups_ref:
                missing.append("merged_nodups_ref")
            if self.input_mode == "raw_reads":
                params = self.juicer_3ddna_parameters
                if not params or not params.restriction_enzyme:
                    missing.append("juicer_3ddna_parameters.restriction_enzyme")
                if not params or not params.chrom_sizes_ref:
                    missing.append("juicer_3ddna_parameters.chrom_sizes_ref")
                if not params or params.threads is None:
                    missing.append("juicer_3ddna_parameters.threads")
        elif not self.hic_alignment_ref:
            missing.append("hic_alignment_ref")
        self.missing_fields = missing
        self.status = "ready_to_execute" if not missing else "needs_clarification"
        return self


class HiCScaffoldingRunConfig(BaseModel):
    """Strict, execution-ready Hi-C configuration consumed by ``gene-agent run``."""

    model_config = ConfigDict(extra="forbid")

    method: HiCMethod = "juicer_3ddna_juicebox"
    methods: list[HiCMethod] | None = None
    input_mode: HiCInputMode | None = None
    chromosome_count: int | None = Field(default=None, gt=0)
    merged_nodups_ref: str | None = None
    hic_alignment_ref: str | None = None
    juicer_3ddna_parameters: Juicer3DDNARunParameters | None = None

    @model_validator(mode="after")
    def _require_ready_candidates(self) -> "HiCScaffoldingRunConfig":
        methods = self.methods or [self.method]
        for method in methods:
            candidate = HiCScaffoldingPlanCandidate(
                method=method,
                input_mode=self.input_mode if method == "juicer_3ddna_juicebox" else None,
                chromosome_count=self.chromosome_count if method == "juicer_3ddna_juicebox" else None,
                merged_nodups_ref=self.merged_nodups_ref if method == "juicer_3ddna_juicebox" else None,
                hic_alignment_ref=self.hic_alignment_ref if method == "yahs" else None,
                juicer_3ddna_parameters=self.juicer_3ddna_parameters if method == "juicer_3ddna_juicebox" else None,
            )
            if candidate.status != "ready_to_execute":
                raise ValueError(
                    f"run_config: hic_scaffolding.{method} is not execution-ready; "
                    f"missing {candidate.missing_fields}"
                )
        return self


class FunctionAnnotationRunConfig(BaseModel):
    """Run-config block for user-supplied function database provenance."""

    model_config = ConfigDict(extra="forbid")

    database_provenance: list[FunctionDatabaseProvenance] | None = Field(default=None, min_length=1)


class GeneAnnotationRunConfig(BaseModel):
    """Run-config block for gene-annotation lineage/species hints."""

    model_config = ConfigDict(extra="forbid")

    lineage_or_species_hints: dict[str, Any] = Field(default_factory=dict)


class RunConfig(BaseModel):
    """Phase 4 run-config consumed by `gene-agent run --run-config`."""

    model_config = ConfigDict(extra="forbid")

    local_exec: list[LocalExecStage] = Field(default_factory=list)
    requested_stages: list[RequestableStage] | None = None
    shared_parameters: SharedParameters | None = None
    read_qc: ReadQCRunConfig | None = None
    genome_survey: GenomeSurveyRunConfig | None = None
    assembly: AssemblyRunConfig | None = None
    assembly_review: AssemblyReviewRunConfig | None = None
    hic_scaffolding: HiCScaffoldingRunConfig | None = None
    gene_annotation: GeneAnnotationRunConfig | None = None
    function_annotation: FunctionAnnotationRunConfig | None = None
    review_policy: ReviewPolicyConfig | None = None

    @model_validator(mode="after")
    def _require_method_parameters_when_assembly_local(self) -> "RunConfig":
        if "assembly" in self.local_exec:
            validate_local_assembly_execution(self.assembly)
        if "hic_scaffolding" in self.local_exec and self.hic_scaffolding is None:
            raise ValueError("run_config: local_exec includes 'hic_scaffolding' but hic_scaffolding execution config is missing")
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
