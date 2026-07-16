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

from app.domains.assembly.selection import is_method_read_type_compatible, normalize_read_type
from app.schemas.artifacts import FunctionDatabaseProvenance
from app.schemas.manifest import InputManifest
from app.schemas.shared_parameters import SharedParameters

RequestableStage = Literal[
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_dedup",
    "assembly_review",
    "hic_scaffolding",
    "t2t",
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
    "t2t",
    "repeat_annotation",
    "gene_annotation",
    "function_annotation",
]

FlyeReadType = Literal["nano-raw", "nano-hq", "pacbio-raw", "pacbio-hifi"]
NextDenovoReadType = Literal["ont", "clr", "hifi"]
HifiasmReadType = Literal["hifi"]
HifiasmOntReadType = Literal["ont"]
AssemblyMethod = Literal["flye", "nextdenovo", "hifiasm", "hifiasm_ont"]
AssemblyRoute = Literal["cyclone", "ont", "hifi", "auto"]
ReadQCLengthFilterMethod = Literal[
    "skip",
    "read_qc.length_filter.seqkit_seq",
]
ReviewPolicyMode = Literal["strict", "auto"]
ReviewPolicyStage = Literal["assembly", "assembly_review", "hic_scaffolding"]

class T2TThresholdProfile(BaseModel):
    """Conservative evidence thresholds for automatic telomere extension."""

    model_config = ConfigDict(extra="forbid")

    min_motif_copies: int = Field(default=4, ge=1)
    terminal_window_bp: int = Field(default=10_000, gt=0)
    min_mapq: int = Field(default=20, ge=0)
    max_target_terminal_distance_bp: int = Field(default=1_000, ge=0)
    min_overlap_bp: int = Field(default=500, gt=0)
    min_supporting_reads: int = Field(default=1, ge=1)


class T2TRunConfig(BaseModel):
    """Optional finishing controls for the top-level T2T stage."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["propose_only", "manual_review", "auto_patch"] = "auto_patch"
    telomere_motif: str = "TTAGGG"
    chromosome_list_ref: str | None = None
    assembly_fasta_ref: str | None = None
    ont_reads_ref: str | None = None
    thresholds: T2TThresholdProfile = Field(default_factory=T2TThresholdProfile)

    @model_validator(mode="after")
    def _motif_is_dna(self) -> "T2TRunConfig":
        motif = self.telomere_motif.upper()
        if len(motif) < 2 or any(base not in "ACGT" for base in motif):
            raise ValueError("t2t.telomere_motif must be a DNA motif of at least two bases")
        self.telomere_motif = motif
        return self


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


class HifiasmRunParameters(BaseModel):
    """Explicit hifiasm parameters for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    read_type: HifiasmReadType | None = None
    threads: int = Field(gt=0)


class HifiasmOntRunParameters(BaseModel):
    """Explicit hifiasm ONT-mode parameters for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    read_type: HifiasmOntReadType | None = None
    threads: int = Field(gt=0)


def infer_assembly_method(assembly: "AssemblyRunConfig") -> AssemblyMethod | None:
    """Return the explicit assembly method or infer it from one parameter block."""
    if assembly.assembly_method is not None:
        return assembly.assembly_method

    provided = _provided_assembly_parameter_methods(assembly)
    if len(provided) == 1:
        return provided[0]
    return None


def _provided_assembly_parameter_methods(assembly: "AssemblyRunConfig") -> list[AssemblyMethod]:
    return [
        name
        for name, value in [
            ("flye", assembly.flye_parameters),
            ("nextdenovo", assembly.nextdenovo_parameters),
            ("hifiasm", assembly.hifiasm_parameters),
            ("hifiasm_ont", assembly.hifiasm_ont_parameters),
        ]
        if value is not None
    ]


class AssemblyRunConfig(BaseModel):
    """Run-config block for the assembly stage."""

    model_config = ConfigDict(extra="forbid")

    assembly_method: AssemblyMethod | None = None
    flye_parameters: FlyeRunParameters | None = None
    nextdenovo_parameters: NextDenovoRunParameters | None = None
    hifiasm_parameters: HifiasmRunParameters | None = None
    hifiasm_ont_parameters: HifiasmOntRunParameters | None = None
    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    target_level: Literal["draft", "near_t2t", "strict_t2t"] = "draft"
    assembly_route: AssemblyRoute = "auto"

    @model_validator(mode="after")
    def _validate_read_type_method_consistency(self) -> "AssemblyRunConfig":
        """Validate explicit method/read-type compatibility for legacy route hints."""
        method = infer_assembly_method(self)
        if method is None or self.assembly_route == "auto":
            return self

        read_type = normalize_read_type(self.assembly_route)
        if read_type is None:
            return self
        if not is_method_read_type_compatible(method, read_type):
            raise ValueError(
                f"assembly_route '{self.assembly_route}' implies read_type '{read_type}' and is "
                f"incompatible with assembly_method '{method}'"
            )

        return self


def validate_local_assembly_execution(assembly: AssemblyRunConfig | None) -> None:
    """Validate the method/parameter contract after all local-exec inputs merge."""
    if assembly is None:
        raise ValueError(
            "run_config: local_exec includes 'assembly' but "
            "assembly.flye_parameters, assembly.nextdenovo_parameters, or "
            "assembly.hifiasm_parameters, or assembly.hifiasm_ont_parameters is missing"
        )

    method = infer_assembly_method(assembly)
    if method == "flye":
        if not assembly.flye_parameters:
            raise ValueError("run_config: local assembly method 'flye' requires assembly.flye_parameters")
        return
    if method == "nextdenovo":
        if not assembly.nextdenovo_parameters:
            raise ValueError("run_config: local assembly method 'nextdenovo' requires assembly.nextdenovo_parameters")
        return
    if method == "hifiasm":
        if not assembly.hifiasm_parameters:
            raise ValueError("run_config: local assembly method 'hifiasm' requires assembly.hifiasm_parameters")
        return
    if method == "hifiasm_ont":
        if not assembly.hifiasm_ont_parameters:
            raise ValueError("run_config: local assembly method 'hifiasm_ont' requires assembly.hifiasm_ont_parameters")
        return

    provided = _provided_assembly_parameter_methods(assembly)
    if not provided:
        raise ValueError(
            "run_config: local_exec includes 'assembly' but "
            "assembly.flye_parameters, assembly.nextdenovo_parameters, or "
            "assembly.hifiasm_parameters, or assembly.hifiasm_ont_parameters is missing"
        )
    if len(provided) > 1:
        raise ValueError(
            "run_config: local assembly has parameters for multiple methods; set assembly.assembly_method"
        )


def validate_genome_size_execution_plan(
    run_config: "RunConfig",
    manifest: InputManifest,
    *,
    local_stages: list[str] | None = None,
) -> None:
    """Require a provided size unless an earlier local survey can produce it."""

    effective_local = list(local_stages if local_stages is not None else run_config.local_exec)
    if "assembly" not in effective_local or run_config.assembly is None:
        return

    method = infer_assembly_method(run_config.assembly)
    if method not in {"flye", "nextdenovo"}:
        return

    method_size = None
    if method == "flye" and run_config.assembly.flye_parameters:
        method_size = run_config.assembly.flye_parameters.estimated_genome_size
    elif method == "nextdenovo" and run_config.assembly.nextdenovo_parameters:
        method_size = run_config.assembly.nextdenovo_parameters.genome_size
    shared_size = run_config.shared_parameters.estimated_genome_size if run_config.shared_parameters else None
    if method_size or shared_size or manifest.genome_size_estimate:
        return

    requested = list(run_config.requested_stages or [])
    survey_precedes_assembly = not requested or (
        "genome_survey" in requested
        and "assembly" in requested
        and requested.index("genome_survey") < requested.index("assembly")
    )
    has_survey_reads = any(
        asset.asset_type
        in {"raw_short_dna_reads", "clean_short_dna_reads", "raw_long_reads", "clean_long_reads"}
        for asset in manifest.assets
    )
    survey_is_executable = (
        "genome_survey" in effective_local
        and run_config.genome_survey is not None
        and run_config.genome_survey.survey_parameters is not None
    )
    if survey_precedes_assembly and survey_is_executable and has_survey_reads:
        return

    raise ValueError(
        f"run_config: local assembly method {method!r} requires a positive genome size; "
        "provide it in the manifest/shared/method parameters, or schedule executable "
        "genome_survey before assembly with short or long genomic reads"
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
    related_species_reference_dir: str | None = None


class AutoHiCRunParameters(BaseModel):
    """Pinned runtime settings plus an optional enzyme hint for the LLM."""

    model_config = ConfigDict(extra="forbid")

    restriction_enzyme: str | None = None
    threads: int = Field(default=10, gt=0)
    number_of_edit_rounds: int = Field(default=2, ge=0, le=10)
    translocation_adjust: bool = True
    inversion_adjust: bool = True
    debris_adjust: bool = False
    error_max_epoch: int = Field(default=5, ge=1, le=20)
    error_min_len: int = Field(default=15_000, gt=0)
    error_max_len: int = Field(default=20_000_000, gt=0)
    error_filter_score: float = Field(default=0.9, ge=0.0, le=1.0)
    error_filter_iou_score: float = Field(default=0.8, ge=0.0, le=1.0)
    python_command: str = "python3.9"

    @model_validator(mode="after")
    def _validate_error_window(self) -> "AutoHiCRunParameters":
        if self.error_max_len < self.error_min_len:
            raise ValueError("autohic_parameters.error_max_len must be >= error_min_len")
        return self


class HiCScaffoldingRunConfig(BaseModel):
    """AutoHiC runtime configuration; method and enzyme approval are stage-owned."""

    model_config = ConfigDict(extra="forbid")

    autohic_parameters: AutoHiCRunParameters | None = None


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
    t2t: T2TRunConfig | None = None
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
