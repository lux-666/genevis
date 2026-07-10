"""Tests for assembly evidence-case selection."""

from __future__ import annotations

import pytest

from app.domains.assembly.case_selector import (
    EvidenceBasedCaseSelector,
    evidence_read_type_matches,
    select_evidence_cases,
)


def test_case_selector_filters_read_type_and_records_trace():
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

    assert [case["case_id"] for case in selected] == ["hifi_case"]
    assert metadata["selected_case_ids"] == ["hifi_case"]
    excluded = [entry for entry in metadata["trace"] if entry["decision"] == "excluded"]
    assert excluded[0]["case_id"] == "ont_case"
    assert excluded[0]["excluded_reasons"] == ["read_type_mismatch"]


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
    excluded = {entry["case_id"]: entry["excluded_reasons"] for entry in metadata["trace"] if entry["decision"] == "excluded"}
    assert excluded == {"hifi_case": ["read_type_mismatch"]}


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
    excluded = {entry["case_id"]: entry["excluded_reasons"] for entry in metadata["trace"] if entry["decision"] == "excluded"}
    assert excluded == {"hifi_case": ["read_type_mismatch"]}


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
                    "read_type_mismatch": True,
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


def test_unknown_or_missing_profile_read_type_does_not_leak_cases_by_method_score():
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

    assert selected == []
    assert {entry["case_id"]: entry["excluded_reasons"] for entry in metadata["trace"]} == {
        "missing_read_type": ["read_type_mismatch"],
        "unknown_read_type": ["read_type_mismatch"],
        "ont_case": ["read_type_mismatch"],
    }


def test_hifiasm_v2_dispatch_does_not_fall_back_to_v1_seed_cases():
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

    assert selected == []
    assert metadata["retrieval_backend"] == "hifiasm_numeric_v2"
    assert metadata["evidence_available"] is False
    assert metadata["selected_case_ids"] == []
