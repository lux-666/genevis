"""Tests for assembly evidence-case selection."""

from __future__ import annotations

import pytest

from app.domains.assembly.case_selector import (
    EvidenceBasedCaseSelector,
    evidence_read_type_matches,
    select_evidence_cases,
)


def test_case_selector_scores_read_type_without_filtering_mismatches():
    cases = [
        {
            "case_id": "hifi_case",
            "features": {"read_type": "hifi", "coverage": 60},
            "params": {"tool": "hifiasm"},
        },
        {
            "case_id": "ont_case",
            "features": {"read_type": "ont", "coverage": 60},
            "params": {"tool": "hifiasm_ont"},
        },
    ]
    profile = {"assets": {"read_type": "hifi"}}

    result = EvidenceBasedCaseSelector().select(
        cases,
        method_id="hifiasm",
        task="recommend_initial",
        prompt_profile=profile,
    )
    selected, metadata = result.selected_cases, result.metadata

    assert [case["case_id"] for case in selected] == ["hifi_case", "ont_case"]
    assert metadata["selected_case_ids"] == ["hifi_case", "ont_case"]
    trace = {entry["case_id"]: entry for entry in metadata["trace"]}
    assert trace["hifi_case"]["score"] > trace["ont_case"]["score"]
    assert trace["ont_case"]["excluded_reasons"] == []


def test_case_selector_keeps_cyclone_route_from_becoming_read_type():
    cases = [
        {
            "case_id": "hifi_case",
            "features": {"read_type": "hifi"},
            "params": {"tool": "hifiasm"},
        },
        {
            "case_id": "ont_case",
            "features": {"read_type": "ont"},
            "params": {"tool": "hifiasm_ont"},
        },
    ]
    profile = {"assets": {"assembly_route": "cyclone", "read_type": "ont"}}

    selected, metadata = select_evidence_cases(
        cases,
        method_id="flye",
        task="recommend_initial",
        prompt_profile=profile,
    )

    assert [case["case_id"] for case in selected] == ["ont_case"]
    trace = {entry["case_id"]: entry for entry in metadata["trace"]}
    assert trace["hifi_case"]["decision"] == "not_selected"
    assert trace["hifi_case"]["excluded_reasons"] == []


def test_case_selector_matches_fine_grained_ont_cases_to_normalized_profile_read_type():
    cases = [
        {
            "case_id": "ont_case",
            "features": {"read_type": "ont"},
            "params": {"tool": "flye"},
        },
        {
            "case_id": "hifi_case",
            "features": {"read_type": "pacbio_hifi"},
            "params": {"tool": "hifiasm"},
        },
    ]
    profile = {"assets": {"read_type": "ont"}}

    selected, metadata = select_evidence_cases(
        cases,
        method_id="flye",
        task="recommend_initial",
        prompt_profile=profile,
    )

    assert [case["case_id"] for case in selected] == ["ont_case"]
    selected_trace = [entry for entry in metadata["trace"] if entry["decision"] == "selected"]
    assert "read_type_match" in selected_trace[0]["matched_features"]
    trace = {entry["case_id"]: entry for entry in metadata["trace"]}
    assert trace["hifi_case"]["decision"] == "not_selected"
    assert trace["hifi_case"]["excluded_reasons"] == []


def test_case_selector_matches_descriptor_method_id_to_short_tool_name():
    cases = [
        {
            "case_id": "hifi_case",
            "features": {"read_type": "hifi"},
            "params": {"tool": "hifiasm"},
        },
        {
            "case_id": "flye_case",
            "features": {"read_type": "hifi"},
            "params": {"tool": "flye"},
        },
    ]
    profile = {"assets": {"read_type": "hifi"}}

    result = EvidenceBasedCaseSelector(max_cases=1).select(
        cases,
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        prompt_profile=profile,
    )
    selected, metadata = result.selected_cases, result.metadata

    assert [case["case_id"] for case in selected] == ["hifi_case"]
    selected_trace = [entry for entry in metadata["trace"] if entry["decision"] == "selected"]
    assert selected_trace[0]["score"] == 16
    assert "method_exact_match" in selected_trace[0]["matched_features"]


def test_case_selector_uses_strategy_weight_overrides():
    cases = [
        {
            "case_id": "same_family",
            "features": {"read_type": "ont"},
            "params": {"tool": "hifiasm_ont"},
        },
        {
            "case_id": "read_type_only",
            "features": {"read_type": "ont"},
            "params": {"tool": "flye"},
        },
    ]
    profile = {"assets": {"read_type": "ont"}}
    strategy = {
        "scoring_weights": {
            "method_family_match": 20,
            "read_type_match": 1,
        }
    }

    selector = EvidenceBasedCaseSelector(max_cases=1, strategy=strategy)
    result = selector.select(
        cases,
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        prompt_profile=profile,
    )

    assert [case["case_id"] for case in result.selected_cases] == ["same_family"]
    selected_trace = [entry for entry in result.trace if entry["decision"] == "selected"]
    assert selected_trace[0]["score"] == 21
    assert "method_family_match" in selected_trace[0]["matched_features"]


def test_invalid_default_strategy_raises_error():
    """P2 fix: invalid default_strategy should raise ValueError instead of silent fallback"""
    cases = [
        {
            "case_id": "case1",
            "features": {"read_type": "hifi"},
            "params": {"tool": "hifiasm"},
        },
    ]
    profile = {"assets": {"read_type": "hifi"}}

    # Strategy with invalid default_strategy name
    strategy = {
        "default_strategy": "nonexistent_strategy",
        "strategies": {
            "valid_strategy": {
                "scoring_weights": {"method_exact_match": 10}
            }
        }
    }

    try:
        selector = EvidenceBasedCaseSelector(strategy=strategy)
        assert False, "Expected ValueError for invalid default_strategy"
    except ValueError as e:
        assert "default_strategy 'nonexistent_strategy' not found" in str(e)
        assert "Available strategies: ['valid_strategy']" in str(e)


def test_valid_default_strategy_works():
    """P2: valid default_strategy should be loaded correctly"""
    cases = [
        {
            "case_id": "case1",
            "features": {"read_type": "hifi"},
            "params": {"tool": "hifiasm"},
        },
    ]
    profile = {"assets": {"read_type": "hifi"}}

    strategy = {
        "default_strategy": "custom_strategy",
        "strategies": {
            "custom_strategy": {
                "hard_filters": {
                    "exclude_failure_cases_for_initial": True,
                },
                "scoring_weights": {
                    "method_exact_match": 100,
                    "read_type_match": 50,
                }
            }
        }
    }

    selector = EvidenceBasedCaseSelector(strategy=strategy)
    result = selector.select(
        cases,
        method_id="hifiasm",
        task="recommend_initial",
        prompt_profile=profile,
    )

    assert len(result.selected_cases) == 1
    selected_trace = [entry for entry in result.trace if entry["decision"] == "selected"]
    # Should use custom weights: 100 (method) + 50 (read_type) = 150
    assert selected_trace[0]["score"] == 150


def test_named_strategy_missing_required_sections_is_rejected():
    strategy = {
        "default_strategy": "custom_strategy",
        "strategies": {
            "custom_strategy": {
                "typo_field": 123,
            }
        },
    }

    with pytest.raises(ValueError, match="missing required field 'hard_filters'"):
        EvidenceBasedCaseSelector(strategy=strategy)


def test_named_strategy_required_sections_must_have_expected_types():
    strategy = {
        "default_strategy": "custom_strategy",
        "strategies": {
            "custom_strategy": {
                "hard_filters": [],
                "scoring_weights": {"method_exact_match": 100},
            }
        },
    }

    with pytest.raises(ValueError, match="hard_filters must be a mapping"):
        EvidenceBasedCaseSelector(strategy=strategy)


def test_read_type_matching_rejects_unknown_and_none_values():
    assert evidence_read_type_matches(None, None) is False
    assert evidence_read_type_matches("unknown", "unknown") is False
    assert evidence_read_type_matches("not-a-read-type", "not-a-read-type") is False


def test_unknown_or_missing_profile_read_type_leaves_method_score_available():
    cases = [
        {
            "case_id": "missing_read_type",
            "features": {},
            "params": {"tool": "flye"},
        },
        {
            "case_id": "unknown_read_type",
            "features": {"read_type": "unknown"},
            "params": {"tool": "flye"},
        },
        {
            "case_id": "ont_case",
            "features": {"read_type": "ont"},
            "params": {"tool": "flye"},
        },
    ]
    profile = {"assets": {"read_type": None}}

    selected, metadata = select_evidence_cases(
        cases,
        method_id="flye",
        task="recommend_initial",
        prompt_profile=profile,
    )

    assert [case["case_id"] for case in selected] == [
        "missing_read_type",
        "ont_case",
        "unknown_read_type",
    ]
    assert all(not entry["excluded_reasons"] for entry in metadata["trace"])
    assert all("read_type_match" not in entry["matched_features"] for entry in metadata["trace"])


def test_hifiasm_dispatch_defaults_to_legacy_until_numeric_cutover():
    cases = [
        {
            "case_id": "legacy_hifiasm_seed",
            "features": {"read_type": "hifi", "coverage": 30},
            "params": {"tool": "hifiasm"},
        }
    ]

    selected, metadata = select_evidence_cases(
        cases,
        method_id="hifiasm",
        task="recommend_initial",
        prompt_profile={"assets": {"read_type": "hifi"}},
    )

    assert [case["case_id"] for case in selected] == ["legacy_hifiasm_seed"]
    assert metadata["retrieval_backend"] == "structured_default"
    assert metadata["hifiasm_numeric_gate"] == {
        "initial_cutover_enabled": False,
        "decision": "structured_default",
        "reason": "initial_cutover_disabled",
    }


@pytest.mark.parametrize(
    "method_id",
    ["hifiasm_ont", "hifiasm-ont", "assembly.candidate.hifiasm_ont"],
)
def test_hifiasm_ont_aliases_enter_the_evidence_case_backend(monkeypatch, method_id):
    from types import SimpleNamespace

    from app.domains.assembly import case_selector as case_selector_module

    captured = {}

    class FakeRetriever:
        def select(self, cases, *, query):
            captured["cases"] = cases
            captured["query"] = query
            return SimpleNamespace(
                selected_cases=[],
                metadata={"retrieval_backend": "hifiasm_numeric", "trace": []},
            )

    monkeypatch.setattr(
        case_selector_module,
        "validate_hifiasm_evidence_cases",
        lambda cases, pack_root: cases,
    )
    monkeypatch.setattr(
        case_selector_module.HifiasmNumericCaseRetriever,
        "from_config",
        lambda config, max_cases: FakeRetriever(),
    )

    selected, metadata = select_evidence_cases(
        [],
        method_id=method_id,
        task="recommend_initial",
        prompt_profile={"assets": {"read_type": "ont"}},
        strategy={"hifiasm_numeric": {"initial_cutover_enabled": True}},
    )

    assert selected == []
    assert captured["query"]["method_id"] == method_id
    assert captured["query"]["read_type"] == "ont"
    assert metadata["retrieval_backend"] == "hifiasm_numeric"
