"""Tests for continuous, missing-aware hifiasm EvidenceCase retrieval."""

from __future__ import annotations

import math
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domains.assembly import case_selector as case_selector_module
from app.domains.assembly import numeric_case_retriever as numeric_retriever_module
from app.domains.assembly.numeric_case_retriever import HifiasmNumericCaseRetriever
from app.domains.assembly.retrieval_query import build_hifiasm_retrieval_query
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalGenomeFacts,
    CanonicalReadFacts,
    CanonicalRecommendationFacts,
    CanonicalSequencingFacts,
    CanonicalSpeciesFacts,
    QuantityFact,
)


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
        "numeric_features": {
            name: {"value": value} for name, value in features.items()
        },
        "categorical_context": {},
    }


def _quantity(
    value: float | int,
    unit: str,
    *,
    kind: str,
    producer: str,
) -> QuantityFact:
    return QuantityFact.model_validate(
        {
            "value": value,
            "unit": unit,
            "origin": {"kind": kind, "producer": producer},
        }
    )


def _facts(
    *,
    genome_size_bp: int | None = None,
    coverage: float | None = None,
    read_n50_bp: int | None = None,
    heterozygosity: float | None = None,
    ploidy: int | None = None,
    clade: str | None = None,
    read_type: str | None = None,
) -> CanonicalRecommendationFacts:
    return CanonicalRecommendationFacts(
        species=CanonicalSpeciesFacts(
            clade=clade,
            ploidy=(
                _quantity(
                    ploidy,
                    "count",
                    kind="provided",
                    producer="run_config",
                )
                if ploidy is not None
                else None
            ),
        ),
        reads=CanonicalReadFacts(
            read_type=read_type,
            read_n50=(
                _quantity(
                    read_n50_bp,
                    "bp",
                    kind="measured",
                    producer="read_qc",
                )
                if read_n50_bp is not None
                else None
            ),
        ),
        genome=CanonicalGenomeFacts(
            effective_genome_size=(
                _quantity(
                    genome_size_bp,
                    "bp",
                    kind="resolved",
                    producer="genome_survey",
                )
                if genome_size_bp is not None
                else None
            ),
            heterozygosity=(
                _quantity(
                    heterozygosity,
                    "fraction",
                    kind="measured",
                    producer="genome_survey",
                )
                if heterozygosity is not None
                else None
            ),
        ),
        sequencing=CanonicalSequencingFacts(
            raw_coverage=(
                _quantity(
                    coverage,
                    "x",
                    kind="derived",
                    producer="read_qc",
                )
                if coverage is not None
                else None
            )
        ),
    )


def _context(
    *,
    read_type: str = "pacbio_hifi",
    assembly_route: str = "hifi",
) -> AssemblyRecommendationContext:
    return AssemblyRecommendationContext.model_validate(
        {
            "stage": "assembly",
            "assembly_route": assembly_route,
            "read_type": read_type,
            "target_level": "strict_t2t",
        }
    )


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
    low = retriever.select(
        [_case("low", genome_size_mb=100)], query=_query(genome_size_mb=200)
    )
    high = retriever.select(
        [_case("high", genome_size_mb=1000)], query=_query(genome_size_mb=2000)
    )

    low_distance = low.trace[0]["numeric_distance"]
    high_distance = high.trace[0]["numeric_distance"]
    assert math.isclose(low_distance, high_distance, rel_tol=1e-9)


def test_missing_values_renormalize_over_shared_dimensions():
    result = HifiasmNumericCaseRetriever().select(
        [
            _case("complete", coverage=30, genome_size_mb=1000),
            _case("missing", coverage=30),
        ],
        query=_query(coverage=30, genome_size_mb=1000),
    )

    assert result.metadata["selected_case_ids"][:2] == ["complete", "missing"]
    trace = {
        item["case_id"]: item
        for item in result.trace
        if item["decision"] == "candidate"
    }
    assert (
        trace["complete"]["numeric_distance"]
        == trace["missing"]["numeric_distance"]
        == 0
    )
    assert trace["complete"]["ranking_score"] == trace["missing"]["ranking_score"] == 1
    assert (
        trace["complete"]["evidence_coverage"] > trace["missing"]["evidence_coverage"]
    )
    assert (
        trace["missing"]["feature_distances"]["genome_size_mb"]["status"]
        == "missing_case_value"
    )


def test_numeric_similarity_does_not_penalize_missing_case_dimensions():
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

    assert result.metadata["selected_case_ids"] == ["weak_exact"]
    trace = {item["case_id"]: item for item in result.trace}
    assert (
        trace["complete"]["numeric_distance"] > trace["weak_exact"]["numeric_distance"]
    )
    assert (
        trace["complete"]["evidence_coverage"]
        > trace["weak_exact"]["evidence_coverage"]
    )


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


def test_method_mismatch_is_documentary_context_only():
    wrong_tool = _case("wrong_tool", coverage=30)
    wrong_tool["applicability"]["source_tool"] = "flye"
    wrong_tool["applicability"]["target_methods"] = ["flye"]

    result = HifiasmNumericCaseRetriever().select(
        [wrong_tool],
        query=_query(coverage=30),
    )

    trace = result.trace[0]
    assert trace["decision"] == "candidate"
    assert trace["eligibility_reasons"] == []
    assert trace["ranking_score"] == pytest.approx(1.0)
    assert "method_match" not in trace
    assert result.selected_cases[0]["applicability"]["source_tool"] == "flye"


def test_query_read_type_is_not_a_selection_feature():
    query = _query(coverage=30)
    query["read_type"] = None

    result = HifiasmNumericCaseRetriever().select(
        [_case("case", coverage=30)],
        query=query,
    )

    assert [case["case_id"] for case in result.selected_cases] == ["case"]
    assert result.trace[0]["eligibility_reasons"] == []
    assert "read_type_match" not in result.trace[0]


def test_method_and_read_type_do_not_score_or_filter_cases():
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
        "neither",
        "read_type_only",
    ]
    trace = {item["case_id"]: item for item in result.trace}
    assert {item["ranking_score"] for item in trace.values()} == {1.0}
    assert all(item["eligibility_reasons"] == [] for item in trace.values())
    assert all("categorical_match_score" not in item for item in trace.values())


def test_query_method_identity_does_not_change_literature_ranking():
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

    assert [case["case_id"] for case in result.selected_cases] == ["hifi", "ont"]
    trace = {item["case_id"]: item for item in result.trace}
    assert trace["ont"]["ranking_score"] == trace["hifi"]["ranking_score"]
    assert "method_match" not in trace["ont"]


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
    assert result.trace[0]["eligibility_reasons"] == []
    assert result.trace[0]["ranking_score"] == 1.0


def test_clade_does_not_override_numeric_distance():
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


def test_actionability_breaks_numeric_ties_without_rewriting_case_card():
    plain = _case("a_plain", coverage=30)
    actionable = _case("z_actionable", coverage=30)
    actionable["intervention"]["parameters"] = {
        "purge_level": {
            "value": 1,
            "reported": True,
            "evidence_span_ids": ["methods.command"],
        }
    }
    actionable["rationale_claims"] = [
        {
            "claim_id": "purge_reason",
            "statement": "The paper's rationale.",
            "explicitness": "explicit",
            "effect_area": "purging",
            "evidence_span_ids": ["methods.rationale"],
        }
    ]
    actionable["source_excerpts"] = [
        {"span_id": "methods.command", "document": "methods", "quote": "-l 1"},
        {
            "span_id": "methods.rationale",
            "document": "methods",
            "quote": "Purging was reduced for the heterozygous sample.",
        },
    ]

    result = HifiasmNumericCaseRetriever(max_cases=2).select(
        [plain, actionable],
        query=_query(coverage=30),
    )

    assert result.metadata["selected_case_ids"] == ["z_actionable", "a_plain"]
    card = result.selected_cases[0]
    assert card["intervention"] == actionable["intervention"]
    assert card["rationale_claims"] == actionable["rationale_claims"]
    assert card["source_excerpts"] == actionable["source_excerpts"]
    assert card["numeric_match"]["actionability_score"] == 1.0


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
    assert (
        configured.trace[0]["numeric_distance"] > default.trace[0]["numeric_distance"]
    )


def test_numeric_strategy_requires_a_mapping_even_when_falsy():
    with pytest.raises(ValueError, match="must be a mapping"):
        HifiasmNumericCaseRetriever.from_config([])


def test_non_finite_numeric_configuration_is_rejected():
    with pytest.raises(ValueError, match="must be a number"):
        HifiasmNumericCaseRetriever(scales={"coverage": math.inf})


@pytest.mark.parametrize(
    "task",
    [
        "retry_after_execution_failure",
        "retry_after_review_failure",
        "not_a_parameter_task",
    ],
)
def test_query_adapter_rejects_non_initial_tasks(task):
    with pytest.raises(ValueError, match="only supports task='recommend_initial'"):
        build_hifiasm_retrieval_query(
            _facts(),
            _context(),
            method_id="assembly.candidate.hifiasm",
            task=task,
        )


@pytest.mark.parametrize(
    "task",
    [
        "retry_after_execution_failure",
        "retry_after_review_failure",
        "not_a_parameter_task",
    ],
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
        facts=_facts(coverage=30, read_type="pacbio_hifi"),
        context=_context(),
        max_cases=max_cases,
        strategy={"hifiasm_numeric": numeric_config or {}},
    )


def test_hifiasm_numeric_selection_respects_budget_and_is_stable(monkeypatch):
    cases = [
        _case(case_id, coverage=30) for case_id in ("g", "a", "f", "c", "b", "e", "d")
    ]
    selected, metadata = _dispatch_initial(monkeypatch, cases, max_cases=1)

    assert [case["case_id"] for case in selected] == ["a"]
    assert metadata["retrieval_backend"] == "numeric_literature"
    assert metadata["eligible_case_count"] == 7

    reversed_selected, _ = _dispatch_initial(
        monkeypatch, list(reversed(cases)), max_cases=1
    )
    assert [case["case_id"] for case in reversed_selected] == ["a"]


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_hifiasm_initial_cutover_selects_every_eligible_case_when_under_five(
    monkeypatch, count
):
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
    assert selected[0]["numeric_match"]["numeric_similarity_display"] == 0.0
    assert selected[0]["numeric_match"]["evidence_coverage"] == 0.0


def test_hifiasm_numeric_cutover_handles_empty_corpus_without_legacy_fallback(
    monkeypatch,
):
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
        facts=_facts(read_type="pacbio_hifi"),
        context=_context(),
        strategy={"hifiasm_numeric": {}},
    )

    assert selected == []
    assert metadata["retrieval_backend"] == "numeric_literature"
    assert metadata["evidence_available"] is False


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

    with pytest.raises(ValueError, match="inherits attempt 0"):
        case_selector_module.select_evidence_cases(
            [],
            method_id="assembly.candidate.hifiasm",
            task=task,
            facts=_facts(read_type="pacbio_hifi"),
            context=_context(),
            strategy={"hifiasm_numeric": {}},
        )


@pytest.mark.parametrize(
    ("numeric_config", "message"),
    [
        ([], "hifiasm_numeric strategy must be a mapping"),
        (
            {"candidate_budget": 5},
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
        lambda values, pack_root: pytest.fail(
            "invalid config must fail before corpus loading"
        ),
    )

    with pytest.raises(ValueError, match=message):
        case_selector_module.select_evidence_cases(
            [],
            method_id="assembly.candidate.hifiasm",
            task="recommend_initial",
            facts=_facts(read_type="pacbio_hifi"),
            context=_context(),
            strategy={"hifiasm_numeric": numeric_config},
        )


def test_hifiasm_dispatch_rejects_unknown_task_before_backend_selection(monkeypatch):
    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda values, pack_root: pytest.fail(
            "invalid task must fail before retrieval"
        ),
    )

    with pytest.raises(ValueError, match="unsupported assembly parameter task"):
        case_selector_module.select_evidence_cases(
            [],
            method_id="assembly.candidate.hifiasm",
            task="not_a_parameter_task",
            facts=_facts(read_type="pacbio_hifi"),
            context=_context(),
            strategy={"hifiasm_numeric": {}},
        )


def test_query_adapter_records_units_and_canonical_source_paths():
    query = build_hifiasm_retrieval_query(
        _facts(
            genome_size_bp=1_200_000_000,
            coverage=31.5,
            read_n50_bp=18_700,
            heterozygosity=0.011,
            ploidy=2,
            clade="vertebrate",
            read_type="pacbio_hifi",
        ),
        _context(),
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
    )

    assert "query_id" not in query
    assert query["method_id"] == "assembly.candidate.hifiasm"
    assert query["read_type"] == "pacbio_hifi"
    assert "assembly_goal" not in query
    assert query["numeric_features"]["genome_size_mb"] == {
        "value": 1200.0,
        "source_ref": "facts.genome.effective_genome_size",
    }
    assert query["numeric_features"]["coverage"] == {
        "value": 31.5,
        "source_ref": "facts.sequencing.raw_coverage",
    }
    assert query["numeric_features"]["read_n50_kb"]["value"] == 18.7


def test_canonical_facts_reject_non_finite_values_before_query_adapter():
    with pytest.raises(ValidationError, match="finite"):
        _quantity(
            math.nan,
            "x",
            kind="derived",
            producer="read_qc",
        )
    with pytest.raises(ValidationError, match="finite"):
        _quantity(
            math.inf,
            "bp",
            kind="resolved",
            producer="genome_survey",
        )

    query = build_hifiasm_retrieval_query(
        _facts(),
        _context(),
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
    )
    assert query["numeric_features"]["coverage"]["value"] is None
    assert query["numeric_features"]["genome_size_mb"]["value"] is None


def test_query_adapter_uses_typed_ont_context_without_filtering():
    query = build_hifiasm_retrieval_query(
        _facts(read_type="ont_raw"),
        _context(read_type="ont_raw", assembly_route="ont"),
        method_id="assembly.candidate.hifiasm_ont",
        task="recommend_initial",
    )

    assert query["method_id"] == "assembly.candidate.hifiasm_ont"
    assert query["read_type"] == "ont_raw"


def test_no_approved_cases_returns_non_blocking_fallback_metadata():
    draft = _case("draft", coverage=30)
    draft["curation"]["status"] = "draft"

    result = HifiasmNumericCaseRetriever().select([draft], query=_query(coverage=30))

    assert result.selected_cases == []
    assert result.metadata["evidence_available"] is False
    assert "current sample profile" in result.metadata["warning"]


def test_approved_case_without_validated_sources_has_distinct_filter_reason():
    case = _case("unvalidated", coverage=30)
    case["retrieval_eligible"] = False

    result = HifiasmNumericCaseRetriever().select([case], query=_query(coverage=30))

    assert result.selected_cases == []
    assert result.trace[0]["eligibility_reasons"] == ["source_not_validated"]


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
    assert "few measured biological fields" in result.metadata["warning"]


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
