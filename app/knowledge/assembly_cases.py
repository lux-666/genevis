"""Single, source-method-preserving exchange for v5-derived Assembly cases."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
    with_config,
)
from typing_extensions import TypedDict

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CORPUS_BYTES = 64 * 1024 * 1024


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Observation(Record):
    name: str
    value: float
    unit: str
    object_id: str
    platform: str | None = None
    state: str = "unknown"
    origin: Literal["reported", "derived"] = "reported"
    basis: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("observation must be finite")
        return value


class SourceMethod(Record):
    tool: str = Field(min_length=1)
    version: str | None = None
    mode: str | None = None
    settings: tuple[str, ...] = ()


@with_config(ConfigDict(extra="forbid"))
class CaseSample(TypedDict):
    name: str
    context: list[str]


@with_config(ConfigDict(extra="forbid"))
class CaseInput(TypedDict, total=False):
    role: str
    material: str
    use: str


@with_config(ConfigDict(extra="forbid"))
class CaseMeasurement(TypedDict, total=False):
    metric: str
    value: str
    objects: list[str]
    basis: str
    context: list[str]
    assessment: str
    evidence: list[str]
    attribution: Literal[
        "direct_output", "downstream_context", "mixed_context", "unknown"
    ]


@with_config(ConfigDict(extra="forbid"))
class CaseRoute(TypedDict, total=False):
    name: str
    role: str
    selection_reason: str


@with_config(ConfigDict(extra="forbid"))
class CaseGap(TypedDict, total=False):
    kind: str
    issue: str
    evidence: list[str]
    leads: list[str]


@with_config(ConfigDict(extra="forbid"))
class CaseContent(TypedDict, total=False):
    action: str
    condition: str
    method_label: str
    samples: list[CaseSample]
    inputs: list[CaseInput]
    outputs: list[str]
    reported_measurements: list[CaseMeasurement]
    normalization_notes: dict[str, str]
    sample_context: str
    outcomes: list[CaseMeasurement]
    routes: list[CaseRoute]
    gaps: list[CaseGap]
    notes: list[str]
    evidence: list[str]
    source_limits: list[str]


class AssemblyCase(Record):
    case_id: str = Field(min_length=1)
    configuration_id: str = Field(min_length=1)
    task: Literal["contig_assembly"] = "contig_assembly"
    method: SourceMethod
    observations: tuple[Observation, ...] = ()
    input_platforms: tuple[str, ...] = ()
    execution_status: Literal["performed", "described_only", "unclear"]
    route_roles: tuple[str, ...] = ()
    content: CaseContent


class CasePaper(Record):
    schema_version: Literal["recommendation-cases.v1"]
    paper_id: str = Field(min_length=1)
    paper: dict[str, str]
    evidence_scope: str = Field(min_length=1)
    independence_group: str | None = None
    cases: tuple[AssemblyCase, ...]

    @model_validator(mode="after")
    def unique_cases(self) -> CasePaper:
        ids = [c.case_id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case identity")
        if any(not c.case_id.startswith(self.paper_id + ":") for c in self.cases):
            raise ValueError("case identity must be namespaced by paper")
        return self


class AssemblyKnowledge(Record):
    papers: tuple[CasePaper, ...] = ()
    software: dict[str, Any]

    @model_validator(mode="after")
    def unique_papers(self) -> AssemblyKnowledge:
        ids = [p.paper_id for p in self.papers]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate paper identity")
        return self


def load_case_papers(root: str | Path | None) -> tuple[CasePaper, ...]:
    if root is None:
        return ()
    folder = Path(root).resolve(strict=True)
    if not folder.is_dir():
        raise ValueError("case source must be a directory")
    total = 0
    papers = []
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValueError("case files must be regular files, not symlinks")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("case file exceeds size budget")
        payload = path.read_bytes()
        total += len(payload)
        if len(payload) > MAX_FILE_BYTES or total > MAX_CORPUS_BYTES:
            raise ValueError("case corpus exceeds size budget")
        papers.append(CasePaper.model_validate_json(payload))
    return tuple(papers)


def canonical_tool(name: str) -> str:
    return "".join(c for c in name.casefold() if c.isalnum())


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
