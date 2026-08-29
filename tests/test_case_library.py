from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.knowledge.cases import (
    MAX_COMPARISON_ARMS,
    CaseValidationError,
    load_case_library,
)

SOURCE_TEXT = (
    "The sample genome size was 1.0 Gb.\n"
    "Hifiasm and Verkko were run with the reported parameters.\n"
    "The final assembly QV was 47.\n"
)


def _case(
    *,
    arm: str = "hifiasm",
    tool: str = "hifiasm",
    status: str = "approved",
) -> dict:
    return {
        "schema_version": 1,
        "case_id": f"paper_001.sample_a.assembly.{arm}",
        "stage": "assembly",
        "source": {
            "paper_id": "paper_001",
            "sample_id": "sample_a",
            "documents": [
                {
                    "document_id": "paper",
                    "kind": "paper",
                    "path": "sources/paper_001/paper.md",
                }
            ],
            "evidence_spans": [
                {
                    "span_id": "genome",
                    "document_id": "paper",
                    "quote": "The sample genome size was 1.0 Gb.",
                },
                {
                    "span_id": "method",
                    "document_id": "paper",
                    "quote": (
                        "Hifiasm and Verkko were run with the reported parameters."
                    ),
                },
                {
                    "span_id": "qv",
                    "document_id": "paper",
                    "quote": "The final assembly QV was 47.",
                },
            ],
        },
        "context": {
            "subject": {
                "scientific_name": "Epinephelus awoara",
                "genome_size": {
                    "value": 1_000_000_000,
                    "unit": "bp",
                    "evidence_span_ids": ["genome"],
                    "derivation": "1.0 Gb -> 1000000000 bp",
                },
            },
            "inputs": [
                {
                    "data_type": "long_reads",
                    "platform": "pacbio_hifi",
                    "role": "primary",
                }
            ],
        },
        "action": {
            "method": {
                "tool_id": tool,
                "evidence_span_ids": ["method"],
            },
            "parameters": [
                {
                    "name": "purge_similarity",
                    "reported_name": "-s",
                    "value": 0.4,
                    "evidence_span_ids": ["method"],
                }
            ],
        },
        "outcome": {
            "qv": {
                "value": 47,
                "unit": "phred",
                "evidence_span_ids": ["qv"],
            }
        },
        "curation": {
            "status": status,
            **(
                {"curator": "curator", "reviewed_at": "2026-08-29"}
                if status == "approved"
                else {}
            ),
        },
    }


def _write_library(
    tmp_path: Path,
    cases: list[dict],
    *,
    filenames: list[str] | None = None,
) -> Path:
    root = tmp_path / "stage_cases"
    root.mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        "schema_version: 1\nstages: [assembly, hic_scaffolding, t2t]\n",
        encoding="utf-8",
    )
    for case in cases:
        for document in case["source"]["documents"]:
            path = root / document["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SOURCE_TEXT, encoding="utf-8")
    for index, case in enumerate(cases):
        stage_dir = root / "cases" / case["stage"]
        stage_dir.mkdir(parents=True, exist_ok=True)
        filename = filenames[index] if filenames else f"{case['case_id']}.yaml"
        (stage_dir / filename).write_text(
            yaml.safe_dump(case, sort_keys=False), encoding="utf-8"
        )
    return root


def test_all_three_stage_models_load_as_drafts(tmp_path: Path) -> None:
    assembly = _case(status="draft")
    hic = deepcopy(assembly)
    hic.update(
        case_id="paper_001.sample_a.hic_scaffolding.autohic",
        stage="hic_scaffolding",
        context={
            "input_assembly": {},
            "hic": [
                {
                    "data_type": "hic_reads",
                    "platform": "illumina",
                    "subtype": "hic",
                    "role": "primary",
                }
            ],
        },
        outcome={},
    )
    t2t = deepcopy(assembly)
    t2t.update(
        case_id="paper_001.sample_a.t2t.minimap2",
        stage="t2t",
        context={"input_assembly": {}, "telomere": {}, "inputs": []},
        outcome={},
    )

    library = load_case_library(_write_library(tmp_path, [assembly, hic, t2t]))

    assert [case.stage for case in library] == [
        "assembly",
        "hic_scaffolding",
        "t2t",
    ]


def test_loads_valid_comparison_group_as_immutable_library(tmp_path: Path) -> None:
    first = _case()
    second = _case(arm="verkko", tool="verkko")
    for case in (first, second):
        case["comparison_group"] = {
            "group_id": "paper_001.sample_a.assembly.primary",
            "arm_id": case["case_id"].rsplit(".", 1)[-1],
        }
    library = load_case_library(_write_library(tmp_path, [first, second]))

    assert [case.case_id for case in library] == [
        "paper_001.sample_a.assembly.hifiasm",
        "paper_001.sample_a.assembly.verkko",
    ]
    assert isinstance(library.records, tuple)
    assert isinstance(library.records[0].context.inputs, tuple)
    with pytest.raises(FrozenInstanceError):
        library.records += ()
    with pytest.raises(ValidationError):
        library.records[0].case_id = "changed"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda case: case["outcome"]["qv"].update(unit="bp"),
            "phred",
        ),
        (
            lambda case: case["context"]["inputs"][0].update(platform="nova"),
            "platform",
        ),
        (
            lambda case: case["context"]["subject"].update(
                scientific_name="not normalized"
            ),
            "scientific_name",
        ),
        (
            lambda case: case["action"]["method"].update(evidence_span_ids=["missing"]),
            "unknown source span",
        ),
    ],
)
def test_rejects_invalid_typed_or_sourced_fields(
    tmp_path: Path, mutation, message: str
) -> None:
    case = _case()
    mutation(case)

    with pytest.raises(CaseValidationError, match=message):
        load_case_library(_write_library(tmp_path, [case]))


def test_rejects_duplicate_input_key(tmp_path: Path) -> None:
    case = _case()
    case["context"]["inputs"].append(deepcopy(case["context"]["inputs"][0]))

    with pytest.raises(CaseValidationError, match="duplicate input key"):
        load_case_library(_write_library(tmp_path, [case]))


@pytest.mark.parametrize("status", ["draft", "approved"])
def test_every_declared_quote_is_checked(tmp_path: Path, status: str) -> None:
    case = _case(status=status)
    case["source"]["evidence_spans"][0]["quote"] = "Invented text."

    with pytest.raises(CaseValidationError, match="not a source substring"):
        load_case_library(_write_library(tmp_path, [case]))


def test_source_path_must_stay_in_its_paper_directory(tmp_path: Path) -> None:
    case = _case()
    case["source"]["documents"][0]["path"] = "sources/other/paper.md"

    with pytest.raises(CaseValidationError, match="inside sources/paper_001"):
        load_case_library(_write_library(tmp_path, [case]))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda case: case.update(case_id="paper_001.sample_a.assembly"),
            "case_id must be",
        ),
        (
            lambda case: case["source"].update(sample_id="sample_b"),
            "components do not match",
        ),
    ],
)
def test_case_id_must_match_source_and_layout(
    tmp_path: Path, mutate, message: str
) -> None:
    case = _case()
    mutate(case)

    with pytest.raises(CaseValidationError, match=message):
        load_case_library(_write_library(tmp_path, [case]))


def test_filename_must_equal_case_id(tmp_path: Path) -> None:
    case = _case()

    with pytest.raises(CaseValidationError, match="filename must equal"):
        load_case_library(_write_library(tmp_path, [case], filenames=["wrong.yaml"]))


def test_approved_case_requires_direct_outcome_and_review(tmp_path: Path) -> None:
    no_outcome = _case()
    no_outcome["outcome"] = {}
    with pytest.raises(CaseValidationError, match="requires an outcome"):
        load_case_library(_write_library(tmp_path / "outcome", [no_outcome]))

    no_review = _case()
    no_review["curation"].pop("curator")
    with pytest.raises(CaseValidationError, match="requires curator"):
        load_case_library(_write_library(tmp_path / "review", [no_review]))


@pytest.mark.parametrize("drift", ["context", "status"])
def test_comparison_group_rejects_drift(tmp_path: Path, drift: str) -> None:
    first = _case()
    second = _case(arm="verkko", tool="verkko")
    for case in (first, second):
        case["comparison_group"] = {
            "group_id": "group",
            "arm_id": case["case_id"].rsplit(".", 1)[-1],
        }
    if drift == "context":
        second["context"]["subject"]["scientific_name"] = "Gadus morhua"
    else:
        second["curation"] = {"status": "draft"}

    with pytest.raises(CaseValidationError, match="share paper, sample, stage, status"):
        load_case_library(_write_library(tmp_path, [first, second]))


def test_draft_comparison_group_may_lack_outcomes(tmp_path: Path) -> None:
    first = _case(status="draft")
    second = _case(arm="verkko", tool="verkko", status="draft")
    for case in (first, second):
        case["outcome"] = {}
        case["comparison_group"] = {
            "group_id": "group",
            "arm_id": case["case_id"].rsplit(".", 1)[-1],
        }

    assert len(load_case_library(_write_library(tmp_path, [first, second]))) == 2


def test_comparison_group_enforces_arm_limit(tmp_path: Path) -> None:
    cases = []
    for index in range(MAX_COMPARISON_ARMS + 1):
        case = _case(arm=f"arm_{index}", tool=f"tool_{index}")
        case["comparison_group"] = {"group_id": "group", "arm_id": f"arm_{index}"}
        cases.append(case)

    with pytest.raises(CaseValidationError, match="must contain 2-8 arms"):
        load_case_library(_write_library(tmp_path, cases))


def test_runtime_method_must_exist_in_same_stage(tmp_path: Path) -> None:
    case = _case()
    case["action"]["method"]["runtime_method_id"] = "t2t.alignment.minimap2"

    with pytest.raises(CaseValidationError, match="registered assembly method"):
        load_case_library(_write_library(tmp_path, [case]))


def test_repository_library_contract() -> None:
    library = load_case_library()

    assert len(library) == 0
