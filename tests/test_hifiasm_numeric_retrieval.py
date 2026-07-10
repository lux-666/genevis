"""Tests for continuous, missing-aware hifiasm EvidenceCase retrieval."""

from __future__ import annotations

import math

from app.domains.assembly.numeric_case_retriever import HifiasmNumericCaseRetriever


def _case(case_id: str, **facts: float) -> dict:
    return {
        "schema_version": 2,
        "case_id": case_id,
        "case_type": "initial",
        "source": {"kind": "paper", "title": case_id},
        "applicability": {
            "read_type": "hifi",
            "source_tool": "hifiasm",
            "target_methods": ["assembly.candidate.hifiasm"],
            "assembly_goal": "primary",
        },
        "observed_facts": {name: {"value": value} for name, value in facts.items()},
        "intervention": {"tool": "hifiasm", "parameters": {}},
        "rationale_claims": [],
        "outcomes": {},
        "source_excerpts": [],
        "retrieval_eligible": True,
        "curation": {"status": "approved"},
    }


def _query(**features: float) -> dict:
    return {
        "method_id": "assembly.candidate.hifiasm",
        "task": "recommend_initial",
        "read_type": "hifi",
        "assembly_goal": "primary",
        "numeric_features": {name: {"value": value} for name, value in features.items()},
        "categorical_context": {},
    }


def test_coverage_values_across_old_bucket_boundary_are_continuous():
    result = HifiasmNumericCaseRetriever().select(
        [_case("below", coverage=29.9), _case("above", coverage=30.1)],
        query=_query(coverage=30.0),
    )
    distances = {
        item["case_id"]: item["numeric_distance"]
        for item in result.trace
        if item["decision"] == "candidate"
    }

    assert abs(distances["below"] - distances["above"]) < 0.001
    assert max(distances.values()) < 0.01


def test_log_ratio_distance_is_scale_invariant_for_doubling():
    retriever = HifiasmNumericCaseRetriever()
    low = retriever.select([_case("low", genome_size_mb=100)], query=_query(genome_size_mb=200))
    high = retriever.select([_case("high", genome_size_mb=1000)], query=_query(genome_size_mb=2000))

    low_distance = low.trace[0]["numeric_distance"]
    high_distance = high.trace[0]["numeric_distance"]
    assert math.isclose(low_distance, high_distance, rel_tol=1e-9)


def test_missing_values_reduce_coverage_and_do_not_improve_rank():
    result = HifiasmNumericCaseRetriever().select(
        [
            _case("complete", coverage=30, genome_size_mb=1000),
            _case("missing", coverage=30),
        ],
        query=_query(coverage=30, genome_size_mb=1000),
    )

    assert result.metadata["selected_case_ids"][:2] == ["complete", "missing"]
    trace = {item["case_id"]: item for item in result.trace if item["decision"] == "candidate"}
    assert trace["complete"]["numeric_distance"] == trace["missing"]["numeric_distance"] == 0
    assert trace["complete"]["evidence_coverage"] > trace["missing"]["evidence_coverage"]
    assert trace["missing"]["feature_distances"]["genome_size_mb"]["status"] == "missing_case_value"


def test_interval_contains_query_and_uses_nearest_boundary_outside():
    interval_case = _case("interval")
    interval_case["observed_facts"]["coverage"] = {
        "value": None,
        "uncertainty": {"kind": "interval", "min": 28, "max": 34},
    }
    retriever = HifiasmNumericCaseRetriever()

    inside = retriever.select([interval_case], query=_query(coverage=31))
    outside = retriever.select([interval_case], query=_query(coverage=40))

    assert inside.trace[0]["numeric_distance"] == 0
    assert outside.trace[0]["numeric_distance"] > 0


def test_no_approved_cases_returns_non_blocking_fallback_metadata():
    draft = _case("draft", coverage=30)
    draft["curation"]["status"] = "draft"

    result = HifiasmNumericCaseRetriever().select([draft], query=_query(coverage=30))

    assert result.selected_cases == []
    assert result.metadata["evidence_available"] is False
    assert result.metadata["case_evidence_required"] is False
    assert "readonly L1 facts" in result.metadata["warning"]


def test_approved_case_without_validated_sources_has_distinct_filter_reason():
    case = _case("unvalidated", coverage=30)
    case["retrieval_eligible"] = False

    result = HifiasmNumericCaseRetriever().select([case], query=_query(coverage=30))

    assert result.selected_cases == []
    assert result.trace[0]["hard_filter_reasons"] == ["source_not_validated"]


def test_hifiasm_prompt_succeeds_without_cases_and_preserves_level_contract():
    from app.stages.assembly.parameter_catalog import load_parameter_catalog
    from app.stages.assembly.prompt_payload import build_prompt_payload

    catalog = load_parameter_catalog("hifiasm")
    profile = {
        "sample": {"sample_id": "no_case", "ploidy": 2, "clade": "vertebrate"},
        "assets": {"read_type": "hifi"},
        "read_qc": {"effective_coverage_1kb": 30.0, "read_n50": 18000},
        "genome_survey": {"estimated_genome_size": 1200000000, "heterozygosity": 0.01},
        "previous_attempt": {},
        "assembly_review": {},
    }

    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=profile,
        task="recommend_initial",
    )

    assert payload["selected_cases"] == []
    assert payload["evidence_cases"] == []
    assert payload["case_retrieval"]["retrieval_backend"] == "hifiasm_numeric"
    assert payload["case_retrieval"]["evidence_available"] is False
    assert payload["recommendation_policy"]["case_evidence_required"] is False
    assert all(spec["level"] == 1 for spec in payload["readonly_params"].values())
    assert all(spec["level"] == 2 for spec in payload["adjustable_params"].values())
    l0_keys = {key for key, spec in catalog.parameters.items() if spec.level == 0}
    assert not l0_keys.intersection(payload["readonly_params"])
    assert not l0_keys.intersection(payload["adjustable_params"])
