"""Tests for source-grounded hifiasm EvidenceCase V2 validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.domains.assembly.evidence_case import validate_hifiasm_v2_cases
from app.knowledge.evidence_cases import EvidenceCaseValidationError


def _write_document(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _approved_case(pack_root: Path) -> dict:
    body_quote = "The diploid genome size was estimated as 1200 Mb."
    methods_context = "HiFi coverage was 30.0x with a read N50 of 18.0 kb."
    methods_command = "We ran hifiasm with purge level 1."
    body_hash = _write_document(
        pack_root / "sources" / "case_001" / "body.md",
        f"Study body.\n\n{body_quote}\n\nAssembly results followed.\n",
    )
    methods_hash = _write_document(
        pack_root / "sources" / "case_001" / "methods.md",
        f"Methods.\n\n{methods_context}\n\n{methods_command}\n",
    )
    return {
        "schema_version": 2,
        "case_id": "case_001",
        "case_type": "initial",
        "source": {"kind": "paper", "title": "Example", "doi": "10.example/case"},
        "applicability": {
            "read_type": "hifi",
            "source_tool": "hifiasm",
            "target_methods": ["assembly.candidate.hifiasm"],
            "assembly_goal": "primary",
        },
        "observed_facts": {
            "genome_size_mb": {"value": 1200, "evidence_span_ids": ["body.genome"]},
            "coverage": {"value": 30.0, "evidence_span_ids": ["methods.reads"]},
            "read_n50_kb": {"value": 18.0, "evidence_span_ids": ["methods.reads"]},
            "ploidy": {"value": 2, "evidence_span_ids": ["body.genome"]},
        },
        "intervention": {
            "tool": "hifiasm",
            "version": "0.21.0",
            "parameters": {
                "purge_level": {
                    "value": 1,
                    "reported": True,
                    "evidence_span_ids": ["methods.command"],
                }
            },
        },
        "rationale_claims": [],
        "outcomes": {},
        "source_documents": {
            "body": {"path": "sources/case_001/body.md", "sha256": body_hash},
            "methods": {"path": "sources/case_001/methods.md", "sha256": methods_hash},
            "spans": [
                {"span_id": "body.genome", "document": "body", "quote": body_quote},
                {"span_id": "methods.reads", "document": "methods", "quote": methods_context},
                {"span_id": "methods.command", "document": "methods", "quote": methods_command},
            ],
        },
        "curation": {"status": "approved", "curator": "tester"},
    }


def test_approved_case_validates_hashes_and_returns_exact_excerpts_only(tmp_path):
    case = _approved_case(tmp_path)

    validated = validate_hifiasm_v2_cases([case], pack_root=tmp_path)

    assert len(validated) == 1
    assert {span["span_id"] for span in validated[0]["source_excerpts"]} == {
        "body.genome",
        "methods.reads",
        "methods.command",
    }
    assert all(set(span) == {"span_id", "document", "quote"} for span in validated[0]["source_excerpts"])
    assert "Study body" not in str(validated[0]["source_excerpts"])


def test_approved_case_rejects_checksum_mismatch(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["methods"]["sha256"] = "0" * 64

    with pytest.raises(EvidenceCaseValidationError, match="checksum mismatch"):
        validate_hifiasm_v2_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_ambiguous_quote(tmp_path):
    case = _approved_case(tmp_path)
    methods_path = tmp_path / "sources" / "case_001" / "methods.md"
    quote = case["source_documents"]["spans"][1]["quote"]
    methods_path.write_text(f"{quote}\n{quote}\n" + case["source_documents"]["spans"][2]["quote"], encoding="utf-8")
    case["source_documents"]["methods"]["sha256"] = hashlib.sha256(methods_path.read_bytes()).hexdigest()

    with pytest.raises(EvidenceCaseValidationError, match="ambiguous"):
        validate_hifiasm_v2_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_path_traversal(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["body"]["path"] = "../outside.md"

    with pytest.raises(EvidenceCaseValidationError, match="escapes knowledge-pack root"):
        validate_hifiasm_v2_cases([case], pack_root=tmp_path)


def test_draft_case_can_remain_incomplete_but_is_not_approved(tmp_path):
    case = _approved_case(tmp_path)
    case["curation"]["status"] = "draft"
    case.pop("source_documents")

    validated = validate_hifiasm_v2_cases([case], pack_root=tmp_path)

    assert validated[0]["curation"]["status"] == "draft"
    assert validated[0]["source_excerpts"] == []


def test_hifiasm_prompt_contains_only_validated_excerpts_from_shortlisted_case(tmp_path):
    import yaml

    from app.stages.assembly.parameter_catalog import load_parameter_catalog
    from app.stages.assembly.prompt_payload import build_prompt_payload

    pack_dir = tmp_path / "parameter_recommender"
    case = _approved_case(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "cases.yaml").write_text(yaml.safe_dump({"version": 2, "cases": [case]}), encoding="utf-8")
    (pack_dir / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    (pack_dir / "SKILL.md").write_text("Recommend only L2 parameters; cases are optional.\n", encoding="utf-8")

    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=load_parameter_catalog("hifiasm"),
        full_profile={
            "sample": {"sample_id": "case_match", "ploidy": 2},
            "assets": {"read_type": "hifi"},
            "read_qc": {"effective_coverage_1kb": 30.0, "read_n50": 18000},
            "genome_survey": {"estimated_genome_size": 1200000000},
            "previous_attempt": {},
            "assembly_review": {},
        },
        task="recommend_initial",
        knowledge_root=tmp_path,
    )

    assert payload["selected_cases"] == []
    assert payload["case_retrieval"]["selected_case_ids"] == ["case_001"]
    assert [case["case_id"] for case in payload["evidence_cases"]] == ["case_001"]
    evidence_text = str(payload["evidence_cases"][0])
    assert "HiFi coverage was 30.0x" in evidence_text
    assert "Study body" not in evidence_text
    assert "source_documents" not in payload["evidence_cases"][0]


def test_declared_hifiasm_schema_version_other_than_v2_is_rejected(tmp_path):
    case = _approved_case(tmp_path)
    case["schema_version"] = 3

    with pytest.raises(EvidenceCaseValidationError, match="unsupported hifiasm schema_version"):
        validate_hifiasm_v2_cases([case], pack_root=tmp_path)
