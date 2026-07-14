"""Tests for prompt payload builder and knowledge pack integration (Phase 11).

NOTE: This test suite uses pytest fixtures and MUST be run with pytest:
    pytest tests/test_phase11_prompt_payload_builder.py -v
Running with unittest will give false-positive (0 tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.domains.assembly import numeric_case_retriever as numeric_retriever_module
from app.domains.assembly.evidence_case import (
    _rank_hifiasm_prompt_case,
    project_hifiasm_prompt_safe_case,
    validate_hifiasm_evidence_cases,
)
from app.knowledge.evidence_cases import EvidenceCaseValidationError
from app.knowledge.packs import load_knowledge_pack
from app.schemas.parameter_catalog import MethodParameterCatalog, ParameterSpec
from app.schemas.artifacts import file_sha256
from app.stages.assembly import prompt_payload as prompt_payload_module
from app.stages.assembly.prompt_payload import (
    ParameterPromptTask,
    _load_initial_prompt_snapshot,
    build_prompt_payload,
    generate_payload_ref,
    generate_profile_ref,
    write_payload_artifact,
)


@pytest.mark.parametrize(
    "path_ref",
    [
        "data/artifacts/assembly/other/parameter_attempt_0/prompt_payload.json",
        "data/artifacts/assembly/s1/parameter_attempt_1/prompt_payload.json",
    ],
)
def test_attempt_zero_prompt_loader_rejects_cross_session_or_nonzero_substitution(
    tmp_path, path_ref
):
    with pytest.raises(ValueError, match="current session attempt 0 path"):
        _load_initial_prompt_snapshot(
            {"prompt_payload_ref": {"path_ref": path_ref, "sha256": "0" * 64}},
            data_dir=tmp_path,
            session_id="s1",
        )


_HIFIASM_SOURCE_DIR = tempfile.TemporaryDirectory(prefix="phase11_prompt_hifiasm_")
_HIFIASM_SOURCE_ROOT = Path(_HIFIASM_SOURCE_DIR.name)


def _legal_hifiasm_case(case_id: str, *, quote: str, distance: float) -> dict:
    span_id = f"methods.{case_id}"
    source_path = _HIFIASM_SOURCE_ROOT / f"{case_id}.methods.md"
    source_path.write_text(
        "Methods context; YAML evidence is curated independently.\n",
        encoding="utf-8",
    )
    validated = validate_hifiasm_evidence_cases(
        [
            {
                "schema_version": 3,
                "case_id": case_id,
                "case_type": "initial",
                "source": {"kind": "paper", "title": f"Example {case_id}"},
                "applicability": {
                    "read_type": "hifi",
                    "source_tool": "hifiasm",
                    "target_methods": ["hifiasm"],
                },
                "observed_facts": {
                    "coverage": {"value": 30, "evidence_span_ids": [span_id]},
                },
                "intervention": {"tool": "hifiasm", "parameters": {}},
                "rationale_claims": [],
                "outcomes": {},
                "curation": {"status": "approved", "curator": "test"},
                "methods_path": source_path.name,
                "evidence_spans": [{"span_id": span_id, "quote": quote}],
            }
        ],
        pack_root=_HIFIASM_SOURCE_ROOT,
    )[0]
    return _rank_hifiasm_prompt_case(
        validated,
        numeric_match={"distance": distance},
    )


def _attested_numeric_selection(cases: list[dict], *, query_id: str) -> dict:
    case_ids = [case["case_id"] for case in cases]
    trace = [
        {
            "case_id": case["case_id"],
            "decision": "candidate",
            "hard_filter_reasons": [],
            **case["numeric_match"],
        }
        for case in cases
    ]
    selection = {
        "retrieval_backend": "hifiasm_numeric",
        "selected_case_ids": case_ids,
        "candidate_case_ids": case_ids,
        "selected_count": len(case_ids),
        "eligible_case_count": len(case_ids),
        "total_cases": len(case_ids),
        "evidence_available": bool(case_ids),
        "case_evidence_required": False,
        "warning": None,
        "query": {"query_id": query_id, "method_id": "hifiasm"},
        "trace": trace,
    }
    selection[numeric_retriever_module._RANKED_SELECTION_FIELD] = (
        numeric_retriever_module._attest_hifiasm_ranked_selection(
            selected_cases=cases,
            metadata=selection,
            trace=trace,
        )
    )
    return selection


# Test fixtures
@pytest.fixture
def sample_catalog() -> MethodParameterCatalog:
    """Create a sample parameter catalog with L0/L1/L2 parameters."""
    return MethodParameterCatalog(
        method_id="test_method",
        tool="test_tool",
        read_type="hifi",
        parameters={
            # L0 - should NOT appear in prompt or payload
            "output_dir": ParameterSpec(
                cli="--output",
                level=0,
                source="execution_context",
                value_type="path",
                fixed_value="/tmp/output",
            ),
            "input_file": ParameterSpec(
                cli="--input",
                level=0,
                source="assembly_out.input_ref",
                value_type="path",
            ),
            # L1 - should be in readonly_params only
            "read_type": ParameterSpec(
                cli=None,
                level=1,
                source="assembly_out.assembly_route",
                value_type="string",
                description="Read technology type",
            ),
            "genome_size_hint": ParameterSpec(
                cli=None,
                level=1,
                source="genome_survey.estimated_genome_size",
                value_type="int",
                description="Estimated genome size from survey",
            ),
            # L2 - should be in adjustable_params
            "threads": ParameterSpec(
                cli="--threads",
                level=2,
                source="llm_recommended",
                value_type="int",
                min=1,
                max=128,
                default=16,
                description="Number of threads",
            ),
            "kmer_size": ParameterSpec(
                cli="-k",
                level=2,
                source="llm_recommended",
                value_type="int",
                allowed_values=[17, 19, 21, 31, 41, 51],
                default=31,
                description="K-mer size for assembly graph",
            ),
            "purge_level": ParameterSpec(
                cli="--purge",
                level=2,
                source="llm_recommended",
                value_type="int",
                min=0,
                max=3,
                default=1,
                description="Purge duplication level",
            ),
        },
    )


@pytest.fixture
def sample_full_profile() -> dict:
    """Create a sample full profile."""
    return {
        "sample": {
            "sample_id": "test_sample_001",
            "organism": "Homo sapiens",
            "taxon_id": 9606,
            "clade": "vertebrate_mammalian",
            "ploidy": 2,
            "expected_chromosome_count": 23,
            "target_level": "chromosome",
        },
        "assets": {
            "read_type": "hifi",
            "read_format": "fastq",
            "has_hifi": True,
            "has_ont": False,
            "has_ultra_long": False,
            "has_hic": True,
            "has_trio": False,
            "has_rnaseq": True,
        },
        "read_qc": {
            "read_count": 5000000,
            "total_bases": 90000000000,
            "read_n50": 15000,
            "read_mean": 18000,
            "effective_coverage_1kb": 60,
            "mean_quality": 30,
        },
        "genome_survey": {
            "estimated_genome_size": 3100000000,
            "heterozygosity": 0.001,
            "repeat_content": 0.45,
            "kmer_peak_coverage": 55,
        },
        "previous_attempt": {},
        "assembly_review": {},
    }


class TestLevelSegregation:
    """Test that L0/L1/L2 parameters are properly segregated."""

    def test_l0_params_not_in_prompt_profile(self, sample_catalog, sample_full_profile):
        """L0 parameters should be excluded from prompt_profile."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # L0 params should not appear in the lean L1/L2 prompt surfaces
        all_params_in_subset = {
            **payload["readonly_params"],
            **payload["adjustable_params"],
        }
        assert "output_dir" not in all_params_in_subset
        assert "input_file" not in all_params_in_subset

        # L0 should not be in readonly or adjustable
        assert "output_dir" not in payload["readonly_params"]
        assert "output_dir" not in payload["adjustable_params"]
        assert "input_file" not in payload["readonly_params"]
        assert "input_file" not in payload["adjustable_params"]

    def test_l1_params_readonly_in_payload(self, sample_catalog, sample_full_profile):
        """L1 parameters should be in readonly_params, not adjustable."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # L1 params in readonly
        assert "read_type" in payload["readonly_params"]
        assert "genome_size_hint" in payload["readonly_params"]
        assert payload["readonly_params"]["read_type"]["level"] == 1
        assert payload["readonly_params"]["genome_size_hint"]["level"] == 1

        # L1 params NOT in adjustable
        assert "read_type" not in payload["adjustable_params"]
        assert "genome_size_hint" not in payload["adjustable_params"]

    def test_l2_params_adjustable_in_payload(self, sample_catalog, sample_full_profile):
        """L2 parameters should be in adjustable_params, not readonly."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # L2 params in adjustable
        assert "threads" in payload["adjustable_params"]
        assert "kmer_size" in payload["adjustable_params"]
        assert "purge_level" in payload["adjustable_params"]
        assert payload["adjustable_params"]["threads"]["level"] == 2
        assert payload["adjustable_params"]["kmer_size"]["level"] == 2

        # L2 params NOT in readonly
        assert "threads" not in payload["readonly_params"]
        assert "kmer_size" not in payload["readonly_params"]

    def test_catalog_subset_only_l1_l2(self, sample_catalog, sample_full_profile):
        """Lean sealed prompt exposes only L1 and L2 parameter surfaces."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        all_subset_params = {
            **payload["readonly_params"],
            **payload["adjustable_params"],
        }
        assert "catalog_subset" not in payload

        # Check all params in subset are L1 or L2
        for key, spec in all_subset_params.items():
            assert spec["level"] in [1, 2], f"Parameter {key} has level {spec['level']}, expected 1 or 2"

        # Verify counts
        assert len(payload["readonly_params"]) == 2  # read_type, genome_size_hint
        assert len(payload["adjustable_params"]) == 3  # threads, kmer_size, purge_level


class TestTaskScopedSlicing:
    """Test that prompt_profile is correctly sliced by task type."""

    def test_recommend_initial_profile_fields(self, sample_catalog, sample_full_profile):
        """recommend_initial should include sample, assets, read_qc, genome_survey."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        profile = payload["prompt_profile"]
        assert "sample" in profile
        assert "assets" in profile
        assert "read_qc" in profile
        assert "genome_survey" in profile
        assert "previous_attempt" not in profile
        assert "assembly_review" not in profile

        # Sample should be filtered to initial fields
        assert "sample_id" in profile["sample"]
        assert "organism" in profile["sample"]
        assert "ploidy" in profile["sample"]

    def test_retry_after_execution_failure_profile(self, sample_catalog, sample_full_profile):
        """retry_after_execution_failure should include previous_attempt execution fields."""
        full_profile_with_attempt = {
            **sample_full_profile,
            "previous_attempt": {
                "attempt_id": "attempt_001",
                "method_id": "hifiasm",
                "exit_code": 137,
                "failure_class": "memory_exhausted",
                "stderr_tail": "oom-kill",
                "stdout_tail": "loaded reads",
                "params": {"threads": 64},
            },
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=full_profile_with_attempt,
            task="retry_after_execution_failure",
        )

        profile = payload["prompt_profile"]
        assert "sample" in profile
        assert "assets" in profile
        assert "previous_attempt" in profile
        assert "read_qc" not in profile
        assert "genome_survey" not in profile

        # Previous attempt should have execution fields
        attempt = profile["previous_attempt"]
        assert "attempt_id" in attempt
        assert "exit_code" in attempt
        assert "failure_class" in attempt
        assert "stderr_tail" not in attempt
        assert "stdout_tail" not in attempt

    def test_retry_after_review_failure_profile(self, sample_catalog, sample_full_profile):
        """retry_after_review_failure should include assembly_review metrics."""
        full_profile_with_review = {
            **sample_full_profile,
            "previous_attempt": {
                "attempt_id": "attempt_002",
                "method_id": "hifiasm",
                "stderr_tail": "ignored failure text",
                "stdout_tail": "ignored progress text",
                "params": {"kmer_size": 51},
            },
            "assembly_review": {
                "assembly_size": 3000000000,
                "contig_count": 500,
                "contig_n50": 1000000,
                "busco_complete": 92.5,
            },
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=full_profile_with_review,
            task="retry_after_review_failure",
        )

        profile = payload["prompt_profile"]
        assert "sample" in profile
        assert "previous_attempt" in profile
        assert "assembly_review" in profile
        assert "assets" not in profile
        assert "read_qc" not in profile
        assert "stderr_tail" not in profile["previous_attempt"]
        assert "stdout_tail" not in profile["previous_attempt"]

        # Assembly review should have metrics
        review = profile["assembly_review"]
        assert "contig_count" in review
        assert "contig_n50" in review
        assert "busco_complete" in review


class TestKnowledgePackLoading:
    """Test knowledge pack loading from filesystem."""

    def test_load_rules_yaml(self):
        """rules.yaml should load and contain rules list."""
        pack = load_knowledge_pack("parameter_recommender")
        assert "rules" in pack
        assert isinstance(pack["rules"], dict)
        assert "rules" in pack["rules"]
        assert isinstance(pack["rules"]["rules"], list)
        assert len(pack["rules"]["rules"]) > 0

        # Check first rule structure
        rule = pack["rules"]["rules"][0]
        assert "rule_id" in rule
        assert "condition" in rule
        assert "constraints" in rule
        assert "reason" in rule

    def test_load_cases_yaml(self):
        """cases.yaml should load with all required fields."""
        pack = load_knowledge_pack("parameter_recommender")
        assert "cases" in pack
        assert isinstance(pack["cases"], list)
        assert len(pack["cases"]) > 0

        assert {
            case["case_id"]
            for case in pack["cases"]
            if case.get("schema_version") == 3
        } == {
            "hifiasm.paper.s41587-022-01261-x",
            "hifiasm.paper.s41592-024-02269-8",
            "hifiasm.paper.s41586-026-10105-6",
            "hifiasm.paper.s41586-022-05325-5",
            "hifiasm.paper.s41467-024-54188-z",
            "hifiasm.paper.s41588-025-02273-4",
            "hifiasm.paper.s41467-025-65778-w",
            "hifiasm.paper.s41467-025-65870-1",
            "hifiasm.paper.s41597-024-03664-8",
            "hifiasm.paper.s41588-025-02137-x",
        }

        # Check the methods-only EvidenceCase v3 structure.
        case = pack["cases"][0]
        assert case["schema_version"] == 3
        assert "case_id" in case
        assert "source" in case
        assert "methods_path" in case
        assert "evidence_spans" in case
        assert "applicability" in case
        assert "intervention" in case
        assert "curation" in case
        assert "source_documents" not in case

    @pytest.mark.parametrize(
        (
            "case_id",
            "doi",
            "read_type",
            "target_method",
            "expected_l2_parameters",
            "context_only_parameter_ids",
        ),
        [
            (
                "hifiasm.paper.s41587-022-01261-x",
                "10.1038/s41587-022-01261-x",
                "hifi",
                "hifiasm",
                set(),
                {"hic_r1", "hic_r2"},
            ),
            (
                "hifiasm.paper.s41592-024-02269-8",
                "10.1038/s41592-024-02269-8",
                "hifi",
                "hifiasm",
                set(),
                {"hic_r1", "hic_r2", "hom_cov", "ul_cut", "ul_reads"},
            ),
            (
                "hifiasm.paper.s41586-026-10105-6",
                "10.1038/s41586-026-10105-6",
                "ont",
                "hifiasm_ont",
                {"dual_scaffold"},
                {"ont_mode", "telomere_motif"},
            ),
            (
                "hifiasm.paper.s41586-022-05325-5",
                "10.1038/s41586-022-05325-5",
                "hifi",
                "hifiasm",
                set(),
                {"hap1_yak", "hap2_yak"},
            ),
            (
                "hifiasm.paper.s41467-024-54188-z",
                "10.1038/s41467-024-54188-z",
                "hifi",
                "hifiasm",
                set(),
                {"hap1_yak", "hap2_yak", "hic_r1", "hic_r2", "ul_reads"},
            ),
            (
                "hifiasm.paper.s41588-025-02273-4",
                "10.1038/s41588-025-02273-4",
                "hifi",
                "hifiasm",
                set(),
                {"hic_r1", "hic_r2", "ul_reads"},
            ),
            (
                "hifiasm.paper.s41467-025-65778-w",
                "10.1038/s41467-025-65778-w",
                "hifi",
                "hifiasm",
                set(),
                {"hic_r1", "hic_r2", "ul_reads"},
            ),
            (
                "hifiasm.paper.s41467-025-65870-1",
                "10.1038/s41467-025-65870-1",
                "hifi",
                "hifiasm",
                {
                    "correction_rounds",
                    "high_freq_kmer_drop",
                    "contig_bubble_pop_distance",
                    "break_low_coverage",
                    "purge_level",
                    "duplicate_overlap_min_reads",
                },
                {"hg_size", "primary_mode"},
            ),
            (
                "hifiasm.paper.s41597-024-03664-8",
                "10.1038/s41597-024-03664-8",
                "hifi",
                "hifiasm",
                set(),
                {"ul_reads"},
            ),
            (
                "hifiasm.paper.s41588-025-02137-x",
                "10.1038/s41588-025-02137-x",
                "hifi",
                "hifiasm",
                set(),
                {"ul_reads"},
            ),
        ],
    )
    def test_production_hifiasm_corpus_preserves_curated_prompt_boundary(
        self,
        sample_full_profile,
        case_id,
        doi,
        read_type,
        target_method,
        expected_l2_parameters,
        context_only_parameter_ids,
    ):
        """All ten curated shards must survive real loading and prompt projection unchanged."""

        def referenced_span_ids(value):
            references = set()
            if isinstance(value, dict):
                for field, item in value.items():
                    if field == "evidence_spans":
                        continue
                    if field == "evidence_span_ids":
                        references.update(item)
                    else:
                        references.update(referenced_span_ids(item))
            elif isinstance(value, list):
                for item in value:
                    references.update(referenced_span_ids(item))
            return references

        pack = load_knowledge_pack("parameter_recommender")
        raw_case = next(case for case in pack["cases"] if case.get("case_id") == case_id)
        validated_cases = validate_hifiasm_evidence_cases(
            pack["cases"],
            pack_root=Path(pack["pack_root"]),
        )
        validated_case = next(case for case in validated_cases if case["case_id"] == case_id)
        projected_case = project_hifiasm_prompt_safe_case(validated_case)

        assert raw_case["source"]["doi"] == doi
        assert raw_case["applicability"]["read_type"] == read_type
        assert raw_case["applicability"]["target_methods"] == [target_method]
        assert projected_case["source"]["doi"] == doi
        assert projected_case["applicability"]["read_type"] == read_type
        assert projected_case["applicability"]["target_methods"] == [target_method]

        declared_span_ids = {span["span_id"] for span in raw_case["evidence_spans"]}
        raw_reference_ids = referenced_span_ids(raw_case)
        visible_span_ids = {span["span_id"] for span in projected_case["source_excerpts"]}
        projected_reference_ids = referenced_span_ids(projected_case)
        assert raw_reference_ids == declared_span_ids
        assert visible_span_ids == declared_span_ids
        assert projected_reference_ids == visible_span_ids
        assert "methods_path" not in projected_case
        assert "evidence_spans" not in projected_case

        projected_parameters = set(projected_case["intervention"]["parameters"])
        assert projected_parameters == expected_l2_parameters
        assert projected_parameters.isdisjoint(context_only_parameter_ids)
        assert raw_case["outcomes"] == {}
        assert projected_case["outcomes"] == {}
        assert "scope" not in raw_case
        assert "attribution" not in projected_case

    def test_load_skill_md(self):
        """SKILL.md should load as string."""
        pack = load_knowledge_pack("parameter_recommender")
        assert "skill" in pack
        assert isinstance(pack["skill"], str)
        assert len(pack["skill"]) > 100  # Should be substantial content
        assert "L0" in pack["skill"]
        assert "L1" in pack["skill"]
        assert "L2" in pack["skill"]

    def test_load_nonexistent_pack_raises(self):
        """Loading nonexistent pack should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_knowledge_pack("nonexistent_pack_12345")


class TestPayloadStructure:
    """Test that payload contains all required keys and structure."""

    def test_payload_contains_all_required_keys(self, sample_catalog, sample_full_profile):
        """Payload should have all documented keys."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        required_keys = {
            "method_id",
            "task",
            "input_profile_ref",
            "prompt_payload_ref",
            "input_profile_hash",
            "prompt_payload_hash",
            "prompt_profile",
            "rules",
            "selected_cases",
            "case_selection",
            "recommendation_policy",
            "skill_instructions",
            "readonly_params",
            "adjustable_params",
        }

        assert set(payload.keys()) == required_keys
        assert "catalog_subset" not in payload
        assert "case_retrieval" not in payload

    def test_readonly_params_are_l1_only(self, sample_catalog, sample_full_profile):
        """readonly_params should only contain L1 parameters."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        for key, spec in payload["readonly_params"].items():
            assert spec["level"] == 1, f"readonly_params[{key}] has level {spec['level']}, expected 1"

    def test_adjustable_params_are_l2_only(self, sample_catalog, sample_full_profile):
        """adjustable_params should only contain L2 parameters."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        for key, spec in payload["adjustable_params"].items():
            assert spec["level"] == 2, f"adjustable_params[{key}] has level {spec['level']}, expected 2"

    def test_catalog_subset_structure(self, sample_catalog, sample_full_profile):
        """Lean sealed prompt keeps one L1 and one L2 catalog surface."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert "catalog_subset" not in payload
        assert isinstance(payload["readonly_params"], dict)
        assert isinstance(payload["adjustable_params"], dict)


class TestReferenceGeneration:
    """Test reference ID generation for replay and audit."""

    def test_input_profile_ref_stable(self, sample_catalog, sample_full_profile):
        """Same profile should generate same ref."""
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )
        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert payload1["input_profile_ref"] == payload2["input_profile_ref"]
        assert len(payload1["input_profile_ref"]) == 64  # SHA256 hex

    def test_input_profile_ref_changes_on_mutation(self, sample_catalog, sample_full_profile):
        """Modified profile should generate different ref."""
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        modified_profile = {**sample_full_profile}
        modified_profile["sample"] = {**sample_full_profile["sample"], "organism": "Mus musculus"}

        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=modified_profile,
            task="recommend_initial",
        )

        assert payload1["input_profile_ref"] != payload2["input_profile_ref"]

    def test_prompt_payload_ref_stable(self, sample_catalog, sample_full_profile):
        """Same inputs should generate same payload ref."""
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )
        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert payload1["prompt_payload_ref"] == payload2["prompt_payload_ref"]
        assert len(payload1["prompt_payload_ref"]) == 64  # SHA256 hex

    def test_refs_are_distinct(self, sample_catalog, sample_full_profile):
        """input_profile_ref and prompt_payload_ref should be different."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        assert payload["input_profile_ref"] != payload["prompt_payload_ref"]

    def test_generate_profile_ref_directly(self):
        """Test generate_profile_ref function directly."""
        profile1 = {"key": "value", "nested": {"a": 1}}
        profile2 = {"key": "value", "nested": {"a": 1}}
        profile3 = {"key": "different", "nested": {"a": 1}}

        ref1 = generate_profile_ref(profile1)
        ref2 = generate_profile_ref(profile2)
        ref3 = generate_profile_ref(profile3)

        assert ref1 == ref2  # Same content
        assert ref1 != ref3  # Different content
        assert len(ref1) == 64

    def test_generate_payload_ref_directly(self):
        """Test generate_payload_ref function directly."""
        payload1 = {"method": "test", "params": [1, 2, 3]}
        payload2 = {"method": "test", "params": [1, 2, 3]}
        payload3 = {"method": "test", "params": [1, 2, 4]}

        ref1 = generate_payload_ref(payload1)
        ref2 = generate_payload_ref(payload2)
        ref3 = generate_payload_ref(payload3)

        assert ref1 == ref2  # Same content
        assert ref1 != ref3  # Different content
        assert len(ref1) == 64


class TestReplayCapability:
    """Test that full profile can be replayed from references."""

    def test_full_profile_replay(self, sample_catalog, sample_full_profile):
        """Same full_profile should produce same input_profile_ref for replay."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # In real system, we'd look up full_profile by input_profile_ref
        # Here we verify the ref is stable for the same input
        ref = payload["input_profile_ref"]
        computed_ref = generate_profile_ref(sample_full_profile)

        assert ref == computed_ref

    def test_payload_includes_full_audit_trail(self, sample_catalog, sample_full_profile):
        """Payload should include all components for full audit trail."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        # Verify all audit trail components present
        assert "input_profile_ref" in payload
        assert "prompt_payload_ref" in payload
        assert "method_id" in payload
        assert "task" in payload
        assert "prompt_profile" in payload
        assert "rules" in payload
        assert "selected_cases" in payload
        assert "case_selection" in payload
        assert "skill_instructions" in payload
        assert "catalog_subset" not in payload

        # Verify we can reconstruct what was sent to LLM
        llm_input_components = {
            "profile": payload["prompt_profile"],
            "readonly": payload["readonly_params"],
            "adjustable": payload["adjustable_params"],
            "rules": payload["rules"],
            "cases": payload["selected_cases"],
            "instructions": payload["skill_instructions"],
        }
        assert all(v is not None for v in llm_input_components.values())


class TestArtifactWriter:
    """Test artifact writing functionality with session/attempt context."""

    def test_write_artifacts_with_session_id(self, sample_catalog, sample_full_profile, tmp_path):
        """Write artifacts with session_id should create session-scoped paths."""
        session_id = "test_session_001"
        attempt_id = 1

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=attempt_id,
            data_dir=tmp_path,
        )

        # Verify profile artifact was written to session-scoped path
        expected_profile_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "input_profile.json"
        assert expected_profile_path.exists(), f"Profile artifact not found at {expected_profile_path}"

        # Verify payload artifact was written to attempt-scoped path
        expected_payload_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "prompt_payload.json"
        assert expected_payload_path.exists(), f"Payload artifact not found at {expected_payload_path}"

        # Verify returned refs are path-based (not hash-based) when session_id is provided
        assert payload["input_profile_ref"].startswith("data/artifacts/assembly/")
        assert payload["input_profile_ref"] == f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}/input_profile.json"
        assert payload["prompt_payload_ref"].startswith("data/artifacts/assembly/")
        assert payload["prompt_payload_ref"] == f"data/artifacts/assembly/{session_id}/parameter_attempt_{attempt_id}/prompt_payload.json"

    def test_write_artifacts_without_session_id(self, sample_catalog, sample_full_profile, tmp_path):
        """Artifact writes require final session/attempt refs."""
        with pytest.raises(ValueError):
            build_prompt_payload(
                method_id="test_method",
                catalog=sample_catalog,
                full_profile=sample_full_profile,
                task="recommend_initial",
                write_artifacts=True,
                session_id=None,
                attempt_id=None,
                data_dir=tmp_path,
            )

    def test_artifact_content_matches_input(self, sample_catalog, sample_full_profile, tmp_path):
        """Written artifacts should contain correct content."""
        session_id = "test_session_002"
        attempt_id = 2

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=attempt_id,
            data_dir=tmp_path,
        )

        # Read and verify profile artifact
        profile_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "input_profile.json"
        with profile_path.open("r") as f:
            loaded_profile = json.load(f)
        assert loaded_profile == sample_full_profile

        # Read and verify payload artifact
        payload_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "prompt_payload.json"
        with payload_path.open("r") as f:
            loaded_payload = json.load(f)

        # Verify key fields are preserved
        assert loaded_payload["method_id"] == "test_method"
        assert loaded_payload["task"] == "recommend_initial"
        assert loaded_payload["prompt_profile"] == payload["prompt_profile"]
        assert loaded_payload["readonly_params"] == payload["readonly_params"]
        assert loaded_payload["adjustable_params"] == payload["adjustable_params"]

    def test_load_roundtrip_with_path_refs(self, sample_catalog, sample_full_profile, tmp_path):
        """Test that load roundtrip with path-based refs works (P1 fix implemented)."""
        from app.stages.assembly.prompt_payload import load_profile_by_ref, load_payload_by_ref

        # Write artifacts with session_id (path-based refs)
        session_id = "test_session_003"
        attempt_id = 3

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=attempt_id,
            data_dir=tmp_path,
        )

        # Verify files exist at correct paths
        profile_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "input_profile.json"
        assert profile_path.exists()

        payload_path = tmp_path / "artifacts" / "assembly" / session_id / f"parameter_attempt_{attempt_id}" / "prompt_payload.json"
        assert payload_path.exists()

        # Load using path refs (should work with the fix)
        loaded_profile = load_profile_by_ref(payload["input_profile_ref"], data_dir=tmp_path)
        assert loaded_profile == sample_full_profile

        loaded_payload = load_payload_by_ref(payload["prompt_payload_ref"], data_dir=tmp_path)
        assert loaded_payload["method_id"] == payload["method_id"]
        assert loaded_payload["task"] == payload["task"]
        assert loaded_payload["prompt_profile"] == payload["prompt_profile"]
        assert loaded_payload["prompt_payload_ref"] == payload["prompt_payload_ref"]
        assert loaded_payload["prompt_payload_hash"] == generate_payload_ref(
            {k: v for k, v in payload.items() if k not in ("prompt_payload_ref", "prompt_payload_hash")}
        )
        assert loaded_payload["prompt_payload_hash"] != file_sha256(payload_path)

    def test_multiple_attempts_same_session(self, sample_catalog, sample_full_profile, tmp_path):
        """Multiple attempts in same session should create separate payload files."""
        session_id = "test_session_004"

        # Write attempt 1
        payload1 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=1,
            data_dir=tmp_path,
        )

        # Write attempt 2 with modified profile
        modified_profile = {**sample_full_profile}
        modified_profile["sample"] = {**sample_full_profile["sample"], "organism": "Mus musculus"}

        payload2 = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=modified_profile,
            task="retry_after_execution_failure",
            write_artifacts=True,
            session_id=session_id,
            attempt_id=2,
            data_dir=tmp_path,
        )

        # Verify both payload files exist
        payload1_path = tmp_path / "artifacts" / "assembly" / session_id / "parameter_attempt_1" / "prompt_payload.json"
        payload2_path = tmp_path / "artifacts" / "assembly" / session_id / "parameter_attempt_2" / "prompt_payload.json"

        assert payload1_path.exists()
        assert payload2_path.exists()

        # Verify refs are different
        assert payload1["prompt_payload_ref"] != payload2["prompt_payload_ref"]
        assert payload1["input_profile_ref"] != payload2["input_profile_ref"]
        assert "parameter_attempt_1/input_profile.json" in payload1["input_profile_ref"]
        assert "parameter_attempt_2/input_profile.json" in payload2["input_profile_ref"]

        # Verify content is different
        with payload1_path.open("r") as f:
            content1 = json.load(f)
        with payload2_path.open("r") as f:
            content2 = json.load(f)

        assert content1["task"] == "recommend_initial"
        assert content2["task"] == "retry_after_execution_failure"
        assert content1["prompt_profile"]["sample"]["organism"] == "Homo sapiens"
        assert content2["prompt_profile"]["sample"]["organism"] == "Mus musculus"

    @pytest.mark.parametrize(
        ("retry_task", "tamper_semantic_hash", "catalog_drift"),
        [
            ("retry_after_execution_failure", False, False),
            ("retry_after_review_failure", False, False),
            ("retry_after_execution_failure", True, False),
            ("retry_after_execution_failure", False, True),
        ],
    )
    def test_hifiasm_retry_rehydrates_json_attempt_zero_without_registry_state(
        self,
        sample_catalog,
        sample_full_profile,
        tmp_path,
        monkeypatch,
        retry_task,
        tamper_semantic_hash,
        catalog_drift,
    ):
        case = _legal_hifiasm_case(
            "case_replay", quote="Curated Methods evidence.", distance=0.1
        )
        selection = _attested_numeric_selection(
            [case], query_id="s1:parameter_attempt_0"
        )
        monkeypatch.setattr(
            prompt_payload_module,
            "load_knowledge_pack",
            lambda *_args, **_kwargs: {
                "cases": [case],
                "rules": {"rules": []},
                "skill": "Use only visible curated evidence.",
                "strategies": {},
                "pack_root": _HIFIASM_SOURCE_ROOT,
            },
        )
        monkeypatch.setattr(
            prompt_payload_module,
            "select_evidence_cases",
            lambda *_args, **_kwargs: ([case], selection),
        )
        catalog = sample_catalog.model_copy(update={"method_id": "hifiasm"})
        initial = build_prompt_payload(
            method_id="hifiasm",
            catalog=catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id="s1",
            attempt_id=0,
            data_dir=tmp_path,
        )
        initial_path = (
            tmp_path
            / "artifacts"
            / "assembly"
            / "s1"
            / "parameter_attempt_0"
            / "prompt_payload.json"
        )
        persisted = json.loads(initial_path.read_text(encoding="utf-8"))
        if tamper_semantic_hash:
            persisted["prompt_payload_hash"] = "0" * 64
            initial_path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
        retry_profile = json.loads(json.dumps(sample_full_profile))
        retry_profile["sample"]["organism"] = "mutated after attempt 0"
        retry_profile["previous_attempt"] = {
            "execution_status": "failed",
            "exit_code": 2,
            "params": {
                "threads": 16,
                "kmer_size": 51,
                "purge_level": 1,
            },
        }
        retry_profile["assembly_review"] = {
            "failure_mode": "under_purged",
            "busco_duplicate": 0.12,
        }
        retry_catalog = catalog.model_copy(deep=True)
        if catalog_drift:
            retry_catalog.parameters["read_type"] = retry_catalog.parameters[
                "read_type"
            ].model_copy(update={"description": "drifted L1 catalog description"})
        retry_kwargs = {
            "method_id": "hifiasm",
            "catalog": retry_catalog,
            "full_profile": retry_profile,
            "task": retry_task,
            "write_artifacts": True,
            "session_id": "s1",
            "attempt_id": 1,
            "data_dir": tmp_path,
            "initial_attempt_snapshot": {
                "prompt_payload_ref": {
                    "path_ref": initial["prompt_payload_ref"],
                    "sha256": file_sha256(initial_path),
                }
            },
        }
        if tamper_semantic_hash:
            with pytest.raises(ValueError, match="prompt_payload_hash does not match"):
                build_prompt_payload(**retry_kwargs)
            return
        if catalog_drift:
            with pytest.raises(
                ValueError,
                match="readonly_params differs from the authoritative catalog snapshot",
            ):
                build_prompt_payload(**retry_kwargs)
            return
        retry = build_prompt_payload(**retry_kwargs)

        assert retry["task"] == retry_task
        for field in ("sample", "assets", "read_qc", "genome_survey"):
            assert retry["prompt_profile"][field] == persisted["prompt_profile"][field]
        assert retry["prompt_profile"]["previous_attempt"]
        if retry_task == "retry_after_review_failure":
            assert retry["prompt_profile"]["assembly_review"]
        assert retry["evidence_cases"] == persisted["evidence_cases"]
        assert retry["case_selection"] == persisted["case_selection"]
        assert "case_retrieval" not in retry
        assert retry["methods_evidence"] == persisted["methods_evidence"]
        assert retry["retry_evidence"]["task"] == retry_task
        assert retry["prompt_payload_hash"] == generate_payload_ref(
            {
                key: value
                for key, value in retry.items()
                if key not in {"prompt_payload_ref", "prompt_payload_hash"}
            }
        )

    def test_write_artifacts_false_no_files_created(self, sample_catalog, sample_full_profile, tmp_path):
        """With write_artifacts=False, no files should be created."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=False,
            session_id="test_session_005",
            attempt_id=1,
            data_dir=tmp_path,
        )

        # Verify no artifacts directory was created
        artifacts_dir = tmp_path / "artifacts"
        assert not artifacts_dir.exists() or len(list(artifacts_dir.rglob("*.json"))) == 0

        # Verify refs are hash-based (not path-based) even with session_id provided
        assert len(payload["input_profile_ref"]) == 64
        assert len(payload["prompt_payload_ref"]) == 64


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_profile_sections(self, sample_catalog):
        """Empty profile sections should be handled gracefully."""
        minimal_profile = {
            "sample": {"sample_id": "test"},
            "assets": {},
            "read_qc": {},
            "genome_survey": {},
            "previous_attempt": {},
            "assembly_review": {},
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=minimal_profile,
            task="recommend_initial",
        )

        assert payload["input_profile_ref"] is not None
        assert payload["prompt_payload_ref"] is not None

    def test_catalog_with_only_l0(self):
        """Catalog with only L0 parameters should produce empty readonly/adjustable."""
        catalog_l0_only = MethodParameterCatalog(
            method_id="l0_only",
            tool="test",
            read_type="hifi",
            parameters={
                "output": ParameterSpec(cli="--out", level=0, source="ctx", value_type="path"),
            },
        )
        minimal_profile = {
            "sample": {},
            "assets": {},
            "read_qc": {},
            "genome_survey": {},
            "previous_attempt": {},
            "assembly_review": {},
        }

        payload = build_prompt_payload(
            method_id="l0_only",
            catalog=catalog_l0_only,
            full_profile=minimal_profile,
            task="recommend_initial",
        )

        assert len(payload["readonly_params"]) == 0
        assert len(payload["adjustable_params"]) == 0

    def test_catalog_with_only_l2(self):
        """Catalog with only L2 parameters should produce empty readonly."""
        catalog_l2_only = MethodParameterCatalog(
            method_id="l2_only",
            tool="test",
            read_type="hifi",
            parameters={
                "threads": ParameterSpec(cli="--threads", level=2, source="llm", value_type="int"),
            },
        )
        minimal_profile = {
            "sample": {},
            "assets": {},
            "read_qc": {},
            "genome_survey": {},
            "previous_attempt": {},
            "assembly_review": {},
        }

        payload = build_prompt_payload(
            method_id="l2_only",
            catalog=catalog_l2_only,
            full_profile=minimal_profile,
            task="recommend_initial",
        )

        assert len(payload["readonly_params"]) == 0
        assert len(payload["adjustable_params"]) == 1

    def test_payload_uses_selected_cases_not_full_database(self, sample_catalog, sample_full_profile):
        """Prompt payload should include a selected evidence subset, not every stored case."""
        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=sample_full_profile,
            task="recommend_initial",
        )

        pack = load_knowledge_pack("parameter_recommender")
        assert "cases" not in payload
        assert len(payload["selected_cases"]) <= 8
        assert len(payload["selected_cases"]) < len(pack["cases"])
        assert payload["case_selection"]["selected_case_ids"] == [
            case["case_id"] for case in payload["selected_cases"]
        ]

    def test_cyclone_route_scores_ont_evidence_cases_highest(self, sample_catalog, sample_full_profile):
        cyclone_profile = {
            **sample_full_profile,
            "assets": {
                **sample_full_profile["assets"],
                "assembly_route": "cyclone",
                "read_type": "ont",
                "has_hifi": False,
                "has_ont": True,
            },
        }

        payload = build_prompt_payload(
            method_id="test_method",
            catalog=sample_catalog,
            full_profile=cyclone_profile,
            task="recommend_initial",
        )

        assert payload["prompt_profile"]["assets"]["assembly_route"] == "cyclone"
        assert payload["prompt_profile"]["assets"]["read_type"] == "ont"
        assert payload["selected_cases"]
        assert payload["selected_cases"][0]["features"]["read_type"] == "ont"
        assert {case["features"]["read_type"] for case in payload["selected_cases"]} == {"hifi", "ont"}
        assert all(
            set(entry["excluded_reasons"]) <= {"failure_case_excluded_for_initial"}
            for entry in payload["case_selection"]["trace"]
        )


class TestRealCatalogCompatibility:
    """Test that knowledge pack examples reference real parameters from catalogs."""

    @pytest.mark.parametrize(
        "method_id,catalog_id",
        [
            ("assembly.candidate.flye", "flye"),
            ("assembly.candidate.nextdenovo", "nextdenovo"),
            ("assembly.candidate.hifiasm", "hifiasm"),
            ("assembly.candidate.hifiasm_ont", "hifiasm_ont"),
        ],
    )
    def test_executable_assembly_methods_have_phase11_catalogs(self, method_id, catalog_id):
        """All executable assembly methods should use the typed parameter-attempt catalog contract."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)

        assert catalog.method_id == catalog_id
        assert sum(1 for p in catalog.parameters.values() if p.level == 0) > 0
        assert sum(1 for p in catalog.parameters.values() if p.level == 1) > 0
        assert sum(1 for p in catalog.parameters.values() if p.level == 2) > 0

    @pytest.mark.parametrize("method_id", ["hifiasm", "hifiasm_ont"])
    def test_cases_yaml_params_are_l2_in_real_catalog(self, method_id):
        """All observed parameter values in cases.yaml must exist in catalog as L2."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)
        pack = load_knowledge_pack("parameter_recommender")

        # Extract all parameter keys from observed params across cases.
        all_param_keys = set()
        for case in pack["cases"]:
            params = case.get("params") or {}
            values = params.get("values") or {}
            all_param_keys.update(values.keys())

        # Verify each parameter exists in catalog and is L2
        for param_key in all_param_keys:
            assert param_key in catalog.parameters, f"Parameter {param_key!r} from cases.yaml not found in {method_id} catalog"
            param_spec = catalog.parameters[param_key]
            assert param_spec.level == 2, f"Parameter {param_key!r} from cases.yaml is L{param_spec.level}, expected L2 in {method_id}"

    @pytest.mark.parametrize("method_id", ["hifiasm", "hifiasm_ont"])
    def test_rules_yaml_params_are_l2_in_real_catalog(self, method_id):
        """All constraint keys in rules.yaml must exist in catalog as L2."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)
        pack = load_knowledge_pack("parameter_recommender")

        # Extract all constraint parameter keys from rules
        all_constraint_keys = set()
        rules_list = pack["rules"].get("rules", []) if isinstance(pack["rules"], dict) else pack["rules"]
        for rule in rules_list:
            if "constraints" in rule:
                all_constraint_keys.update(rule["constraints"].keys())

        # Verify each parameter exists in catalog and is L2
        for param_key in all_constraint_keys:
            assert param_key in catalog.parameters, f"Parameter {param_key!r} from rules.yaml not found in {method_id} catalog"
            param_spec = catalog.parameters[param_key]
            assert param_spec.level == 2, f"Parameter {param_key!r} from rules.yaml is L{param_spec.level}, expected L2 in {method_id}"

    @pytest.mark.parametrize("method_id", ["hifiasm", "hifiasm_ont"])
    def test_full_payload_with_real_catalog(self, method_id):
        """Verify real catalog has expected L1/L2 structure for payload building."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)

        # Count L0, L1, L2 parameters
        l0_count = sum(1 for p in catalog.parameters.values() if p.level == 0)
        l1_count = sum(1 for p in catalog.parameters.values() if p.level == 1)
        l2_count = sum(1 for p in catalog.parameters.values() if p.level == 2)

        # Verify we have parameters at each level
        assert l0_count > 0, f"{method_id} catalog should have L0 parameters"
        assert l1_count > 0, f"{method_id} catalog should have L1 parameters for readonly_params"
        assert l2_count > 0, f"{method_id} catalog should have L2 parameters for adjustable_params"

        # Verify specific L1 parameters that are referenced in profile building
        assert "hg_size" in catalog.parameters, f"{method_id} catalog missing hg_size"
        assert catalog.parameters["hg_size"].level == 1, f"{method_id} hg_size should be L1"
        assert "hom_cov" in catalog.parameters, f"{method_id} catalog missing hom_cov"
        assert catalog.parameters["hom_cov"].level == 1, f"{method_id} hom_cov should be L1"
        assert "n_hap" in catalog.parameters, f"{method_id} catalog missing n_hap"
        assert catalog.parameters["n_hap"].level == 1, f"{method_id} n_hap should be L1"

        # Verify all L2 parameters have required fields for LLM recommendation
        for key, spec in catalog.parameters.items():
            if spec.level == 2:
                assert spec.source == "parameter_recommender", f"{method_id} L2 param {key} should have source='parameter_recommender'"
                assert spec.value_type is not None, f"{method_id} L2 param {key} should have value_type"
                # CLI can be None for some parameters, but most should have it
                # description can be None but most should have it

        # Verify no parameters have level > 2
        for key, spec in catalog.parameters.items():
            assert spec.level in [0, 1, 2], f"{method_id} parameter {key} has invalid level {spec.level}"

    @pytest.mark.parametrize("method_id,read_type", [("hifiasm", "hifi"), ("hifiasm_ont", "ont")])
    def test_method_and_read_type_are_scoring_attributes(self, method_id, read_type):
        """Method/read type affect ranking without becoming discrete hard filters."""
        from app.stages.assembly.parameter_catalog import load_parameter_catalog

        catalog = load_parameter_catalog(method_id)
        sample_profile = {
            "sample": {
                "sample_id": "test_sample_001",
                "organism": "Homo sapiens",
                "taxon_id": 9606,
                "clade": "vertebrate_mammalian",
                "ploidy": 2,
                "expected_chromosome_count": 23,
                "target_level": "chromosome",
            },
            "assets": {
                "read_type": read_type,
                "read_format": "fastq",
                "has_hifi": read_type == "hifi",
                "has_ont": read_type == "ont",
                "has_ultra_long": False,
                "has_hic": True,
                "has_trio": False,
                "has_rnaseq": True,
            },
            "read_qc": {
                "read_count": 5000000,
                "total_bases": 90000000000,
                "read_n50": 15000,
                "read_mean": 18000,
                "effective_coverage_1kb": 60,
                "mean_quality": 30,
            },
            "genome_survey": {
                "estimated_genome_size": 3100000000,
                "heterozygosity": 0.001,
                "repeat_content": 0.45,
                "kmer_peak_coverage": 55,
            },
            "previous_attempt": {},
            "assembly_review": {},
        }

        payload = build_prompt_payload(
            method_id=method_id,
            catalog=catalog,
            full_profile=sample_profile,
            task="recommend_initial",
        )

        cases = payload["evidence_cases"]
        assert cases
        case_read_types = {(case.get("applicability") or {}).get("read_type") for case in cases}
        assert read_type in case_read_types

        # The fixed top-five shortlist may now be filled by the larger matching
        # corpus. Verify cross-read-type cases remain retrieval-eligible instead
        # of requiring one to displace a stronger shortlist match.
        pack = load_knowledge_pack("parameter_recommender")
        validated_cases = validate_hifiasm_evidence_cases(
            pack["cases"],
            pack_root=Path(pack["pack_root"]),
        )
        assert all(case["retrieval_eligible"] is True for case in validated_cases)
        assert {
            (case.get("applicability") or {}).get("read_type")
            for case in validated_cases
        } == {"hifi", "ont"}

        assert cases[0]["applicability"]["read_type"] == read_type
        assert method_id in cases[0]["applicability"]["target_methods"]
        assert payload["prompt_profile"]["assets"]["read_type"] == read_type
        assert payload["method_id"] == method_id


def test_production_hifiasm_prompt_consumes_ranked_real_paper_evidence(
    sample_full_profile,
):
    """The production pack, selector, top-five, and prompt projection stay connected."""
    from app.stages.assembly.parameter_catalog import load_parameter_catalog

    pack = load_knowledge_pack("parameter_recommender")
    approved_papers = {
        case["case_id"]: case
        for case in pack["cases"]
        if case.get("schema_version") == 3
        and (case.get("source") or {}).get("kind") == "paper"
        and (case.get("curation") or {}).get("status") == "approved"
    }
    assert len(approved_papers) == 10

    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=load_parameter_catalog("hifiasm"),
        full_profile=sample_full_profile,
        task="recommend_initial",
    )

    retrieval = payload["case_selection"]
    prompt_cases = payload["evidence_cases"]
    prompt_case_ids = [case["case_id"] for case in prompt_cases]
    assert retrieval["retrieval_backend"] == "hifiasm_numeric"
    assert len(prompt_case_ids) == 5
    assert prompt_case_ids == retrieval["candidate_case_ids"]
    assert prompt_case_ids == retrieval["selected_case_ids"]
    assert prompt_case_ids == retrieval["evidence_case_ids"]
    assert set(prompt_case_ids) <= set(approved_papers)
    assert "selected_cases" not in payload
    assert "case_retrieval" not in payload
    assert "catalog_subset" not in payload
    assert "_hifiasm_ranked_selection_attestation" not in payload
    assert "_hifiasm_ranked_selection_attestation" not in retrieval
    rendered = json.dumps(payload, sort_keys=True)
    assert "_hifiasm_ranked_selection_attestation" not in rendered

    for prompt_case in prompt_cases:
        source_case = approved_papers[prompt_case["case_id"]]
        source_quotes = {
            span["span_id"]: span["quote"] for span in source_case["evidence_spans"]
        }
        assert prompt_case["source"]["doi"] == source_case["source"]["doi"]
        assert prompt_case["source_excerpts"]
        assert {
            excerpt["span_id"]: excerpt["quote"]
            for excerpt in prompt_case["source_excerpts"]
        } == source_quotes


def test_hifiasm_writes_ref_backed_methods_evidence_payload(
    sample_catalog, sample_full_profile, tmp_path, monkeypatch
):
    evidence_case = _legal_hifiasm_case(
        "case_a",
        quote="Exact excerpt.",
        distance=0.1,
    )
    selection = _attested_numeric_selection(
        [evidence_case],
        query_id="s1:parameter_attempt_0",
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.load_knowledge_pack",
        lambda *_args, **_kwargs: {
            "cases": [],
            "rules": {"rules": []},
            "skill": "Use conservative evidence.",
            "strategies": {},
            "pack_root": tmp_path,
        },
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.select_evidence_cases",
        lambda *_args, **_kwargs: ([evidence_case], selection),
    )

    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=sample_catalog.model_copy(update={"method_id": "hifiasm"}),
        full_profile=sample_full_profile,
        task="recommend_initial",
        write_artifacts=True,
        session_id="s1",
        attempt_id=0,
        data_dir=tmp_path,
    )

    evidence_path = tmp_path / payload["evidence_payload_ref"].removeprefix("data/")
    prompt_path = tmp_path / payload["prompt_payload_ref"].removeprefix("data/")
    assert evidence_path.name == "evidence_payload.json"
    assert evidence_path.exists()
    assert prompt_path.exists()
    assert payload["evidence_payload_ref"]
    assert "evidence_payload_ref" not in payload["case_selection"]
    assert "case_retrieval" not in payload
    assert payload["methods_evidence"]["response_contract"] == "case_assessments_and_recommendation"
    loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert loaded["candidate_case_ids"] == ["case_a"]
    assert loaded["cases"][0]["source_excerpts"][0]["quote"] == "Exact excerpt."
    assert json.loads(prompt_path.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize(
    "mutate",
    [
        lambda selection: selection["trace"][0].update({"distance": 999}),
        lambda selection: selection["candidate_case_ids"].append("forged-case"),
        lambda selection: selection.update({"eligible_case_count": 999}),
        lambda selection: selection.update({"warning": "forged warning"}),
        lambda selection: selection["query"].update({"query_id": "forged-query"}),
    ],
    ids=[
        "trace-distance",
        "appended-candidate",
        "eligible-count",
        "warning",
        "query",
    ],
)
def test_hifiasm_retrieval_artifacts_require_task3_attested_audit_fields(
    sample_catalog,
    sample_full_profile,
    tmp_path,
    monkeypatch,
    mutate,
):
    evidence_case = _legal_hifiasm_case(
        "case_a",
        quote="Exact excerpt.",
        distance=0.1,
    )
    selection = _attested_numeric_selection(
        [evidence_case],
        query_id="s1:parameter_attempt_0",
    )
    mutate(selection)
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.load_knowledge_pack",
        lambda *_args, **_kwargs: {
            "cases": [],
            "rules": {"rules": []},
            "skill": "Use conservative evidence.",
            "strategies": {},
            "pack_root": tmp_path,
        },
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.select_evidence_cases",
        lambda *_args, **_kwargs: ([evidence_case], selection),
    )
    publications = []
    original_publish = prompt_payload_module._publish_prepared_json_artifact

    def _track_publications(**kwargs):
        publications.append(kwargs["filename"])
        return original_publish(**kwargs)

    monkeypatch.setattr(
        prompt_payload_module,
        "_publish_prepared_json_artifact",
        _track_publications,
    )

    with pytest.raises(EvidenceCaseValidationError, match="Task 3 selection attestation"):
        build_prompt_payload(
            method_id="hifiasm",
            catalog=sample_catalog.model_copy(update={"method_id": "hifiasm"}),
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id="s1",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert publications == []
    assert not (tmp_path / "artifacts").exists()


def test_hifiasm_malformed_case_fails_before_any_artifact_publication(
    sample_catalog,
    sample_full_profile,
    tmp_path,
    monkeypatch,
):
    evidence_case = _legal_hifiasm_case(
        "case_a",
        quote="Exact excerpt.",
        distance=0.1,
    )
    selection = _attested_numeric_selection(
        [evidence_case],
        query_id="s1:parameter_attempt_0",
    )
    malformed_case = dict(evidence_case)
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.load_knowledge_pack",
        lambda *_args, **_kwargs: {
            "cases": [],
            "rules": {"rules": []},
            "skill": "Use conservative evidence.",
            "strategies": {},
            "pack_root": tmp_path,
        },
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.select_evidence_cases",
        lambda *_args, **_kwargs: ([malformed_case], selection),
    )

    with pytest.raises(EvidenceCaseValidationError, match="Task 2 validation-owned attestation"):
        build_prompt_payload(
            method_id="hifiasm",
            catalog=sample_catalog.model_copy(update={"method_id": "hifiasm"}),
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id="s1",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("failure_point", ["canonicalization", "canonical_write"])
def test_hifiasm_prompt_artifact_is_absent_if_canonical_build_is_interrupted(
    sample_catalog, sample_full_profile, tmp_path, monkeypatch, failure_point
):
    evidence_case = _legal_hifiasm_case(
        "case_a",
        quote="Exact excerpt.",
        distance=0.1,
    )
    selection = _attested_numeric_selection(
        [evidence_case],
        query_id="s1:parameter_attempt_0",
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.load_knowledge_pack",
        lambda *_args, **_kwargs: {
            "cases": [],
            "rules": {"rules": []},
            "skill": "Use conservative evidence.",
            "strategies": {},
            "pack_root": tmp_path,
        },
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.select_evidence_cases",
        lambda *_args, **_kwargs: ([evidence_case], selection),
    )

    if failure_point == "canonicalization":
        def _interrupt(_payload, **_kwargs):
            raise RuntimeError("interrupted before canonical write")

        monkeypatch.setattr(
            "app.stages.assembly.prompt_payload.sanitize_hifiasm_methods_prompt_payload",
            _interrupt,
        )
        error = "interrupted before canonical write"
    else:
        original_publish = prompt_payload_module._publish_prepared_json_artifact

        def _fail_canonical_write(**kwargs):
            if kwargs["filename"] != "prompt_payload.json":
                return original_publish(**kwargs)
            payload = json.loads(kwargs["serialized"])
            assert payload["hifiasm_methods_prompt_schema_version"] == 2
            assert payload["prompt_payload_hash"] == generate_payload_ref(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"prompt_payload_ref", "prompt_payload_hash"}
                }
            )
            raise RuntimeError("canonical prompt write failed")

        monkeypatch.setattr(
            prompt_payload_module,
            "_publish_prepared_json_artifact",
            _fail_canonical_write,
        )
        error = "canonical prompt write failed"

    with pytest.raises(RuntimeError, match=error):
        build_prompt_payload(
            method_id="hifiasm",
            catalog=sample_catalog.model_copy(update={"method_id": "hifiasm"}),
            full_profile=sample_full_profile,
            task="recommend_initial",
            write_artifacts=True,
            session_id="s1",
            attempt_id=0,
            data_dir=tmp_path,
        )

    attempt_dir = tmp_path / "artifacts" / "assembly" / "s1" / "parameter_attempt_0"
    if failure_point == "canonicalization":
        assert not (tmp_path / "artifacts").exists()
    else:
        assert (attempt_dir / "input_profile.json").exists()
        assert (attempt_dir / "evidence_payload.json").exists()
    assert not (attempt_dir / "prompt_payload.json").exists()


def test_prompt_artifact_partial_write_preserves_previous_file_and_cleans_temp(
    tmp_path, monkeypatch
):
    previous = {"version": "complete-previous"}
    artifact = write_payload_artifact(
        payload=previous,
        session_id="s1",
        attempt_id=0,
        data_dir=tmp_path,
    )
    artifact_path = tmp_path / artifact.path_ref.removeprefix("data/")

    def _fail_after_first_byte(handle, serialized):
        handle.write(serialized[:1])
        handle.flush()
        raise OSError("simulated partial write")

    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload._write_and_sync_json",
        _fail_after_first_byte,
    )

    with pytest.raises(OSError, match="simulated partial write"):
        write_payload_artifact(
            payload={"version": "incomplete-new"},
            session_id="s1",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert json.loads(artifact_path.read_text(encoding="utf-8")) == previous
    assert not list(artifact_path.parent.glob(".prompt_payload.json.*.tmp"))


def test_artifact_writer_repeated_write_is_idempotent_but_never_clobbers(tmp_path):
    original = {"version": "immutable"}
    first_ref = write_payload_artifact(
        payload=original,
        session_id="repeated",
        attempt_id=0,
        data_dir=tmp_path,
    )
    repeated_ref = write_payload_artifact(
        payload=original,
        session_id="repeated",
        attempt_id=0,
        data_dir=tmp_path,
    )

    assert repeated_ref == first_ref
    with pytest.raises(FileExistsError, match="different content"):
        write_payload_artifact(
            payload={"version": "replacement"},
            session_id="repeated",
            attempt_id=0,
            data_dir=tmp_path,
        )

    artifact_path = tmp_path / first_ref.path_ref.removeprefix("data/")
    assert artifact_path.read_bytes() == json.dumps(original, indent=2).encode("utf-8")
    assert not list(artifact_path.parent.glob(".prompt_payload.json.*.tmp"))


def test_artifact_writer_concurrent_identical_content_is_idempotent(tmp_path, monkeypatch):
    payload = {"version": "same", "items": [1, 2, 3]}
    barrier = threading.Barrier(2)
    original_write = prompt_payload_module._write_and_sync_json

    def _write_then_wait(handle, serialized):
        original_write(handle, serialized)
        barrier.wait(timeout=5)

    monkeypatch.setattr(prompt_payload_module, "_write_and_sync_json", _write_then_wait)

    def _write():
        return write_payload_artifact(
            payload=payload,
            session_id="concurrent-same",
            attempt_id=0,
            data_dir=tmp_path,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_write), executor.submit(_write)]
        refs = [future.result(timeout=10) for future in futures]

    assert refs[0] == refs[1]
    artifact_path = tmp_path / refs[0].path_ref.removeprefix("data/")
    expected_bytes = json.dumps(payload, indent=2).encode("utf-8")
    assert artifact_path.read_bytes() == expected_bytes
    assert refs[0].sha256 == hashlib.sha256(expected_bytes).hexdigest()
    assert not list(artifact_path.parent.glob(".prompt_payload.json.*.tmp"))


def test_artifact_writer_concurrent_different_content_never_clobbers(tmp_path, monkeypatch):
    payloads = [{"writer": "a"}, {"writer": "b"}]
    barrier = threading.Barrier(2)
    original_write = prompt_payload_module._write_and_sync_json

    def _write_then_wait(handle, serialized):
        original_write(handle, serialized)
        barrier.wait(timeout=5)

    monkeypatch.setattr(prompt_payload_module, "_write_and_sync_json", _write_then_wait)

    def _write(payload):
        return write_payload_artifact(
            payload=payload,
            session_id="concurrent-different",
            attempt_id=0,
            data_dir=tmp_path,
        )

    refs = []
    errors = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_write, payload) for payload in payloads]
        for future in futures:
            try:
                refs.append(future.result(timeout=10))
            except BaseException as exc:
                errors.append(exc)

    assert len(refs) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], FileExistsError)
    assert "different content" in str(errors[0])
    artifact_path = tmp_path / refs[0].path_ref.removeprefix("data/")
    final_bytes = artifact_path.read_bytes()
    expected_bytes = {
        json.dumps(payload, indent=2).encode("utf-8") for payload in payloads
    }
    assert final_bytes in expected_bytes
    assert refs[0].sha256 == hashlib.sha256(final_bytes).hexdigest()
    assert not list(artifact_path.parent.glob(".prompt_payload.json.*.tmp"))


@pytest.mark.parametrize(
    "session_id",
    ["/tmp/escape", ".", "..", "../escape", "nested/session", r"nested\session"],
)
def test_artifact_writer_rejects_unsafe_session_path_components(tmp_path, session_id):
    with pytest.raises(ValueError, match="session_id"):
        write_payload_artifact(
            payload={"safe": True},
            session_id=session_id,
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert not list(tmp_path.rglob("prompt_payload.json"))


@pytest.mark.parametrize(
    "attempt_id",
    [True, False, -1, "0", "../escape", 0.0, None],
)
def test_artifact_writer_rejects_invalid_attempt_ids_before_path_creation(
    tmp_path, attempt_id
):
    with pytest.raises(ValueError, match="attempt_id"):
        write_payload_artifact(
            payload={"safe": True},
            session_id="safe-session",
            attempt_id=attempt_id,
            data_dir=tmp_path,
        )

    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize(
    "symlink_level",
    ["data-root", "artifacts", "assembly", "attempt"],
)
def test_artifact_writer_rejects_preexisting_directory_symlinks(
    tmp_path, symlink_level
):
    outside = tmp_path / "outside"
    outside.mkdir()
    data_root = tmp_path / "data-root"

    if symlink_level == "data-root":
        data_root.symlink_to(outside, target_is_directory=True)
    elif symlink_level == "artifacts":
        data_root.mkdir()
        (data_root / "artifacts").symlink_to(outside, target_is_directory=True)
    elif symlink_level == "assembly":
        (data_root / "artifacts").mkdir(parents=True)
        (data_root / "artifacts" / "assembly").symlink_to(
            outside,
            target_is_directory=True,
        )
    else:
        session_dir = data_root / "artifacts" / "assembly" / "safe-session"
        session_dir.mkdir(parents=True)
        (session_dir / "parameter_attempt_0").symlink_to(
            outside,
            target_is_directory=True,
        )

    with pytest.raises(ValueError, match="symlink|non-directory"):
        write_payload_artifact(
            payload={"safe": True},
            session_id="safe-session",
            attempt_id=0,
            data_dir=data_root,
        )

    assert not (outside / "prompt_payload.json").exists()


def test_artifact_writer_rejects_resolved_session_symlink_escape(tmp_path):
    artifact_base = tmp_path / "artifacts" / "assembly"
    artifact_base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifact_base / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="session_id"):
        write_payload_artifact(
            payload={"safe": True},
            session_id="linked",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert not (outside / "parameter_attempt_0" / "prompt_payload.json").exists()


def test_artifact_writer_attempt_directory_swap_is_detected_and_rolled_back(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    attempt_dir = (
        tmp_path
        / "artifacts"
        / "assembly"
        / "swap-session"
        / "parameter_attempt_0"
    )
    displaced_dir = tmp_path / "displaced-attempt"
    original_link = os.link
    swapped = False

    def _swap_path_then_publish_anchored(source, destination, **kwargs):
        nonlocal swapped
        if not swapped:
            attempt_dir.rename(displaced_dir)
            attempt_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(
        prompt_payload_module.os,
        "link",
        _swap_path_then_publish_anchored,
    )

    with pytest.raises(ValueError, match="artifact namespace changed"):
        write_payload_artifact(
            payload={"anchored": True},
            session_id="swap-session",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert not (outside / "prompt_payload.json").exists()
    assert not (displaced_dir / "prompt_payload.json").exists()
    assert not list(displaced_dir.glob(".prompt_payload.json.*.tmp"))


def test_artifact_writer_rejects_existing_final_symlink_without_following(tmp_path):
    attempt_dir = (
        tmp_path
        / "artifacts"
        / "assembly"
        / "final-symlink"
        / "parameter_attempt_0"
    )
    attempt_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    final_path = attempt_dir / "prompt_payload.json"
    final_path.symlink_to(outside)

    with pytest.raises(FileExistsError, match="not a regular file"):
        write_payload_artifact(
            payload={"replacement": True},
            session_id="final-symlink",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert final_path.is_symlink()
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert not list(attempt_dir.glob(".prompt_payload.json.*.tmp"))


@pytest.mark.parametrize("node_kind", ["fifo", "directory"])
def test_artifact_writer_rejects_existing_nonregular_final_node(tmp_path, node_kind):
    attempt_dir = (
        tmp_path
        / "artifacts"
        / "assembly"
        / f"final-{node_kind}"
        / "parameter_attempt_0"
    )
    attempt_dir.mkdir(parents=True)
    final_path = attempt_dir / "prompt_payload.json"
    if node_kind == "fifo":
        os.mkfifo(final_path)
    else:
        final_path.mkdir()

    with pytest.raises(FileExistsError, match="not a regular file"):
        write_payload_artifact(
            payload={"replacement": True},
            session_id=f"final-{node_kind}",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert not list(attempt_dir.glob(".prompt_payload.json.*.tmp"))


@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt("writer interrupted"), SystemExit("writer exiting")],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_artifact_writer_base_exception_preserves_previous_and_cleans_temp(
    tmp_path, monkeypatch, failure
):
    previous = {"version": "complete-previous"}
    artifact = write_payload_artifact(
        payload=previous,
        session_id="base-exception",
        attempt_id=0,
        data_dir=tmp_path,
    )
    artifact_path = tmp_path / artifact.path_ref.removeprefix("data/")

    def _fail_after_first_byte(handle, serialized):
        handle.write(serialized[:1])
        handle.flush()
        raise failure

    monkeypatch.setattr(
        prompt_payload_module,
        "_write_and_sync_json",
        _fail_after_first_byte,
    )

    with pytest.raises(type(failure)) as caught:
        write_payload_artifact(
            payload={"version": "incomplete-new"},
            session_id="base-exception",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert str(caught.value) == str(failure)
    assert artifact_path.read_bytes() == json.dumps(previous, indent=2).encode("utf-8")
    assert not list(artifact_path.parent.glob(".prompt_payload.json.*.tmp"))


def test_open_unique_temp_file_fsyncs_unlink_after_fdopen_keyboard_interrupt(
    tmp_path,
    monkeypatch,
):
    events = []
    original_unlink = os.unlink
    original_sync = prompt_payload_module._fsync_directory

    def _interrupt_fdopen(_fd, _mode):
        raise KeyboardInterrupt("fdopen interrupted")

    def _tracked_unlink(path, **kwargs):
        events.append(("unlink", path))
        return original_unlink(path, **kwargs)

    def _tracked_sync(directory_fd):
        events.append(("fsync", directory_fd))
        return original_sync(directory_fd)

    monkeypatch.setattr(prompt_payload_module.os, "fdopen", _interrupt_fdopen)
    monkeypatch.setattr(prompt_payload_module.os, "unlink", _tracked_unlink)
    monkeypatch.setattr(prompt_payload_module, "_fsync_directory", _tracked_sync)

    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(KeyboardInterrupt, match="fdopen interrupted"):
            prompt_payload_module._open_unique_temp_file(
                directory_fd,
                "prompt_payload.json",
            )
    finally:
        os.close(directory_fd)

    assert [event[0] for event in events] == ["unlink", "fsync"]
    assert not list(tmp_path.glob(".prompt_payload.json.*.tmp"))


def test_artifact_ref_validation_finishes_before_publication(tmp_path, monkeypatch):
    def _reject_ref(**_kwargs):
        raise RuntimeError("invalid artifact ref")

    monkeypatch.setattr(prompt_payload_module, "ArtifactRef", _reject_ref)

    with pytest.raises(RuntimeError, match="invalid artifact ref"):
        write_payload_artifact(
            payload={"value": 1},
            session_id="prepublish-validation",
            attempt_id=0,
            data_dir=tmp_path,
        )

    assert not list(tmp_path.rglob("prompt_payload.json"))
    assert not list(tmp_path.rglob(".prompt_payload.json.*.tmp"))


def test_new_artifact_publication_does_not_read_shared_final_path(tmp_path, monkeypatch):
    def _reject_final_read(_path):
        raise AssertionError("new publication must not read the shared final path")

    monkeypatch.setattr(Path, "read_bytes", _reject_final_read)

    artifact = write_payload_artifact(
        payload={"hash_source": "prepublication-bytes"},
        session_id="no-final-read",
        attempt_id=0,
        data_dir=tmp_path,
    )

    expected = json.dumps({"hash_source": "prepublication-bytes"}, indent=2).encode(
        "utf-8"
    )
    assert artifact.sha256 == hashlib.sha256(expected).hexdigest()
    artifact_path = tmp_path / artifact.path_ref.removeprefix("data/")
    with artifact_path.open("rb") as handle:
        assert handle.read() == expected


def test_artifact_writer_syncs_file_then_publishes_then_syncs_parent(
    tmp_path, monkeypatch
):
    attempt_dir = (
        tmp_path
        / "artifacts"
        / "assembly"
        / "sync-order"
        / "parameter_attempt_0"
    )
    attempt_dir.mkdir(parents=True)
    events = []
    original_fsync = os.fsync
    original_link = os.link
    original_unlink = os.unlink

    def _tracked_fsync(fd):
        event = "directory_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync"
        events.append(event)
        return original_fsync(fd)

    def _tracked_link(source, destination, **kwargs):
        events.append("publish_link")
        return original_link(source, destination, **kwargs)

    def _tracked_unlink(path, **kwargs):
        if str(path).startswith(".prompt_payload.json."):
            events.append("temp_unlink")
        return original_unlink(path, **kwargs)

    monkeypatch.setattr(prompt_payload_module.os, "fsync", _tracked_fsync)
    monkeypatch.setattr(prompt_payload_module.os, "link", _tracked_link)
    monkeypatch.setattr(prompt_payload_module.os, "unlink", _tracked_unlink)

    write_payload_artifact(
        payload={"durable": True},
        session_id="sync-order",
        attempt_id=0,
        data_dir=tmp_path,
    )

    assert events == [
        "file_fsync",
        "publish_link",
        "directory_fsync",
        "temp_unlink",
        "directory_fsync",
    ]


def test_artifact_writer_fsyncs_parent_after_each_new_directory(tmp_path, monkeypatch):
    events = []
    original_mkdir = os.mkdir
    original_sync = prompt_payload_module._fsync_directory

    def _tracked_mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        events.append(f"mkdir:{path}")
        return result

    def _tracked_sync(directory_fd):
        events.append("parent_fsync")
        return original_sync(directory_fd)

    monkeypatch.setattr(prompt_payload_module.os, "mkdir", _tracked_mkdir)
    monkeypatch.setattr(prompt_payload_module, "_fsync_directory", _tracked_sync)

    write_payload_artifact(
        payload={"durable_directories": True},
        session_id="mkdir-sync",
        attempt_id=0,
        data_dir=tmp_path,
    )

    assert events[:8] == [
        "mkdir:artifacts",
        "parent_fsync",
        "mkdir:assembly",
        "parent_fsync",
        "mkdir:mkdir-sync",
        "parent_fsync",
        "mkdir:parameter_attempt_0",
        "parent_fsync",
    ]


def test_hifiasm_limits_final_methods_evidence_to_eight_cases(
    sample_catalog, sample_full_profile, tmp_path, monkeypatch
):
    evidence_cases = [
        _legal_hifiasm_case(
            f"case_{index:02d}",
            quote=f"Exact excerpt {index}.",
            distance=index / 100,
        )
        for index in range(12)
    ]
    candidate_ids = [case["case_id"] for case in evidence_cases]
    selection = _attested_numeric_selection(
        evidence_cases,
        query_id="s1:parameter_attempt_0",
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.load_knowledge_pack",
        lambda *_args, **_kwargs: {
            "cases": [],
            "rules": {"rules": []},
            "skill": "Use conservative evidence.",
            "strategies": {},
            "pack_root": tmp_path,
        },
    )
    monkeypatch.setattr(
        "app.stages.assembly.prompt_payload.select_evidence_cases",
        lambda *_args, **_kwargs: (evidence_cases, selection),
    )

    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=sample_catalog.model_copy(update={"method_id": "hifiasm"}),
        full_profile=sample_full_profile,
        task="recommend_initial",
        write_artifacts=True,
        session_id="s1",
        attempt_id=0,
        data_dir=tmp_path,
    )

    assert payload["case_selection"]["candidate_case_ids"] == candidate_ids
    assert payload["case_selection"]["evidence_case_ids"] == candidate_ids[:8]
    assert [case["case_id"] for case in payload["evidence_cases"]] == candidate_ids[:8]
    evidence_path = tmp_path / payload["evidence_payload_ref"].removeprefix("data/")
    loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert loaded["candidate_case_ids"] == candidate_ids[:8]
    assert loaded["retrieval_candidate_case_ids"] == candidate_ids
