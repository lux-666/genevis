"""Tests for continuous, missing-aware hifiasm EvidenceCase retrieval."""

from __future__ import annotations

import math
from copy import deepcopy

import pytest

from app.domains.assembly import case_selector as case_selector_module
from app.domains.assembly import numeric_case_retriever as numeric_retriever_module
from app.domains.assembly.numeric_case_retriever import HifiasmNumericCaseRetriever
from app.domains.assembly.retrieval_query import build_hifiasm_retrieval_query


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


@pytest.fixture(autouse=True)
def _isolate_numeric_algorithm_from_task2_attestation(monkeypatch):
    """Keep this module focused on Task 3 math; full-chain tests use real proof."""

    monkeypatch.setattr(
        numeric_retriever_module,
        "project_hifiasm_retrieval_case",
        deepcopy,
    )

    def _project(case: dict, *, numeric_match=None) -> dict:
        projected = {
            key: deepcopy(value)
            for key, value in case.items()
            if key
            not in {
                "curation",
                "numeric_match",
                "retrieval_eligible",
                "source_documents",
            }
        }
        if numeric_match is not None:
            projected["numeric_match"] = deepcopy(numeric_match)
        return projected

    monkeypatch.setattr(numeric_retriever_module, "_rank_hifiasm_prompt_case", _project)
    monkeypatch.setattr(
        numeric_retriever_module,
        "_project_hifiasm_ranked_prompt_case",
        deepcopy,
    )


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


def test_complete_candidate_is_not_displaced_by_one_field_exact_match():
    result = HifiasmNumericCaseRetriever(max_cases=1).select(
        [
            _case(
                "complete",
                genome_size_mb=1005,
                coverage=30.3,
                read_n50_kb=18.1,
                heterozygosity=0.0101,
                ploidy=2,
            ),
            _case("weak_exact", coverage=30),
        ],
        query=_query(
            genome_size_mb=1000,
            coverage=30,
            read_n50_kb=18,
            heterozygosity=0.01,
            ploidy=2,
        ),
    )

    assert result.metadata["selected_case_ids"] == ["complete"]
    trace = {item["case_id"]: item for item in result.trace}
    assert trace["complete"]["numeric_distance"] > trace["weak_exact"]["numeric_distance"]
    assert trace["complete"]["evidence_coverage"] > trace["weak_exact"]["evidence_coverage"]


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


def test_near_zero_heterozygosity_distance_is_finite():
    result = HifiasmNumericCaseRetriever().select(
        [_case("near_zero", heterozygosity=0.0)],
        query=_query(heterozygosity=1e-12),
    )

    assert math.isfinite(result.trace[0]["numeric_distance"])


def test_hard_filters_skip_numeric_comparison_and_mark_every_feature():
    wrong_tool = _case("wrong_tool", coverage=30)
    wrong_tool["applicability"]["source_tool"] = "flye"

    result = HifiasmNumericCaseRetriever().select(
        [wrong_tool],
        query=_query(coverage=30),
    )

    trace = result.trace[0]
    assert trace["decision"] == "excluded"
    assert trace["hard_filter_reasons"] == ["source_tool_mismatch"]
    assert set(trace["feature_distances"]) == {
        "genome_size_mb",
        "coverage",
        "read_n50_kb",
        "heterozygosity",
        "ploidy",
    }
    assert {
        item["status"] for item in trace["feature_distances"].values()
    } == {"not_compared_hard_filter"}


def test_missing_query_read_type_is_hard_filtered():
    query = _query(coverage=30)
    query["read_type"] = None

    result = HifiasmNumericCaseRetriever().select(
        [_case("case", coverage=30)],
        query=query,
    )

    assert result.selected_cases == []
    assert result.trace[0]["hard_filter_reasons"] == ["read_type_mismatch"]


def test_clade_tie_break_cannot_override_numeric_distance():
    numeric_match = _case("numeric_match", coverage=30)
    numeric_match["observed_facts"]["clade"] = {"value": "plant"}
    clade_match = _case("clade_match", coverage=300)
    clade_match["observed_facts"]["clade"] = {"value": "vertebrate"}
    query = _query(coverage=30)
    query["categorical_context"]["clade"] = "vertebrate"

    result = HifiasmNumericCaseRetriever().select(
        [clade_match, numeric_match],
        query=query,
    )

    assert result.metadata["selected_case_ids"][:2] == ["numeric_match", "clade_match"]


def test_numeric_ties_are_stable_by_case_id():
    result = HifiasmNumericCaseRetriever().select(
        [_case("case_b", coverage=30), _case("case_a", coverage=30)],
        query=_query(coverage=30),
    )

    assert result.metadata["selected_case_ids"][:2] == ["case_a", "case_b"]


def test_candidate_trace_explains_compared_and_missing_features():
    result = HifiasmNumericCaseRetriever().select(
        [_case("partial", coverage=30, genome_size_mb=1000)],
        query=_query(coverage=30, read_n50_kb=18),
    )

    features = result.trace[0]["feature_distances"]
    assert features["coverage"]["status"] == "compared"
    assert features["genome_size_mb"]["status"] == "missing_query_value"
    assert features["read_n50_kb"]["status"] == "missing_case_value"
    assert features["heterozygosity"]["status"] == "missing_query_value"
    assert features["ploidy"]["status"] == "missing_query_value"


def test_numeric_scales_and_weights_are_configuration_backed():
    default = HifiasmNumericCaseRetriever().select(
        [_case("case", coverage=60)],
        query=_query(coverage=30),
    )
    configured_retriever = HifiasmNumericCaseRetriever.from_config(
        {
            "candidate_budget": 5,
            "scales": {"coverage": math.log(1.25)},
            "weights": {"coverage": 3.0},
        }
    )
    configured = configured_retriever.select(
        [_case("case", coverage=60)],
        query=_query(coverage=30),
    )

    assert configured_retriever.max_cases == 5
    assert configured.trace[0]["numeric_distance"] > default.trace[0]["numeric_distance"]


def test_numeric_strategy_requires_a_mapping_even_when_falsy():
    with pytest.raises(ValueError, match="must be a mapping"):
        HifiasmNumericCaseRetriever.from_config([])


def test_non_finite_numeric_configuration_is_rejected():
    with pytest.raises(ValueError, match="must be a number"):
        HifiasmNumericCaseRetriever(scales={"coverage": math.inf})


@pytest.mark.parametrize(
    "task",
    ["retry_after_execution_failure", "retry_after_review_failure", "not_a_parameter_task"],
)
def test_query_adapter_rejects_non_initial_tasks(task):
    with pytest.raises(ValueError, match="only supports task='recommend_initial'"):
        build_hifiasm_retrieval_query(
            {"assets": {"read_type": "hifi"}},
            method_id="assembly.candidate.hifiasm",
            task=task,
        )


@pytest.mark.parametrize(
    "task",
    ["retry_after_execution_failure", "retry_after_review_failure", "not_a_parameter_task"],
)
def test_numeric_retriever_rejects_non_initial_queries(task):
    query = _query(coverage=30)
    query["task"] = task

    with pytest.raises(ValueError, match="only supports task='recommend_initial'"):
        HifiasmNumericCaseRetriever().select([_case("case", coverage=30)], query=query)


def test_hifiasm_dispatch_uses_configured_candidate_budget(monkeypatch):
    cases = [_case(f"case_{index:02d}", coverage=30 + index) for index in range(10)]
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: values,
    )

    selected, metadata = case_selector_module.select_evidence_cases(
        cases,
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        prompt_profile={
            "assets": {"read_type": "hifi"},
            "read_qc": {"effective_coverage_1kb": 30},
        },
        strategy={
            "hifiasm_numeric": {
                "production_cutover_enabled": True,
                "candidate_budget": 12,
            }
        },
    )

    assert len(selected) == 10
    assert metadata["retrieval_backend"] == "hifiasm_numeric"
    assert metadata["hifiasm_numeric_gate"]["decision"] == "hifiasm_numeric"
    assert metadata["eligible_case_count"] == 10
    assert "Only 10 of 12 requested" in metadata["warning"]


def test_hifiasm_numeric_cutover_flag_is_fail_closed(monkeypatch):
    legacy = {
        "case_id": "legacy_hifiasm_seed",
        "features": {"read_type": "hifi", "coverage": 30},
        "params": {"tool": "hifiasm"},
    }
    draft_v2 = _case("draft_v2", coverage=30)
    draft_v2["curation"]["status"] = "draft"
    draft_v2.update(
        {
            "features": {"read_type": "hifi", "coverage": 30},
            "params": {"tool": "hifiasm"},
        }
    )
    rejected_v2 = _case("rejected_v2", coverage=30)
    rejected_v2["curation"]["status"] = "rejected"
    rejected_v2.update(
        {
            "features": {"read_type": "hifi", "coverage": 30},
            "params": {"tool": "hifiasm"},
        }
    )
    invalid_v2 = {
        "schema_version": 2,
        "case_id": "invalid_v2",
        "features": {"read_type": "hifi", "coverage": 30},
        "params": {"tool": "hifiasm"},
    }
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: pytest.fail("disabled cutover must not validate curated cases"),
    )

    selected, metadata = case_selector_module.select_evidence_cases(
        [legacy, draft_v2, rejected_v2, invalid_v2],
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        prompt_profile={"assets": {"read_type": "hifi"}},
        strategy={"hifiasm_numeric": {"production_cutover_enabled": False}},
    )

    assert [case["case_id"] for case in selected] == ["legacy_hifiasm_seed"]
    assert metadata["total_cases"] == 1
    assert [entry["case_id"] for entry in metadata["trace"]] == ["legacy_hifiasm_seed"]
    assert metadata["retrieval_backend"] == "structured_default"
    assert metadata["hifiasm_numeric_gate"]["reason"] == "production_cutover_disabled"


def test_hifiasm_numeric_cutover_handles_empty_corpus_without_legacy_fallback(monkeypatch):
    legacy = {
        "case_id": "legacy_hifiasm_seed",
        "features": {"read_type": "hifi", "coverage": 30},
        "params": {"tool": "hifiasm"},
    }
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: [],
    )

    selected, metadata = case_selector_module.select_evidence_cases(
        [legacy],
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        prompt_profile={"assets": {"read_type": "hifi"}},
        strategy={"hifiasm_numeric": {"production_cutover_enabled": True}},
    )

    assert selected == []
    assert metadata["retrieval_backend"] == "hifiasm_numeric"
    assert metadata["evidence_available"] is False
    assert metadata["hifiasm_numeric_gate"] == {
        "production_cutover_enabled": True,
        "decision": "hifiasm_numeric",
        "reason": "production_cutover_enabled",
    }


@pytest.mark.parametrize(
    "task",
    ["retry_after_execution_failure", "retry_after_review_failure"],
)
def test_hifiasm_retry_never_dispatches_to_task3_numeric_retrieval(monkeypatch, task):
    retry_case = {
        "case_id": "legacy_retry",
        "features": {"read_type": "hifi", "failure_mode": "fragmented"},
        "params": {"tool": "hifiasm"},
    }
    invalid_v2_retry = {
        "schema_version": 2,
        "case_id": "invalid_v2_retry",
        "case_type": "retry",
        "features": {"read_type": "hifi", "failure_mode": "fragmented"},
        "params": {"tool": "hifiasm"},
        "curation": {"status": "draft"},
    }
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: pytest.fail("Task 5 retry retrieval is not implemented"),
    )
    monkeypatch.setattr(
        case_selector_module,
        "build_hifiasm_retrieval_query",
        lambda *args, **kwargs: pytest.fail("Task 3 query building is initial-only"),
    )
    monkeypatch.setattr(
        case_selector_module.HifiasmNumericCaseRetriever,
        "from_config",
        lambda *args, **kwargs: pytest.fail("Task 3 numeric ranking is initial-only"),
    )

    selected, metadata = case_selector_module.select_evidence_cases(
        [retry_case, invalid_v2_retry],
        method_id="assembly.candidate.hifiasm",
        task=task,
        prompt_profile={"assets": {"read_type": "hifi"}},
        strategy={"hifiasm_numeric": {"production_cutover_enabled": True}},
    )

    assert [case["case_id"] for case in selected] == ["legacy_retry"]
    assert metadata["total_cases"] == 1
    assert [entry["case_id"] for entry in metadata["trace"]] == ["legacy_retry"]
    assert metadata["retrieval_backend"] == "structured_default"
    assert metadata["hifiasm_numeric_gate"]["reason"] == "retry_pending_task_5"


@pytest.mark.parametrize(
    ("numeric_config", "message"),
    [
        ([], "hifiasm_numeric strategy must be a mapping"),
        ({"production_cutover_enabled": "yes"}, "production_cutover_enabled must be a bool"),
        (
            {"production_cutover_enabled": True, "candidate_budget": 0},
            "candidate_budget must be a positive integer",
        ),
    ],
)
def test_hifiasm_numeric_cutover_rejects_invalid_active_configuration(
    monkeypatch, numeric_config, message
):
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: pytest.fail("invalid config must fail before corpus loading"),
    )

    with pytest.raises(ValueError, match=message):
        case_selector_module.select_evidence_cases(
            [],
            method_id="assembly.candidate.hifiasm",
            task="recommend_initial",
            prompt_profile={"assets": {"read_type": "hifi"}},
            strategy={"hifiasm_numeric": numeric_config},
        )


def test_hifiasm_dispatch_rejects_unknown_task_before_backend_selection(monkeypatch):
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: pytest.fail("invalid task must fail before retrieval"),
    )

    with pytest.raises(ValueError, match="unsupported hifiasm parameter task"):
        case_selector_module.select_evidence_cases(
            [],
            method_id="assembly.candidate.hifiasm",
            task="not_a_parameter_task",
            prompt_profile={"assets": {"read_type": "hifi"}},
            strategy={"hifiasm_numeric": {"production_cutover_enabled": True}},
        )


def test_query_adapter_records_units_precedence_and_source_paths():
    query = build_hifiasm_retrieval_query(
        {
            "sample": {"ploidy": 2, "clade": "vertebrate"},
            "assets": {"read_type": "hifi"},
            "read_qc": {
                "effective_coverage_1kb": 31.5,
                "effective_coverage_5kb": 29.0,
                "read_n50": 18700,
            },
            "genome_survey": {
                "estimated_genome_size": 1_200_000_000,
                "heterozygosity": 0.011,
            },
        },
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        query_id="session:parameter_attempt_2",
    )

    assert query["query_id"] == "session:parameter_attempt_2"
    assert query["numeric_features"]["genome_size_mb"] == {
        "value": 1200.0,
        "source_ref": "prompt_profile.genome_survey.estimated_genome_size",
    }
    assert query["numeric_features"]["coverage"] == {
        "value": 31.5,
        "source_ref": "prompt_profile.read_qc.effective_coverage_1kb",
    }
    assert query["numeric_features"]["read_n50_kb"]["value"] == 18.7


def test_query_adapter_treats_non_finite_values_as_missing():
    query = build_hifiasm_retrieval_query(
        {
            "assets": {"read_type": "hifi"},
            "read_qc": {"effective_coverage_1kb": math.nan},
            "genome_survey": {"estimated_genome_size": math.inf},
        },
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
    )

    assert query["numeric_features"]["coverage"]["value"] is None
    assert query["numeric_features"]["genome_size_mb"]["value"] is None


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


def test_any_weak_selected_case_produces_an_explicit_warning():
    result = HifiasmNumericCaseRetriever(max_cases=2).select(
        [
            _case(
                "complete",
                genome_size_mb=1000,
                coverage=30,
                read_n50_kb=18,
                heterozygosity=0.01,
                ploidy=2,
            ),
            _case("weak", coverage=30),
        ],
        query=_query(
            genome_size_mb=1000,
            coverage=30,
            read_n50_kb=18,
            heterozygosity=0.01,
            ploidy=2,
        ),
    )

    assert result.metadata["warning"] is not None
    assert "weak evidence coverage" in result.metadata["warning"]


def test_underfilled_candidate_budget_warns_even_with_complete_evidence():
    result = HifiasmNumericCaseRetriever(max_cases=2).select(
        [
            _case(
                "complete",
                genome_size_mb=1000,
                coverage=30,
                read_n50_kb=18,
                heterozygosity=0.01,
                ploidy=2,
            )
        ],
        query=_query(
            genome_size_mb=1000,
            coverage=30,
            read_n50_kb=18,
            heterozygosity=0.01,
            ploidy=2,
        ),
    )

    assert result.trace[0]["weak_evidence"] is False
    assert "Only 1 of 2 requested" in result.metadata["warning"]


def test_hifiasm_prompt_preserves_pre_cutover_backend_and_level_contract():
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

    assert payload["selected_cases"]
    assert payload["evidence_cases"] == []
    assert payload["case_retrieval"] is None
    assert payload["case_selection"]["retrieval_backend"] == "structured_default"
    assert (
        payload["case_selection"]["hifiasm_numeric_gate"]["reason"]
        == "production_cutover_disabled"
    )
    assert payload["recommendation_policy"]["case_evidence_required"] is False
    assert all(spec["level"] == 1 for spec in payload["readonly_params"].values())
    assert all(spec["level"] == 2 for spec in payload["adjustable_params"].values())
    l0_keys = {key for key, spec in catalog.parameters.items() if spec.level == 0}
    assert not l0_keys.intersection(payload["readonly_params"])
    assert not l0_keys.intersection(payload["adjustable_params"])
