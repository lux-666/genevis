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
        "schema_version": 3,
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
                "methods_path",
                "evidence_spans",
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


def test_method_mismatch_is_scored_instead_of_hard_filtered():
    wrong_tool = _case("wrong_tool", coverage=30)
    wrong_tool["applicability"]["source_tool"] = "flye"
    wrong_tool["applicability"]["target_methods"] = ["flye"]

    result = HifiasmNumericCaseRetriever().select(
        [wrong_tool],
        query=_query(coverage=30),
    )

    trace = result.trace[0]
    assert trace["decision"] == "candidate"
    assert trace["hard_filter_reasons"] == []
    assert trace["method_match"] is False
    assert trace["read_type_match"] is True
    assert trace["categorical_match_score"] == 1.0
    assert trace["ranking_score"] == pytest.approx(1.2)


def test_missing_query_read_type_remains_missing_without_excluding_case():
    query = _query(coverage=30)
    query["read_type"] = None

    result = HifiasmNumericCaseRetriever().select(
        [_case("case", coverage=30)],
        query=query,
    )

    assert [case["case_id"] for case in result.selected_cases] == ["case"]
    assert result.trace[0]["hard_filter_reasons"] == []
    assert result.trace[0]["read_type_match"] is False
    assert result.trace[0]["categorical_match_score"] == 1.0


def test_method_and_read_type_matches_rank_without_discrete_filtering():
    exact = _case("exact", coverage=30)
    method_only = _case("method_only", coverage=30)
    method_only["applicability"]["read_type"] = "ont"
    read_type_only = _case("read_type_only", coverage=30)
    read_type_only["applicability"]["target_methods"] = ["flye"]
    neither = _case("neither", coverage=30)
    neither["applicability"].update(
        {"read_type": "ont", "target_methods": ["nextdenovo"]}
    )

    result = HifiasmNumericCaseRetriever(max_cases=4).select(
        [neither, read_type_only, method_only, exact],
        query=_query(coverage=30),
    )

    assert result.metadata["eligible_case_count"] == 4
    assert [case["case_id"] for case in result.selected_cases] == [
        "exact",
        "method_only",
        "read_type_only",
        "neither",
    ]
    trace = {item["case_id"]: item for item in result.trace}
    assert trace["exact"]["categorical_match_score"] == 2.0
    assert trace["method_only"]["categorical_match_score"] == 1.0
    assert trace["read_type_only"]["categorical_match_score"] == 1.0
    assert trace["neither"]["categorical_match_score"] == 0.0
    assert trace["exact"]["ranking_score"] == pytest.approx(2.2)
    assert trace["method_only"]["ranking_score"] == pytest.approx(1.2)
    assert trace["read_type_only"]["ranking_score"] == pytest.approx(1.2)
    assert trace["neither"]["ranking_score"] == pytest.approx(0.2)
    assert all(item["hard_filter_reasons"] == [] for item in trace.values())


def test_internal_and_external_hifiasm_ont_names_share_method_identity():
    ont_case = _case("ont", coverage=30)
    ont_case["applicability"].update(
        {"read_type": "ont", "target_methods": ["hifiasm_ont"]}
    )
    hifi_case = _case("hifi", coverage=30)

    query = _query(coverage=30)
    query.update({"method_id": "hifiasm-ont", "read_type": "ont"})
    result = HifiasmNumericCaseRetriever(max_cases=2).select(
        [hifi_case, ont_case], query=query
    )

    assert [case["case_id"] for case in result.selected_cases] == ["ont", "hifi"]
    trace = {item["case_id"]: item for item in result.trace}
    assert trace["ont"]["method_match"] is True
    assert trace["ont"]["read_type_match"] is True
    assert trace["hifi"]["method_match"] is False
    assert trace["hifi"]["read_type_match"] is False


def test_assembly_goal_and_topology_metadata_do_not_filter_or_score_cases():
    case = _case("documentary_context", coverage=30)
    case["applicability"].update(
        {
            "assembly_goal": "near_t2t",
            "assembly_topology": "hifi_ul_hic_trio",
        }
    )
    query = _query(coverage=30)
    query.update(
        {
            "assembly_goal": "primary",
            "assembly_topology": "hifi_only",
        }
    )

    result = HifiasmNumericCaseRetriever().select([case], query=query)

    assert [item["case_id"] for item in result.selected_cases] == [
        "documentary_context"
    ]
    assert result.trace[0]["hard_filter_reasons"] == []
    assert result.trace[0]["categorical_match_score"] == 2.0


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
            "scales": {"coverage": math.log(1.25)},
            "weights": {"coverage": 3.0},
        },
        max_cases=5,
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


def _dispatch_initial(monkeypatch, cases, *, max_cases=None, numeric_config=None):
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: values,
    )
    return case_selector_module.select_evidence_cases(
        cases,
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        prompt_profile={
            "assets": {"read_type": "hifi"},
            "read_qc": {"effective_coverage_1kb": 30},
        },
        max_cases=max_cases,
        strategy={"hifiasm_numeric": {"initial_cutover_enabled": True, **(numeric_config or {})}},
    )


def test_hifiasm_initial_cutover_selects_stable_top_five_for_tied_shuffled_cases(monkeypatch):
    cases = [_case(case_id, coverage=30) for case_id in ("g", "a", "f", "c", "b", "e", "d")]
    selected, metadata = _dispatch_initial(monkeypatch, cases, max_cases=1)

    assert [case["case_id"] for case in selected] == ["a", "b", "c", "d", "e"]
    assert metadata["retrieval_backend"] == "hifiasm_numeric"
    assert metadata["hifiasm_numeric_gate"]["decision"] == "hifiasm_numeric"
    assert metadata["eligible_case_count"] == 7

    reversed_selected, _ = _dispatch_initial(monkeypatch, list(reversed(cases)))
    assert [case["case_id"] for case in reversed_selected] == ["a", "b", "c", "d", "e"]


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_hifiasm_initial_cutover_selects_every_eligible_case_when_under_five(monkeypatch, count):
    selected, metadata = _dispatch_initial(
        monkeypatch,
        [_case(f"case_{index}", coverage=30) for index in range(count)],
    )

    assert len(selected) == count
    assert metadata["selected_count"] == count


def test_hifiasm_initial_cutover_has_no_score_or_coverage_threshold(monkeypatch):
    monkeypatch.setattr(
        case_selector_module,
        "build_hifiasm_retrieval_query",
        lambda *_args, **_kwargs: _query(),
    )
    selected, metadata = _dispatch_initial(monkeypatch, [_case("missing_everything")])

    assert [case["case_id"] for case in selected] == ["missing_everything"]
    assert metadata["evidence_available"] is True
    assert metadata["trace"][0]["numeric_similarity_display"] == 0.0
    assert metadata["trace"][0]["evidence_coverage"] == 0.0


def test_hifiasm_numeric_cutover_flag_is_fail_closed(monkeypatch):
    legacy = {
        "case_id": "legacy_hifiasm_seed",
        "features": {"read_type": "hifi", "coverage": 30},
        "params": {"tool": "hifiasm"},
    }
    draft_v3 = _case("draft_v3", coverage=30)
    draft_v3["curation"]["status"] = "draft"
    draft_v3.update(
        {
            "features": {"read_type": "hifi", "coverage": 30},
            "params": {"tool": "hifiasm"},
        }
    )
    rejected_v3 = _case("rejected_v3", coverage=30)
    rejected_v3["curation"]["status"] = "rejected"
    rejected_v3.update(
        {
            "features": {"read_type": "hifi", "coverage": 30},
            "params": {"tool": "hifiasm"},
        }
    )
    invalid_v3 = {
        "schema_version": 3,
        "case_id": "invalid_v3",
        "features": {"read_type": "hifi", "coverage": 30},
        "params": {"tool": "hifiasm"},
    }
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: pytest.fail("disabled cutover must not validate curated cases"),
    )
    monkeypatch.setattr(
        case_selector_module,
        "is_hifiasm_evidence_case",
        lambda case: case.get("schema_version") == 3,
    )

    selected, metadata = case_selector_module.select_evidence_cases(
        [legacy, draft_v3, rejected_v3, invalid_v3],
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        prompt_profile={"assets": {"read_type": "hifi"}},
        strategy={"hifiasm_numeric": {"initial_cutover_enabled": False}},
    )

    assert [case["case_id"] for case in selected] == ["legacy_hifiasm_seed"]
    assert metadata["total_cases"] == 1
    assert [entry["case_id"] for entry in metadata["trace"]] == ["legacy_hifiasm_seed"]
    assert metadata["retrieval_backend"] == "structured_default"
    assert metadata["hifiasm_numeric_gate"]["reason"] == "initial_cutover_disabled"


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
        strategy={"hifiasm_numeric": {"initial_cutover_enabled": True}},
    )

    assert selected == []
    assert metadata["retrieval_backend"] == "hifiasm_numeric"
    assert metadata["evidence_available"] is False
    assert metadata["hifiasm_numeric_gate"] == {
        "initial_cutover_enabled": True,
        "decision": "hifiasm_numeric",
        "reason": "initial_cutover_enabled",
    }


@pytest.mark.parametrize(
    "task",
    ["retry_after_execution_failure", "retry_after_review_failure"],
)
def test_hifiasm_retry_never_enters_a_selector(monkeypatch, task):
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda *_args, **_kwargs: pytest.fail("retry must not validate a case corpus"),
    )
    monkeypatch.setattr(
        case_selector_module.HifiasmNumericCaseRetriever,
        "from_config",
        lambda *_args, **_kwargs: pytest.fail("retry must not construct a selector"),
    )

    with pytest.raises(ValueError, match="do not use evidence-case selection"):
        case_selector_module.select_evidence_cases(
            [],
            method_id="assembly.candidate.hifiasm",
            task=task,
            prompt_profile={"assets": {"read_type": "hifi"}},
            strategy={"hifiasm_numeric": {"initial_cutover_enabled": True}},
        )


@pytest.mark.parametrize(
    ("numeric_config", "message"),
    [
        ([], "hifiasm_numeric strategy must be a mapping"),
        ({"initial_cutover_enabled": "yes"}, "initial_cutover_enabled must be a bool"),
        (
            {"initial_cutover_enabled": True, "candidate_budget": 5},
            "unknown keys.*candidate_budget",
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
            strategy={"hifiasm_numeric": {"initial_cutover_enabled": True}},
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
    assert query["method_id"] == "assembly.candidate.hifiasm"
    assert query["read_type"] == "hifi"
    assert "assembly_goal" not in query
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


def test_query_adapter_normalizes_ont_without_using_it_as_a_filter():
    query = build_hifiasm_retrieval_query(
        {"assets": {"read_type": "ont"}},
        method_id="assembly.candidate.hifiasm-ont",
        task="recommend_initial",
    )

    assert query["method_id"] == "assembly.candidate.hifiasm-ont"
    assert query["read_type"] == "ont"


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


def test_underfilled_shortlist_warns_even_with_complete_evidence():
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
