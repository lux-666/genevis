"""Structured run-config contract for frozen-session execution.

The TUI freezes this document before execution. It supplies requested stages
with explicit local-execution authorization, shared biological facts, and
executor-owned settings.

The schema rejects unknown stages and unknown fields.  ``assembly`` carries
only planning facts and input references: the stage selects its method and
parameters after upstream evidence exists.  The schema does not invent
execution consent, carry descriptor or env facts, or act as a method catalog.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
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

from app.domains.assembly.selection import NormalizedReadType
from app.review.gates import registered_review_stages
from app.schemas.manifest import Platform
from app.schemas.shared_parameters import SpeciesFacts
from app.schemas.state import MAX_WORKFLOW_STEPS
from app.workflow import MANUAL_ONLY_STAGES

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

AssemblyRoute = Literal["cyclone", "ont", "hifi", "auto"]
RecommendationMode = Literal["advisor", "deterministic_baseline"]
ReadQCLengthFilterMethod = Literal[
    "skip",
    "read_qc.length_filter.seqkit_seq",
]
T2TReadPlatform = Literal["cycloneseq", "ont", "clr", "pacbio_hifi"]

DEFAULT_REVIEW_MAX_BLOCKED_SECONDS = 300

_BUSCO_DATASET_CONFIG_KEYS = frozenset(
    {
        "name",
        "domain",
        "creation_date",
        "number_of_BUSCOs",
        "number_of_species",
    }
)
_BUSCO_LINEAGE_CONTRACTS: dict[str, dict[str, bool]] = {
    "odb10": {"requires_lengths_cutoff": True},
    "odb12": {"requires_lengths_cutoff": False},
    "odb12.2": {"requires_lengths_cutoff": True},
}
_BUSCO_VERSION_RE = re.compile(r"_(odb\d+(?:\.\d+)?)$")

_RUN_CONFIG_PATH_FIELDS = (
    ("assembly", "hifi_reads_ref"),
    ("assembly", "hic_reads_ref"),
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


class AssemblyRunConfig(BaseModel):
    """Planning facts and input selectors for deferred Assembly execution.

    Method identity and method-specific parameters deliberately are not valid
    RunConfig fields. They are recommended, approved, and frozen by Assembly
    after read-QC/genome-survey evidence is available.
    """

    model_config = ConfigDict(extra="forbid")

    hifi_reads_ref: str | None = None
    hic_reads_ref: str | None = None
    target_level: Literal["draft", "near_t2t", "strict_t2t"] = "draft"
    assembly_route: AssemblyRoute = "auto"
    method_route_advisor: RecommendationMode = "advisor"
    parameter_advisor: RecommendationMode = "advisor"


class ReadQCLengthFilterConfig(BaseModel):
    """Operator approval and thresholds for the system-owned QC filter."""

    model_config = ConfigDict(extra="forbid")

    approved: bool = False
    thresholds: dict[str, Any] = Field(default_factory=dict)

    def selected_method(self) -> ReadQCLengthFilterMethod:
        """Return the internal fixed method without exposing it in RunConfig."""

        return "read_qc.length_filter.seqkit_seq" if self.approved else "skip"

    @model_validator(mode="after")
    def _require_thresholds_for_executable_filter(self) -> "ReadQCLengthFilterConfig":
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


class AssemblyDedupRunConfig(BaseModel):
    """Explicit authorization and frozen executor knobs for purge_dups."""

    model_config = ConfigDict(extra="forbid")

    automatic_purge_approved: StrictBool = False
    threads: int = Field(default=8, gt=0)
    get_seqs_exclude_haplotigs: StrictBool = True


class BuscoRunParameters(BaseModel):
    """BUSCO execution knobs plus a legacy lineage consistency assertion.

    Database paths, versions, and provenance are host-owned and therefore are
    rejected by this RunConfig model. The freeze boundary derives ``lineage``
    from the manifest target species and the active host database registry.
    """

    model_config = ConfigDict(extra="forbid")

    lineage: str | None = None
    mode: Literal["genome"] = "genome"
    threads: int = Field(default=8, gt=0)

    @field_validator("lineage")
    @classmethod
    def _lineage_selector_must_be_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("assembly_review.busco.lineage must be non-empty")
        return value


class QuastRunParameters(BaseModel):
    """Explicit QUAST parameters for assembly_review local execution."""

    model_config = ConfigDict(extra="forbid")

    threads: int = Field(gt=0)


class AssemblyReviewRunConfig(BaseModel):
    """Run-config block for the assembly_review stage."""

    model_config = ConfigDict(extra="forbid")

    quast: QuastRunParameters | None = None
    busco: BuscoRunParameters | None = None


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
    assembly_dedup: AssemblyDedupRunConfig | None = None
    assembly_review: AssemblyReviewRunConfig | None = None
    hic_scaffolding: HiCScaffoldingRunConfig | None = None
    t2t: T2TRunConfig | None = None
    review_policy: ReviewPolicyConfig = Field(default_factory=ReviewPolicyConfig)

    @model_validator(mode="after")
    def _require_fixed_stage_execution_config(self) -> "RunConfig":
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
    def _require_busco_for_assembly_review(self) -> "RunConfig":
        # BUSCO database identity is host-owned.  An omitted review block,
        # or an omitted BUSCO block within it, receives executor defaults at
        # stage entry; no database path is copied into the run config.
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

    @model_validator(mode="after")
    def _require_local_execution_for_approved_purge(self) -> "RunConfig":
        if (
            self.assembly_dedup
            and self.assembly_dedup.automatic_purge_approved
            and not self.any_occurrence_allows_local_execution("assembly_dedup")
        ):
            raise ValueError(
                "run_config: assembly_dedup.automatic_purge_approved is true but "
                "requested_stages[assembly_dedup].allow_local_execution is not true"
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


def _require_nonempty_regular_file(path: Path, *, label: str) -> None:
    try:
        valid = path.is_file() and path.stat().st_size > 0
    except OSError as exc:
        raise ValueError(
            f"assembly_review.busco.lineage {label} cannot be inspected: {path}: {exc}"
        ) from exc
    if not valid:
        raise ValueError(
            f"assembly_review.busco.lineage {label} must be a non-empty "
            f"regular file: {path}"
        )


def _parse_busco_dataset_config(path: Path) -> dict[str, str]:
    _require_nonempty_regular_file(path, label="dataset.cfg")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            "assembly_review.busco.lineage dataset.cfg cannot be parsed as UTF-8: "
            f"{path}: {exc}"
        ) from exc

    config: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            raise ValueError(
                "assembly_review.busco.lineage dataset.cfg contains an invalid "
                f"entry at line {line_number}: {path}"
            )
        if key in config:
            raise ValueError(
                "assembly_review.busco.lineage dataset.cfg contains duplicate "
                f"key {key!r}: {path}"
            )
        config[key] = value

    missing = sorted(_BUSCO_DATASET_CONFIG_KEYS.difference(config))
    if missing:
        raise ValueError(
            "assembly_review.busco.lineage dataset.cfg is missing required keys: "
            + ", ".join(missing)
        )
    for key in ("number_of_BUSCOs", "number_of_species"):
        try:
            count = int(config[key])
        except ValueError as exc:
            raise ValueError(
                "assembly_review.busco.lineage dataset.cfg "
                f"{key} must be a positive integer"
            ) from exc
        if count <= 0:
            raise ValueError(
                "assembly_review.busco.lineage dataset.cfg "
                f"{key} must be a positive integer"
            )
    try:
        date.fromisoformat(config["creation_date"])
    except ValueError as exc:
        raise ValueError(
            "assembly_review.busco.lineage dataset.cfg creation_date must be "
            "an ISO date"
        ) from exc
    return config


def _busco_profile_identity(path: Path) -> str:
    _require_nonempty_regular_file(path, label="HMM profile")
    try:
        with path.open("rb") as handle:
            head = handle.read(8192).decode("ascii", errors="replace")
            handle.seek(max(0, path.stat().st_size - 64))
            tail = handle.read()
    except OSError as exc:
        raise ValueError(
            f"assembly_review.busco.lineage HMM profile cannot be read: {path}: {exc}"
        ) from exc

    lines = [line.strip() for line in head.splitlines()]
    if not lines or not lines[0].startswith("HMMER3/"):
        raise ValueError(
            "assembly_review.busco.lineage HMM profile is missing a HMMER3 "
            f"header: {path}"
        )
    fields: dict[str, str] = {}
    for line in lines[1:]:
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] in {"NAME", "LENG", "ALPH"}:
            fields[parts[0]] = parts[1].split()[0]
    if fields.get("NAME") != path.stem:
        raise ValueError(
            "assembly_review.busco.lineage HMM profile NAME must match its "
            f"filename: {path}"
        )
    try:
        length = int(fields.get("LENG", ""))
    except ValueError as exc:
        raise ValueError(
            f"assembly_review.busco.lineage HMM profile LENG is invalid: {path}"
        ) from exc
    if length <= 0 or fields.get("ALPH") != "amino":
        raise ValueError(
            "assembly_review.busco.lineage HMM profile must declare a positive "
            f"LENG and ALPH amino: {path}"
        )
    if not any(line.startswith("HMM ") for line in lines) or not tail.rstrip().endswith(
        b"//"
    ):
        raise ValueError(
            "assembly_review.busco.lineage HMM profile is structurally "
            f"incomplete: {path}"
        )
    return path.stem


def _parse_busco_scores_cutoff(path: Path) -> set[str]:
    _require_nonempty_regular_file(path, label="scores_cutoff")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            "assembly_review.busco.lineage scores_cutoff cannot be parsed: "
            f"{path}: {exc}"
        ) from exc

    identities: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            raise ValueError(
                "assembly_review.busco.lineage scores_cutoff contains an invalid "
                f"entry at line {line_number}: {path}"
            )
        identity = fields[0]
        try:
            float(fields[1])
        except ValueError as exc:
            raise ValueError(
                "assembly_review.busco.lineage scores_cutoff contains a non-numeric "
                f"score at line {line_number}: {path}"
            ) from exc
        if identity in identities:
            raise ValueError(
                "assembly_review.busco.lineage scores_cutoff contains duplicate "
                f"profile {identity!r}: {path}"
            )
        identities.add(identity)
    if not identities:
        raise ValueError(
            f"assembly_review.busco.lineage scores_cutoff contains no profiles: {path}"
        )
    return identities


def validate_busco_lineage(
    lineage_path: Path,
    *,
    lineage_name: str,
    lineage_version: str,
    lineage_source: str,
) -> None:
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (lineage_name, lineage_version, lineage_source)
    ):
        raise ValueError("BUSCO lineage identity fields must be complete")
    config = _parse_busco_dataset_config(lineage_path / "dataset.cfg")
    dataset_name = config["name"]
    if lineage_path.name != dataset_name or lineage_name != dataset_name:
        raise ValueError(
            "assembly_review.busco lineage identity mismatch: path basename, "
            "dataset.cfg name, and lineage_name must be identical"
        )

    version_match = _BUSCO_VERSION_RE.search(dataset_name)
    if version_match is None:
        raise ValueError(
            "assembly_review.busco lineage name must end in a supported "
            "OrthoDB version such as _odb10 or _odb12.2"
        )
    expected_version = version_match.group(1)
    contract = _BUSCO_LINEAGE_CONTRACTS.get(expected_version)
    if contract is None:
        raise ValueError(
            "assembly_review.busco lineage version is unsupported by this "
            f"validator: {expected_version}"
        )
    if lineage_version != expected_version:
        raise ValueError(
            "assembly_review.busco lineage_version must match the version "
            f"derived from lineage identity: expected {expected_version}"
        )

    hmms_path = lineage_path / "hmms"
    if not hmms_path.is_dir():
        raise ValueError(
            f"assembly_review.busco.lineage hmms must be a directory: {hmms_path}"
        )
    profile_ids = {_busco_profile_identity(path) for path in hmms_path.glob("*.hmm")}
    expected_count = int(config["number_of_BUSCOs"])
    if len(profile_ids) != expected_count:
        raise ValueError(
            "assembly_review.busco.lineage HMM profile count does not match "
            f"dataset.cfg number_of_BUSCOs: expected {expected_count}, "
            f"found {len(profile_ids)}"
        )

    score_ids = _parse_busco_scores_cutoff(lineage_path / "scores_cutoff")
    if score_ids != profile_ids:
        raise ValueError(
            "assembly_review.busco.lineage scores_cutoff profiles must exactly "
            "match hmms/*.hmm"
        )
    if contract["requires_lengths_cutoff"]:
        _require_nonempty_regular_file(
            lineage_path / "lengths_cutoff",
            label=f"{expected_version} lengths_cutoff",
        )

    ancestral = lineage_path / "ancestral"
    refseq = lineage_path / "refseq_db.faa.gz"
    if config["domain"].casefold() == "eukaryota":
        _require_nonempty_regular_file(ancestral, label="eukaryota ancestral")
        _require_nonempty_regular_file(refseq, label="eukaryota refseq_db.faa.gz")
    else:
        reference_inputs = (ancestral, refseq)
        try:
            reference_ready = any(
                path.is_file() and path.stat().st_size > 0 for path in reference_inputs
            )
        except OSError as exc:
            raise ValueError(
                "assembly_review.busco.lineage reference data cannot be inspected: "
                f"{exc}"
            ) from exc
        if not reference_ready:
            raise ValueError(
                "assembly_review.busco.lineage requires non-empty ancestral or "
                "refseq_db.faa.gz reference data"
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
