"""Strict, stage-scoped loading for an external CasePack V1."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

CASE_SCHEMA_VERSION = 1
CASE_STAGES = ("assembly", "hic_scaffolding", "t2t")
MAX_COMPARISON_ARMS = 8
MAX_QUOTE_CHARS = 2_000
MAX_QUOTES_PER_CASE = 32
MAX_TOTAL_QUOTE_CHARS = 16_000
MAX_SOURCE_BYTES = 256 * 1024

_ID_PART = re.compile(r"[a-z0-9][a-z0-9_-]*")
_SCIENTIFIC_NAME = re.compile(r"[A-Z][a-z]+(?: [a-z][a-z-]+)+")

Unit: TypeAlias = Literal["bp", "x", "fraction", "phred", "count"]
DataType: TypeAlias = Literal[
    "long_reads", "hic_reads", "short_reads", "assembly", "reference"
]
Platform: TypeAlias = Literal[
    "pacbio_hifi",
    "pacbio_clr",
    "ont",
    "cycloneseq",
    "illumina",
    "dnbseq",
]
Subtype: TypeAlias = Literal["standard", "ultralong", "hic", "pore_c"]
InputRole: TypeAlias = Literal["primary", "supporting", "reference"]
JsonScalar: TypeAlias = str | int | float | bool


class CaseValidationError(ValueError):
    """Raised when the library cannot establish a trusted Case snapshot."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must be non-empty")
    return value


def _tuple(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def _span_ids(value: Any) -> tuple[str, ...]:
    value = _tuple(value)
    if not isinstance(value, tuple) or not value:
        raise ValueError("source span IDs must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("source span IDs must be non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError("source span IDs must be unique")
    return value


class _Quantity(_StrictModel):
    value: int | float
    evidence_span_ids: tuple[str, ...] = Field(min_length=1)
    derivation: str | None = None

    _ids = field_validator("evidence_span_ids", mode="before")(
        classmethod(lambda cls, value: _span_ids(value))
    )
    _text = field_validator("derivation")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )


class BpValue(_Quantity):
    unit: Literal["bp"]

    @field_validator("value")
    @classmethod
    def positive(cls, value: int | float) -> int | float:
        if value <= 0:
            raise ValueError("bp value must be positive")
        return value


class CoverageValue(_Quantity):
    unit: Literal["x"]

    @field_validator("value")
    @classmethod
    def positive(cls, value: int | float) -> int | float:
        if value <= 0:
            raise ValueError("coverage must be positive")
        return value


class FractionValue(_Quantity):
    unit: Literal["fraction"]

    @field_validator("value")
    @classmethod
    def bounded(cls, value: int | float) -> int | float:
        if not 0 <= value <= 1:
            raise ValueError("fraction must be between 0 and 1")
        return value


class PhredValue(_Quantity):
    unit: Literal["phred"]

    @field_validator("value")
    @classmethod
    def nonnegative(cls, value: int | float) -> int | float:
        if value < 0:
            raise ValueError("phred value must be non-negative")
        return value


class CountValue(_StrictModel):
    value: int = Field(ge=0)
    unit: Literal["count"]
    evidence_span_ids: tuple[str, ...] = Field(min_length=1)
    derivation: str | None = None

    _ids = field_validator("evidence_span_ids", mode="before")(
        classmethod(lambda cls, value: _span_ids(value))
    )
    _text = field_validator("derivation")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )


class SourceDocument(_StrictModel):
    document_id: str
    kind: Literal["paper", "supplement", "script"]
    path: str

    _text = field_validator("document_id", "path")(
        classmethod(lambda cls, value: _nonblank(value))
    )


class EvidenceSpan(_StrictModel):
    span_id: str
    document_id: str
    quote: str = Field(max_length=MAX_QUOTE_CHARS)
    locator: str | None = None

    _text = field_validator("span_id", "document_id", "quote", "locator")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )


class CaseSource(_StrictModel):
    paper_id: str
    sample_id: str
    documents: tuple[SourceDocument, ...] = Field(min_length=1)
    evidence_spans: tuple[EvidenceSpan, ...] = Field(
        default=(), max_length=MAX_QUOTES_PER_CASE
    )
    doi: str | None = None
    year: int | None = Field(default=None, ge=0)
    title: str | None = None

    _collections = field_validator("documents", "evidence_spans", mode="before")(
        classmethod(lambda cls, value: _tuple(value))
    )
    _text = field_validator("paper_id", "sample_id", "doi", "title")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )

    @model_validator(mode="after")
    def unique_references(self) -> "CaseSource":
        document_ids = [item.document_id for item in self.documents]
        span_ids = [item.span_id for item in self.evidence_spans]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("duplicate document_id")
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("duplicate span_id")
        if sum(len(item.quote) for item in self.evidence_spans) > MAX_TOTAL_QUOTE_CHARS:
            raise ValueError("evidence quotes exceed the per-Case budget")
        known_documents = set(document_ids)
        if unknown := [
            item.document_id
            for item in self.evidence_spans
            if item.document_id not in known_documents
        ]:
            raise ValueError(f"unknown evidence document_id {unknown[0]!r}")
        return self


class InputData(_StrictModel):
    data_type: DataType
    platform: Platform
    subtype: Subtype | None = None
    role: InputRole
    coverage: CoverageValue | None = None
    read_n50: BpValue | None = None

    @property
    def key(self) -> tuple[str, str, str | None, str]:
        return self.data_type, self.platform, self.subtype, self.role


class AssemblySubject(_StrictModel):
    scientific_name: str
    genome_size: BpValue | None = None
    ploidy: CountValue | None = None
    heterozygosity: FractionValue | None = None
    repeat_fraction: FractionValue | None = None

    @field_validator("scientific_name")
    @classmethod
    def normalized_name(cls, value: str) -> str:
        if not _SCIENTIFIC_NAME.fullmatch(value):
            raise ValueError("scientific_name must be a normalized binomial name")
        return value


def _unique_inputs(value: tuple[InputData, ...]) -> tuple[InputData, ...]:
    keys = [item.key for item in value]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate input key (data_type, platform, subtype, role)")
    return value


class AssemblyContext(_StrictModel):
    subject: AssemblySubject
    inputs: tuple[InputData, ...] = Field(min_length=1)

    _items = field_validator("inputs", mode="before")(
        classmethod(lambda cls, value: _tuple(value))
    )
    _unique = field_validator("inputs")(
        classmethod(lambda cls, value: _unique_inputs(value))
    )


class AssemblySnapshot(_StrictModel):
    assembly_size: BpValue | None = None
    contig_n50: BpValue | None = None
    contig_count: CountValue | None = None
    gap_count: CountValue | None = None


class HiCContext(_StrictModel):
    input_assembly: AssemblySnapshot
    hic: tuple[InputData, ...] = Field(min_length=1)

    _items = field_validator("hic", mode="before")(
        classmethod(lambda cls, value: _tuple(value))
    )
    _unique = field_validator("hic")(
        classmethod(lambda cls, value: _unique_inputs(value))
    )

    @model_validator(mode="after")
    def hic_inputs_only(self) -> "HiCContext":
        if any(item.data_type != "hic_reads" for item in self.hic):
            raise ValueError("hic context accepts only hic_reads")
        return self


class TelomereContext(_StrictModel):
    complete_ends: CountValue | None = None
    expected_ends: CountValue | None = None


class T2TContext(_StrictModel):
    input_assembly: AssemblySnapshot
    telomere: TelomereContext
    inputs: tuple[InputData, ...] = Field(default=())

    _items = field_validator("inputs", mode="before")(
        classmethod(lambda cls, value: _tuple(value))
    )
    _unique = field_validator("inputs")(
        classmethod(lambda cls, value: _unique_inputs(value))
    )


class CaseMethod(_StrictModel):
    tool_id: str
    evidence_span_ids: tuple[str, ...] = Field(min_length=1)
    version: str | None = None
    runtime_method_id: str | None = None

    _ids = field_validator("evidence_span_ids", mode="before")(
        classmethod(lambda cls, value: _span_ids(value))
    )
    _text = field_validator("tool_id", "version", "runtime_method_id")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )


class CaseParameter(_StrictModel):
    name: str
    value: JsonScalar
    evidence_span_ids: tuple[str, ...] = Field(min_length=1)
    reported_name: str | None = None
    unit: Unit | None = None
    derivation: str | None = None

    _ids = field_validator("evidence_span_ids", mode="before")(
        classmethod(lambda cls, value: _span_ids(value))
    )
    _text = field_validator("name", "reported_name", "derivation")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )

    @model_validator(mode="after")
    def valid_unit_value(self) -> "CaseParameter":
        if self.unit is None:
            return self
        value = self.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("parameter unit requires a numeric value")
        if self.unit in {"bp", "x"} and value <= 0:
            raise ValueError(f"parameter {self.unit} value must be positive")
        if self.unit == "fraction" and not 0 <= value <= 1:
            raise ValueError("parameter fraction must be between 0 and 1")
        if self.unit == "phred" and value < 0:
            raise ValueError("parameter phred value must be non-negative")
        if self.unit == "count" and (not isinstance(value, int) or value < 0):
            raise ValueError("parameter count must be a non-negative integer")
        return self


class CaseAction(_StrictModel):
    method: CaseMethod
    parameters: tuple[CaseParameter, ...] = Field(default=())
    mode: str | None = None

    _items = field_validator("parameters", mode="before")(
        classmethod(lambda cls, value: _tuple(value))
    )
    _mode = field_validator("mode")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )

    @model_validator(mode="after")
    def unique_parameters(self) -> "CaseAction":
        names = [item.name for item in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("duplicate parameter name")
        return self


class ComparisonGroup(_StrictModel):
    group_id: str
    arm_id: str

    _text = field_validator("group_id", "arm_id")(
        classmethod(lambda cls, value: _nonblank(value))
    )


class Rationale(_StrictModel):
    text: str
    source_span_ids: tuple[str, ...] = Field(min_length=1)

    _ids = field_validator("source_span_ids", mode="before")(
        classmethod(lambda cls, value: _span_ids(value))
    )
    _text = field_validator("text")(classmethod(lambda cls, value: _nonblank(value)))


class CaseCuration(_StrictModel):
    status: Literal["draft", "approved", "rejected"]
    curator: str | None = None
    reviewed_at: str | None = None
    notes: str | None = None

    _text = field_validator("curator", "notes")(
        classmethod(lambda cls, value: _nonblank(value) if value is not None else value)
    )

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def iso_date(cls, value: Any) -> Any:
        if isinstance(value, date):
            value = value.isoformat()
        if isinstance(value, str):
            date.fromisoformat(_nonblank(value))
        return value


class AssemblyOutcome(_StrictModel):
    assembly_size: BpValue | None = None
    contig_n50: BpValue | None = None
    contig_count: CountValue | None = None
    busco_complete: FractionValue | None = None
    busco_duplicate: FractionValue | None = None
    qv: PhredValue | None = None
    gap_count: CountValue | None = None


class HiCOutcome(_StrictModel):
    anchored_fraction: FractionValue | None = None
    chromosome_count: CountValue | None = None
    scaffold_n50: BpValue | None = None
    orientation_errors: CountValue | None = None
    manual_corrections: CountValue | None = None


class T2TOutcome(_StrictModel):
    remaining_gaps: CountValue | None = None
    telomere_complete_ends: CountValue | None = None
    patched_regions: CountValue | None = None
    rejected_patches: CountValue | None = None
    qv: PhredValue | None = None


class _CaseBase(_StrictModel):
    schema_version: Literal[1]
    case_id: str
    source: CaseSource
    action: CaseAction
    curation: CaseCuration
    comparison_group: ComparisonGroup | None = None
    rationale: Rationale | None = None

    _id = field_validator("case_id")(classmethod(lambda cls, value: _nonblank(value)))


class AssemblyCase(_CaseBase):
    stage: Literal["assembly"]
    context: AssemblyContext
    outcome: AssemblyOutcome = Field(default_factory=AssemblyOutcome)


class HiCScaffoldingCase(_CaseBase):
    stage: Literal["hic_scaffolding"]
    context: HiCContext
    outcome: HiCOutcome = Field(default_factory=HiCOutcome)


class T2TCase(_CaseBase):
    stage: Literal["t2t"]
    context: T2TContext
    outcome: T2TOutcome = Field(default_factory=T2TOutcome)


CaseRecord: TypeAlias = Annotated[
    AssemblyCase | HiCScaffoldingCase | T2TCase,
    Field(discriminator="stage"),
]
_CASE_ADAPTER: TypeAdapter[CaseRecord] = TypeAdapter(CaseRecord)


class _Manifest(_StrictModel):
    schema_version: Literal[1]
    stages: tuple[Literal["assembly", "hic_scaffolding", "t2t"], ...]
    description: str | None = None

    _items = field_validator("stages", mode="before")(
        classmethod(lambda cls, value: _tuple(value))
    )

    @model_validator(mode="after")
    def exact_stages(self) -> "_Manifest":
        if self.stages != CASE_STAGES:
            raise ValueError(f"stages must be exactly {CASE_STAGES!r}")
        return self


@dataclass(frozen=True, slots=True)
class CaseLibrary:
    """Loader-owned immutable records; selection must never reopen source files."""

    root: Path
    records: tuple[CaseRecord, ...]

    def __iter__(self) -> Iterator[CaseRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __bool__(self) -> bool:
        return bool(self.records)


def load_case_library(root: Path) -> CaseLibrary:
    """Load and fully validate one external CasePack V1."""

    library_root = root.resolve()
    manifest_path = library_root / "manifest.yaml"
    try:
        manifest = _Manifest.model_validate(_load_mapping(manifest_path))
    except (OSError, ValueError, ValidationError) as exc:
        raise CaseValidationError(
            f"invalid Case manifest {manifest_path}: {exc}"
        ) from exc

    loaded: list[CaseRecord] = []
    for stage in manifest.stages:
        stage_dir = library_root / "cases" / stage
        if not stage_dir.is_dir():
            continue
        for case_path in sorted(stage_dir.glob("*.yaml")):
            case = _load_case(case_path)
            _validate_identity(case, case_path, stage)
            _validate_case(case, library_root)
            loaded.append(case)

    ids = [case.case_id for case in loaded]
    if len(ids) != len(set(ids)):
        raise CaseValidationError("duplicate case_id in Case library")
    _validate_comparison_groups(loaded)
    return CaseLibrary(root=library_root, records=tuple(loaded))


def _load_case(path: Path) -> CaseRecord:
    try:
        raw = _load_mapping(path)
        json.dumps(raw, allow_nan=False)
        return _CASE_ADAPTER.validate_python(raw)
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        raise CaseValidationError(f"invalid Case {path}: {exc}") from exc


def _load_mapping(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CaseValidationError("expected a YAML mapping")
    return raw


def _validate_identity(case: CaseRecord, path: Path, stage: str) -> None:
    parts = case.case_id.split(".")
    if len(parts) != 4 or any(not _ID_PART.fullmatch(part) for part in parts):
        raise CaseValidationError(
            f"{case.case_id}: case_id must be <paper_id>.<sample_id>.<stage>.<arm_slug>"
        )
    paper_id, sample_id, id_stage, arm_slug = parts
    if (
        paper_id != case.source.paper_id
        or sample_id != case.source.sample_id
        or id_stage != case.stage
        or stage != case.stage
    ):
        raise CaseValidationError(
            f"{case.case_id}: case_id components do not match Case fields"
        )
    if path.name != f"{case.case_id}.yaml":
        raise CaseValidationError(f"{case.case_id}: filename must equal <case_id>.yaml")
    if case.comparison_group and case.comparison_group.arm_id != arm_slug:
        raise CaseValidationError(
            f"{case.case_id}: comparison arm_id must equal arm_slug"
        )


def _validate_case(case: CaseRecord, root: Path) -> None:
    known_spans = {span.span_id for span in case.source.evidence_spans}
    for span_id in _referenced_span_ids(case):
        if span_id not in known_spans:
            raise CaseValidationError(
                f"{case.case_id}: unknown source span {span_id!r}"
            )

    if case.curation.status == "approved":
        if not case.curation.curator or not case.curation.reviewed_at:
            raise CaseValidationError(
                f"{case.case_id}: approved Case requires curator and reviewed_at"
            )
        if not case.outcome.model_dump(exclude_none=True):
            raise CaseValidationError(
                f"{case.case_id}: approved Case requires an outcome"
            )

    documents = {item.document_id: item for item in case.source.documents}
    contents: dict[str, str] = {}
    for document in documents.values():
        path = _source_path(root, case.source.paper_id, document.path, case.case_id)
        try:
            payload = path.read_bytes()
            if len(payload) > MAX_SOURCE_BYTES:
                raise CaseValidationError(
                    f"{case.case_id}: source document is too large"
                )
            contents[document.document_id] = payload.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise CaseValidationError(
                f"{case.case_id}: unable to read source {document.path!r}"
            ) from exc
    for span in case.source.evidence_spans:
        if span.quote not in contents[span.document_id]:
            raise CaseValidationError(
                f"{case.case_id}: quote for span {span.span_id!r} is not a "
                "source substring"
            )

    runtime_method_id = case.action.method.runtime_method_id
    if runtime_method_id:
        from app.configuration import load_descriptor_registry

        method = load_descriptor_registry().methods.get(runtime_method_id)
        if method is None or method.stage != case.stage:
            raise CaseValidationError(
                f"{case.case_id}: runtime_method_id {runtime_method_id!r} is not a "
                f"registered {case.stage} method"
            )


def _source_path(root: Path, paper_id: str, value: str, case_id: str) -> Path:
    relative = Path(value)
    expected_root = (root / "sources" / paper_id).resolve()
    if relative.is_absolute() or ".." in relative.parts:
        raise CaseValidationError(f"{case_id}: source path escapes sources/{paper_id}")
    try:
        path = (root / relative).resolve(strict=True)
    except FileNotFoundError as exc:
        raise CaseValidationError(
            f"{case_id}: source document not found: {value}"
        ) from exc
    if not path.is_relative_to(expected_root) or not path.is_file():
        raise CaseValidationError(
            f"{case_id}: source path must be inside sources/{paper_id}"
        )
    return path


def _referenced_span_ids(case: CaseRecord) -> set[str]:
    ids: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, BaseModel):
            visit(value.model_dump(mode="python", exclude_none=True))
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in {"evidence_span_ids", "source_span_ids"}:
                    ids.update(child)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(case)
    return ids


def _validate_comparison_groups(cases: list[CaseRecord]) -> None:
    groups: dict[str, list[CaseRecord]] = {}
    for case in cases:
        if case.comparison_group:
            groups.setdefault(case.comparison_group.group_id, []).append(case)
    for group_id, members in groups.items():
        if not 2 <= len(members) <= MAX_COMPARISON_ARMS:
            raise CaseValidationError(
                f"comparison_group {group_id!r} must contain "
                f"2-{MAX_COMPARISON_ARMS} arms"
            )
        first = members[0]
        arms = [
            case.comparison_group.arm_id for case in members if case.comparison_group
        ]
        if len(arms) != len(set(arms)):
            raise CaseValidationError(
                f"comparison_group {group_id!r} has duplicate arm_id"
            )
        for case in members[1:]:
            if (
                case.stage != first.stage
                or case.source.paper_id != first.source.paper_id
                or case.source.sample_id != first.source.sample_id
                or case.curation.status != first.curation.status
                or _without_provenance(case.context)
                != _without_provenance(first.context)
            ):
                raise CaseValidationError(
                    f"comparison_group {group_id!r} members must share paper, "
                    "sample, stage, status, and context"
                )
        actions = [_without_provenance(case.action) for case in members]
        if len({json.dumps(action, sort_keys=True) for action in actions}) != len(
            actions
        ):
            raise CaseValidationError(
                f"comparison_group {group_id!r} members must have distinct actions"
            )


def _without_provenance(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=True)
    if isinstance(value, dict):
        return {
            key: _without_provenance(child)
            for key, child in value.items()
            if key not in {"evidence_span_ids", "derivation"}
        }
    if isinstance(value, (list, tuple)):
        return tuple(_without_provenance(child) for child in value)
    return value


__all__ = [
    "CASE_SCHEMA_VERSION",
    "CASE_STAGES",
    "CaseLibrary",
    "CaseValidationError",
    "load_case_library",
]
