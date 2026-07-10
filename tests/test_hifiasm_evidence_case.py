"""Tests for source-grounded hifiasm EvidenceCase validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app.domains.assembly.evidence_case import validate_hifiasm_evidence_cases
from app.knowledge.evidence_cases import EvidenceCaseValidationError
from app.knowledge.packs import load_knowledge_pack


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

    validated = validate_hifiasm_evidence_cases([case], pack_root=tmp_path)

    assert len(validated) == 1
    assert {span["span_id"] for span in validated[0]["source_excerpts"]} == {
        "body.genome",
        "methods.reads",
        "methods.command",
    }
    assert all(set(span) == {"span_id", "document", "quote"} for span in validated[0]["source_excerpts"])
    assert "Study body" not in str(validated[0]["source_excerpts"])
    assert validated[0]["retrieval_eligible"] is True
    assert "source_documents" not in validated[0]


def test_approved_case_rejects_checksum_mismatch(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["methods"]["sha256"] = "0" * 64

    with pytest.raises(EvidenceCaseValidationError, match="checksum mismatch"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_requires_source_hashes(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["body"]["sha256"] = None

    with pytest.raises(EvidenceCaseValidationError, match="sha256 is required"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_ambiguous_quote(tmp_path):
    case = _approved_case(tmp_path)
    methods_path = tmp_path / "sources" / "case_001" / "methods.md"
    quote = case["source_documents"]["spans"][1]["quote"]
    methods_path.write_text(f"{quote}\n{quote}\n" + case["source_documents"]["spans"][2]["quote"], encoding="utf-8")
    case["source_documents"]["methods"]["sha256"] = hashlib.sha256(methods_path.read_bytes()).hexdigest()

    with pytest.raises(EvidenceCaseValidationError, match="ambiguous"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_missing_quote(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["spans"][0]["quote"] = "Text absent from body."

    with pytest.raises(EvidenceCaseValidationError, match="was not found"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_duplicate_span_ids(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["spans"][1]["span_id"] = "body.genome"

    with pytest.raises(EvidenceCaseValidationError, match="duplicate span_id"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_unknown_referenced_span(tmp_path):
    case = _approved_case(tmp_path)
    case["observed_facts"]["coverage"]["evidence_span_ids"] = ["methods.unknown"]

    with pytest.raises(EvidenceCaseValidationError, match="methods.unknown"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_numeric_fact_requires_at_least_one_evidence_span(tmp_path):
    case = _approved_case(tmp_path)
    case["observed_facts"]["coverage"].pop("evidence_span_ids")

    with pytest.raises(EvidenceCaseValidationError, match="numeric observed_facts.coverage"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_path_traversal(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["body"]["path"] = "../outside.md"

    with pytest.raises(EvidenceCaseValidationError, match="escapes knowledge-pack root"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_absolute_source_path(tmp_path):
    case = _approved_case(tmp_path)
    case["source_documents"]["body"]["path"] = str(
        (tmp_path / "sources" / "case_001" / "body.md").resolve()
    )

    with pytest.raises(EvidenceCaseValidationError, match="escapes knowledge-pack root"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_approved_case_rejects_symlink_escape(tmp_path):
    case = _approved_case(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}_outside.md"
    outside.write_text("External body.", encoding="utf-8")
    link = tmp_path / "sources" / "case_001" / "body_link.md"
    link.symlink_to(outside)
    case["source_documents"]["body"]["path"] = "sources/case_001/body_link.md"

    with pytest.raises(EvidenceCaseValidationError, match="escapes knowledge-pack root"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_draft_case_can_remain_incomplete_but_is_not_approved(tmp_path):
    case = _approved_case(tmp_path)
    case["curation"]["status"] = "draft"
    case.pop("source_documents")

    validated = validate_hifiasm_evidence_cases([case], pack_root=tmp_path)

    assert validated[0]["curation"]["status"] == "draft"
    assert validated[0]["source_excerpts"] == []
    assert validated[0]["retrieval_eligible"] is False
    assert "source_documents" not in validated[0]


def test_rejected_case_is_retained_for_audit_but_not_retrieval(tmp_path):
    case = _approved_case(tmp_path)
    case["curation"]["status"] = "rejected"
    case.pop("source_documents")

    validated = validate_hifiasm_evidence_cases([case], pack_root=tmp_path)

    assert validated[0]["curation"]["status"] == "rejected"
    assert validated[0]["retrieval_eligible"] is False


def test_initial_case_rejects_retry_only_context(tmp_path):
    case = _approved_case(tmp_path)
    case["previous_attempt"] = {"parameters": {}, "failure_mode": "under_purged"}

    with pytest.raises(EvidenceCaseValidationError, match="must not define previous_attempt"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_retry_case_requires_failure_context_and_parameter_changes(tmp_path):
    case = _approved_case(tmp_path)
    case["case_type"] = "retry"
    case["previous_attempt"] = {"parameters": {"purge_level": 2}}
    case["intervention"]["parameter_changes"] = {"purge_level": {"from": 2, "to": 1}}

    with pytest.raises(EvidenceCaseValidationError, match="failure_mode or execution_failure_class"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_retry_case_accepts_complete_change_contract(tmp_path):
    case = _approved_case(tmp_path)
    case["case_type"] = "retry"
    case["previous_attempt"] = {
        "parameters": {"purge_level": 2},
        "failure_mode": "under_purged",
        "review_metrics": {},
    }
    case["intervention"]["parameter_changes"] = {"purge_level": {"from": 2, "to": 1}}

    validated = validate_hifiasm_evidence_cases([case], pack_root=tmp_path)

    assert validated[0]["case_type"] == "retry"
    assert validated[0]["retrieval_eligible"] is True


def test_reported_parameter_must_cite_methods(tmp_path):
    case = _approved_case(tmp_path)
    case["intervention"]["parameters"]["purge_level"]["evidence_span_ids"] = ["body.genome"]

    with pytest.raises(EvidenceCaseValidationError, match="must cite methods spans"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_explicit_rationale_must_cite_methods_while_inference_remains_labeled(tmp_path):
    case = _approved_case(tmp_path)
    case["rationale_claims"] = [
        {
            "claim_id": "curator_inference",
            "statement": "Lower purge may preserve alternate sequence.",
            "explicitness": "inferred",
            "effect_area": "haplotype_preservation",
            "evidence_span_ids": ["body.genome"],
        }
    ]

    validated = validate_hifiasm_evidence_cases([case], pack_root=tmp_path)

    assert validated[0]["rationale_claims"][0]["explicitness"] == "inferred"

    case["rationale_claims"][0]["explicitness"] = "explicit"
    with pytest.raises(EvidenceCaseValidationError, match="must cite methods spans"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def test_hifiasm_prompt_contains_only_validated_excerpts_from_shortlisted_case(tmp_path):
    from app.stages.assembly.parameter_catalog import load_parameter_catalog
    from app.stages.assembly.prompt_payload import build_prompt_payload

    pack_dir = tmp_path / "parameter_recommender"
    case = _approved_case(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)
    shard = pack_dir / "cases" / "hifiasm" / "initial.yaml"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_text(yaml.safe_dump({"cases": [case]}), encoding="utf-8")
    (pack_dir / "cases.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "case_schema": "assembly_evidence_case",
                "case_files": ["cases/hifiasm/initial.yaml"],
            }
        ),
        encoding="utf-8",
    )
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


def test_hifiasm_retrieval_query_and_trace_are_attempt_scoped_artifacts(tmp_path):
    from app.stages.assembly.parameter_catalog import load_parameter_catalog
    from app.stages.assembly.prompt_payload import build_prompt_payload

    pack_dir = tmp_path / "parameter_recommender"
    case = _approved_case(pack_dir)
    _write_case_pack(
        tmp_path,
        {
            "version": 2,
            "case_schema": "assembly_evidence_case",
            "case_files": ["cases/hifiasm/initial.yaml"],
        },
        {"cases": [case]},
    )
    data_dir = tmp_path / "data"
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
        write_artifacts=True,
        session_id="session_a",
        attempt_id=2,
        data_dir=data_dir,
    )

    retrieval = payload["case_retrieval"]
    assert retrieval == payload["case_selection"]
    assert "query" not in retrieval
    assert "trace" not in retrieval
    assert retrieval["query_id"] == "session_a:parameter_attempt_2"
    assert retrieval["query_ref"].endswith("parameter_attempt_2/retrieval_query.json")
    assert retrieval["trace_ref"].endswith("parameter_attempt_2/retrieval_trace.json")

    query_path = data_dir / retrieval["query_ref"].removeprefix("data/")
    trace_path = data_dir / retrieval["trace_ref"].removeprefix("data/")
    query_artifact = json.loads(query_path.read_text(encoding="utf-8"))
    trace_artifact = json.loads(trace_path.read_text(encoding="utf-8"))
    assert query_artifact["query_id"] == retrieval["query_id"]
    assert trace_artifact["selected_case_ids"] == ["case_001"]
    assert trace_artifact["trace"][0]["feature_distances"]["coverage"]["status"] == "compared"
    artifact_text = query_path.read_text(encoding="utf-8") + trace_path.read_text(encoding="utf-8")
    assert "source_documents" not in artifact_text
    assert "Study body" not in artifact_text
    assert "Methods." not in artifact_text


def test_declared_unsupported_hifiasm_schema_version_is_rejected(tmp_path):
    case = _approved_case(tmp_path)
    case["schema_version"] = 3

    with pytest.raises(EvidenceCaseValidationError, match="unsupported hifiasm schema_version"):
        validate_hifiasm_evidence_cases([case], pack_root=tmp_path)


def _write_case_pack(tmp_path: Path, manifest: dict, shard: object = None) -> None:
    pack_dir = tmp_path / "parameter_recommender"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "cases.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (pack_dir / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    (pack_dir / "SKILL.md").write_text("Use curated evidence.\n", encoding="utf-8")
    if shard is not None:
        shard_path = pack_dir / "cases" / "hifiasm" / "initial.yaml"
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_text(yaml.safe_dump(shard), encoding="utf-8")


def test_evidence_manifest_and_shard_load_as_one_case_collection(tmp_path):
    manifest = {
        "version": 2,
        "case_schema": "assembly_evidence_case",
        "case_files": ["cases/hifiasm/initial.yaml"],
    }
    _write_case_pack(tmp_path, manifest, {"cases": [{"case_id": "case_from_shard"}]})

    pack = load_knowledge_pack("parameter_recommender", root=tmp_path)

    assert pack["cases"] == [{"case_id": "case_from_shard"}]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 3, "Unsupported evidence manifest version"),
        ("case_schema", "unknown_schema", "Unsupported case_schema"),
        ("case_files", "cases/hifiasm/initial.yaml", "case_files must be a list"),
        ("case_files", [], "case_files must be a list"),
    ],
)
def test_evidence_manifest_rejects_invalid_contract(tmp_path, field, value, message):
    manifest = {
        "version": 2,
        "case_schema": "assembly_evidence_case",
        "case_files": ["cases/hifiasm/initial.yaml"],
    }
    manifest[field] = value
    _write_case_pack(tmp_path, manifest, {"cases": []})

    with pytest.raises((TypeError, ValueError), match=message):
        load_knowledge_pack("parameter_recommender", root=tmp_path)


def test_evidence_manifest_rejects_malformed_shard_cases(tmp_path):
    manifest = {
        "version": 2,
        "case_schema": "assembly_evidence_case",
        "case_files": ["cases/hifiasm/initial.yaml"],
    }
    _write_case_pack(tmp_path, manifest, {"cases": {"case_id": "not-a-list"}})

    with pytest.raises(TypeError, match="cases must be a list"):
        load_knowledge_pack("parameter_recommender", root=tmp_path)


def test_evidence_manifest_rejects_shard_path_escape(tmp_path):
    manifest = {
        "version": 2,
        "case_schema": "assembly_evidence_case",
        "case_files": ["../outside.yaml"],
    }
    _write_case_pack(tmp_path, manifest)

    with pytest.raises(ValueError, match="escapes pack root"):
        load_knowledge_pack("parameter_recommender", root=tmp_path)
