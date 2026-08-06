"""Structured run-config contract for frozen-session execution.

The TUI freezes this document before execution. It supplies requested stages
with explicit local-execution authorization, shared biological facts, and
executor-owned settings.

The schema rejects unknown stages, unknown fields, and local `assembly`
authorization without the selected method's required parameters. It does not
invent execution consent, does not carry descriptor or env facts, and is not a
method catalog.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from app.domains.assembly.selection import (
    NormalizedReadType,
    is_method_read_type_compatible,
    normalize_read_type,
)
from app.review.gates import registered_review_stages
from app.schemas.manifest import InputManifest, Platform
from app.schemas.shared_parameters import SpeciesFacts
from app.schemas.state import MAX_WORKFLOW_STEPS
from app.workflow import MANUAL_ONLY_STAGES, analyze_review_sequence

RequestableStage = Literal[
    "read_qc",
    "genome_survey",
    "assembly",
    "assembly_dedup",
    "assembly_review",
    "hic_scaffolding",
    "gapcloser",
    "t2t",
    "base_polish",
]

AssemblyMethod = Literal["flye", "nextdenovo", "hifiasm", "hifiasm_ont"]
AssemblyRoute = Literal["cyclone", "ont", "hifi", "auto"]
ReadQCLengthFilterMethod = Literal[
    "skip",
    "read_qc.length_filter.seqkit_seq",
]
T2TReadPlatform = Literal["cycloneseq", "ont", "clr", "pacbio_hifi"]

DEFAULT_REVIEW_MAX_BLOCKED_SECONDS = 300

_RUN_CONFIG_PATH_FIELDS = (
    ("assembly", "hifi_reads_ref"),
    ("assembly", "hic_reads_ref"),
    ("assembly_review", "busco", "lineage_path"),
    ("assembly_review", "related_species_reference_dir"),
    ("t2t", "chromosome_list_ref"),
    ("t2t", "assembly_fasta_ref"),
)


class RunConfigSharedParameters(BaseModel):
    """Cross-stage hints accepted from an external run config."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform | None = None
    read_type: NormalizedReadType | None = None
    species_facts: SpeciesFacts | None = None


class T2TReadInput(BaseModel):
    """One explicitly classified long-read input selected for T2T finishing."""

    model_config = ConfigDict(extra="forbid")

    path: str
    platform: T2TReadPlatform

    @field_validator("path")
    @classmethod
    def _path_must_be_non_empty(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("t2t.read_inputs[].path must be a non-empty path")
        return normalized


class T2TRunConfig(BaseModel):
    """Optional finishing controls for the top-level T2T stage."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["propose_only", "manual_review", "auto_patch"] | None = None
    chromosome_list_ref: str | None = None
    assembly_fasta_ref: str | None = None
    read_inputs: list[T2TReadInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def _read_inputs_must_not_repeat_paths(self) -> "T2TRunConfig":
        paths = [item.path for item in self.read_inputs]
        if len(paths) != len(set(paths)):
            raise ValueError("t2t.read_inputs must not repeat a path")
        return self


class StageReviewPolicyConfig(BaseModel):
    """Per-stage foreground review countdown override."""

    model_config = ConfigDict(extra="forbid")

    max_blocked_seconds: int | None = Field(default=None, ge=-1)


class ReviewPolicyConfig(BaseModel):
    """Run-level foreground review countdown plus optional stage overrides."""

    model_config = ConfigDict(extra="forbid")

    max_blocked_seconds: int = Field(
        default=DEFAULT_REVIEW_MAX_BLOCKED_SECONDS,
        ge=-1,
    )
    stages: dict[str, StageReviewPolicyConfig] = Field(default_factory=dict)

    @field_validator("stages")
    @classmethod
    def _stage_overrides_must_target_registered_review_stages(
        cls, value: dict[str, StageReviewPolicyConfig]
    ) -> dict[str, StageReviewPolicyConfig]:
        registered = set(registered_review_stages())
        unknown = sorted(set(value) - registered)
        if unknown:
            raise ValueError(
                "review_policy.stages contains unregistered review stages: "
                + ", ".join(unknown)
            )
        return value

    def effective_max_blocked_seconds(self, stage: str) -> int:
        override = self.stages.get(stage)
        if override is not None and override.max_blocked_seconds is not None:
            return override.max_blocked_seconds
        return self.max_blocked_seconds


class FlyeRunParameters(BaseModel):
    """Flye executor-owned settings for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    threads: int | None = Field(default=None, gt=0)


class NextDenovoRunParameters(BaseModel):
    """NextDenovo executor-owned settings for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    threads: int | None = Field(default=None, gt=0)


class HifiasmRunParameters(BaseModel):
    """Hifiasm executor-owned settings for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    threads: int | None = Field(default=None, gt=0)
    use_ultralong_ont: bool = False
    use_hic_phasing: bool = False


class HifiasmOntRunParameters(BaseModel):
    """Hifiasm ONT executor-owned settings for local assembly execution."""

    model_config = ConfigDict(extra="forbid")

    threads: int | None = Field(default=None, gt=0)


def infer_assembly_method(assembly: "AssemblyRunConfig") -> AssemblyMethod | None:
    """Return the explicit assembly method or infer it from one parameter block."""
    if assembly.assembly_method is not None:
        return assembly.assembly_method

    provided = _provided_assembly_parameter_methods(assembly)
    if len(provided) == 1:
        return provided[0]
    return None


def _provided_assembly_parameter_methods(
    assembly: "AssemblyRunConfig",
) -> list[AssemblyMethod]:
    candidates: tuple[tuple[AssemblyMethod, BaseModel | None], ...] = (
        ("flye", assembly.flye_parameters),
        ("nextdenovo", assembly.nextdenovo_parameters),
        ("hifiasm", assembly.hifiasm_parameters),
        ("hifiasm_ont", assembly.hifiasm_ont_parameters),
    )
    return [name for name, value in candidates if value is not None]


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
        """Validate explicit method/read-type compatibility for route hints."""
        method = infer_assembly_method(self)
        if method is None or self.assembly_route == "auto":
            return self

        read_type = normalize_read_type(self.assembly_route)
        if read_type is None:
            return self
        if not is_method_read_type_compatible(method, read_type):
            raise ValueError(
                f"assembly_route '{self.assembly_route}' implies read_type "
                f"'{read_type}' and is "
                f"incompatible with assembly_method '{method}'"
            )

        return self

    @model_validator(mode="after")
    def _reject_parameter_blocks_for_other_methods(self) -> "AssemblyRunConfig":
        if self.assembly_method is None:
            return self
        provided = _provided_assembly_parameter_methods(self)
        unexpected = [method for method in provided if method != self.assembly_method]
        if unexpected:
            required_key = f"assembly.{self.assembly_method}_parameters"
            raise ValueError(
                f"assembly_method {self.assembly_method!r} requires "
                f"{required_key} and cannot use parameter blocks for {unexpected}"
            )
        return self


def validate_local_assembly_execution(assembly: AssemblyRunConfig | None) -> None:
    """Validate the method/parameter contract after all local-exec inputs merge."""
    if assembly is None:
        raise ValueError(
            "run_config: requested_stages[assembly].allow_local_execution is true "
            "but "
            "assembly.flye_parameters, assembly.nextdenovo_parameters, or "
            "assembly.hifiasm_parameters, or assembly.hifiasm_ont_parameters is missing"
        )

    method = infer_assembly_method(assembly)
    if method == "flye":
        if not assembly.flye_parameters:
            raise ValueError(
                "run_config: local assembly method 'flye' requires "
                "assembly.flye_parameters"
            )
        return
    if method == "nextdenovo":
        if not assembly.nextdenovo_parameters:
            raise ValueError(
                "run_config: local assembly method 'nextdenovo' requires "
                "assembly.nextdenovo_parameters"
            )
        return
    if method == "hifiasm":
        if not assembly.hifiasm_parameters:
            raise ValueError(
                "run_config: local assembly method 'hifiasm' requires "
                "assembly.hifiasm_parameters"
            )
        return
    if method == "hifiasm_ont":
        if not assembly.hifiasm_ont_parameters:
            raise ValueError(
                "run_config: local assembly method 'hifiasm_ont' requires "
                "assembly.hifiasm_ont_parameters"
            )
        return

    provided = _provided_assembly_parameter_methods(assembly)
    if not provided:
        raise ValueError(
            "run_config: requested_stages[assembly].allow_local_execution is true "
            "but "
            "assembly.flye_parameters, assembly.nextdenovo_parameters, or "
            "assembly.hifiasm_parameters, or assembly.hifiasm_ont_parameters is missing"
        )
    if len(provided) > 1:
        raise ValueError(
            "run_config: local assembly has parameters for multiple methods; "
            "set assembly.assembly_method"
        )


def validate_genome_size_execution_plan(
    run_config: "RunConfig",
    manifest: InputManifest,
) -> None:
    """Require a provided size unless an earlier local survey can produce it."""

    if run_config.assembly is None:
        return

    def is_local(index: int) -> bool:
        return run_config.requested_stages[index].allow_local_execution

    local_assembly_indexes = [
        index
        for index, request in enumerate(run_config.requested_stages)
        if request.stage == "assembly" and is_local(index)
    ]
    if not local_assembly_indexes:
        return

    method = infer_assembly_method(run_config.assembly)
    if method not in {"flye", "nextdenovo"}:
        return

    if manifest.genome_size_estimate:
        return

    has_survey_reads = any(
        asset.asset_type
        in {
            "raw_short_dna_reads",
            "clean_short_dna_reads",
            "raw_long_reads",
            "clean_long_reads",
        }
        for asset in manifest.assets
    )
    survey_is_configured = (
        run_config.genome_survey is not None
        and run_config.genome_survey.survey_parameters is not None
    )
    every_assembly_has_prior_survey = all(
        any(
            earlier.stage == "genome_survey" and is_local(earlier_index)
            for earlier_index, earlier in enumerate(
                run_config.requested_stages[:assembly_index]
            )
        )
        for assembly_index in local_assembly_indexes
    )
    if survey_is_configured and has_survey_reads and every_assembly_has_prior_survey:
        return

    raise ValueError(
        f"run_config: local assembly method {method!r} requires a positive "
        "genome size; "
        "provide it in the manifest, or schedule executable "
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
            raise ValueError(
                "read_qc.length_filter: approved must be false when method is 'skip'"
            )
        if self.approved and self.method not in (
            "skip",
            "read_qc.length_filter.seqkit_seq",
        ):
            raise ValueError(
                "read_qc.length_filter approved execution is currently "
                "supported only for "
                "read_qc.length_filter.seqkit_seq"
            )
        if self.method == "read_qc.length_filter.seqkit_seq":
            min_len = self.thresholds.get("min_len")
            if self.approved and (not isinstance(min_len, int) or min_len <= 0):
                raise ValueError(
                    "read_qc.length_filter.thresholds.min_len must be a "
                    "positive integer when approved"
                )
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
            raise ValueError(
                "genome_survey.survey_parameters.hash_size must be non-empty"
            )
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
    related_species_reference_dir: str | None = None


class AutoHiCRunParameters(BaseModel):
    """Pinned executor settings plus an optional enzyme fact for the LLM."""

    model_config = ConfigDict(extra="forbid")

    restriction_enzyme: str | None = None
    threads: int | None = Field(default=None, gt=0)
    python_command: str = "python3.9"


class HiCScaffoldingRunConfig(BaseModel):
    """AutoHiC runtime configuration; method and enzyme approval are stage-owned."""

    model_config = ConfigDict(extra="forbid")

    autohic_parameters: AutoHiCRunParameters | None = None
    retry_on_execution_failure: bool = False
    max_execution_retries: int = Field(default=0, ge=0)
    max_review_retries: int = Field(default=1, ge=0)


class RequestedStage(BaseModel):
    """One requested workflow stage and its explicit local-execution consent."""

    model_config = ConfigDict(extra="forbid")

    stage: RequestableStage
    allow_local_execution: StrictBool


class RunConfig(BaseModel):
    """Strict run-config consumed by the internal frozen-session executor."""

    model_config = ConfigDict(extra="forbid")

    requested_stages: list[RequestedStage] = Field(max_length=MAX_WORKFLOW_STEPS - 1)
    shared_parameters: RunConfigSharedParameters | None = None
    read_qc: ReadQCRunConfig | None = None
    genome_survey: GenomeSurveyRunConfig | None = None
    assembly: AssemblyRunConfig | None = None
    assembly_review: AssemblyReviewRunConfig | None = None
    hic_scaffolding: HiCScaffoldingRunConfig | None = None
    t2t: T2TRunConfig | None = None
    review_policy: ReviewPolicyConfig = Field(default_factory=ReviewPolicyConfig)

    @model_validator(mode="after")
    def _require_method_parameters_when_assembly_local(self) -> "RunConfig":
        if self.any_occurrence_allows_local_execution("assembly"):
            validate_local_assembly_execution(self.assembly)
        if (
            self.any_occurrence_allows_local_execution("hic_scaffolding")
            and self.hic_scaffolding is None
        ):
            raise ValueError(
                "run_config: requested_stages[hic_scaffolding].allow_local_execution "
                "is true but "
                "hic_scaffolding execution config is missing"
            )
        return self

    @model_validator(mode="after")
    def _validate_requested_stages(self) -> "RunConfig":
        invalid_local = [
            request.stage
            for request in self.requested_stages
            if request.allow_local_execution and request.stage in MANUAL_ONLY_STAGES
        ]
        if invalid_local:
            raise ValueError(
                "run_config.requested_stages allow_local_execution is unsupported "
                "for manual-only/non-local stage(s): " + ", ".join(invalid_local)
            )
        return self

    @model_validator(mode="after")
    def _require_mandatory_assembly_reviews(self) -> "RunConfig":
        """Reject an occurrence sequence with an unclosed assembly handoff."""

        analyze_review_sequence(self.requested_stage_names())
        return self

    @model_validator(mode="after")
    def _require_local_execution_for_approved_filter(self) -> "RunConfig":
        if (
            self.read_qc
            and self.read_qc.length_filter
            and self.read_qc.length_filter.approved
            and not self.any_occurrence_allows_local_execution("read_qc")
        ):
            raise ValueError(
                "run_config: read_qc.length_filter.approved is true but "
                "requested_stages[read_qc].allow_local_execution is not true"
            )
        return self

    def requested_stage_names(self) -> list[RequestableStage]:
        """Return the exact frozen occurrence sequence, including repeats."""

        return [request.stage for request in self.requested_stages]

    def any_occurrence_allows_local_execution(self, stage: str) -> bool:
        """Return whether any declared occurrence grants local-process consent."""

        return any(
            request.stage == stage and request.allow_local_execution
            for request in self.requested_stages
        )


def canonicalize_run_config_paths(
    payload: Mapping[str, Any],
    *,
    source_dir: Path | None,
) -> dict[str, Any]:
    """Return a copy whose declared filesystem inputs are canonical paths.

    File-backed configs resolve relative paths against their own directory.
    In-memory configs have no stable anchor and must already contain absolute
    paths.  This function is the shared path boundary for ordinary run-config
    loading and the create-only frozen-input transaction.
    """

    normalized = deepcopy(dict(payload))
    for field_path in _RUN_CONFIG_PATH_FIELDS:
        _canonicalize_payload_path(normalized, field_path, source_dir)
    _canonicalize_t2t_read_inputs(normalized, source_dir)
    return normalized


def _canonicalize_payload_path(
    payload: dict[str, Any],
    field_path: tuple[str, ...],
    source_dir: Path | None,
) -> None:
    parent: Any = payload
    for key in field_path[:-1]:
        if not isinstance(parent, dict) or key not in parent:
            return
        parent = parent[key]
    if not isinstance(parent, dict):
        return
    key = field_path[-1]
    value = parent.get(key)
    if value is None:
        return
    parent[key] = _canonical_path(
        value,
        source_dir=source_dir,
        field=".".join(field_path),
    )


def _canonicalize_t2t_read_inputs(
    payload: dict[str, Any],
    source_dir: Path | None,
) -> None:
    raw_t2t = payload.get("t2t")
    if raw_t2t is None:
        return
    if not isinstance(raw_t2t, dict):
        return
    raw_inputs = raw_t2t.get("read_inputs")
    if raw_inputs is None:
        return
    if not isinstance(raw_inputs, list):
        raise ValueError("t2t.read_inputs must be a list")
    for index, item in enumerate(raw_inputs):
        if not isinstance(item, dict):
            raise ValueError(f"t2t.read_inputs[{index}] must be an object")
        item["path"] = _canonical_path(
            item.get("path"),
            source_dir=source_dir,
            field=f"t2t.read_inputs[{index}].path",
        )


def _canonical_path(
    value: object,
    *,
    source_dir: Path | None,
    field: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty path")
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(path.resolve(strict=False))
    if source_dir is None:
        raise ValueError(f"{field} must be absolute for an in-memory run config")
    return str((source_dir / path).resolve(strict=False))


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
        canonical = canonicalize_run_config_paths(
            raw,
            source_dir=config_path.expanduser().resolve(strict=False).parent,
        )
        return RunConfig.model_validate(canonical)
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"run-config failed validation: {exc}") from exc
