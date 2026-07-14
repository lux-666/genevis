from __future__ import annotations

import pytest

from app.domains.assembly.parameter_recommendation import (
    PARAMETER_RECOMMENDATION_CONTRACT,
    default_parameter_recommendation,
    normalize_parameter_recommendation,
)


def _prompt(*, task: str = "recommend_initial") -> dict:
    payload = {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "task": task,
        "observations": [
            {
                "id": "pf_het",
                "section": "genome_survey",
                "name": "heterozygosity",
                "value": 0.018,
            }
        ],
        "method_guidance": [
            {
                "guidance_id": "hifiasm.purging.heterozygosity.v1",
                "parameters": ["purge_level"],
            }
        ],
        "literature_cases": [
            {
                "case_id": "paper.1",
                "source_excerpts": [
                    {"span_id": "quote.parameter", "quote": "Used -l1."}
                ],
            }
        ],
        "adjustable_params": {"purge_level": {"default": 2}},
        "baseline_params": {"purge_level": 2},
        "baseline_overrides": {},
    }
    if task != "recommend_initial":
        payload["baseline_overrides"] = {"purge_level": 1}
    return payload


def _response(**entry_overrides) -> dict:
    entry = {
        "value": 1,
        "observation_ids": ["pf_het"],
        "guidance_ids": ["hifiasm.purging.heterozygosity.v1"],
        "literature_anchors": [
            {"case_id": "paper.1", "quote_ids": ["quote.parameter"]}
        ],
        "expected_effect": "Reduce the risk of purging true haplotypes.",
        "tradeoffs": ["May retain more alternate sequence."],
        "confidence": "medium",
        "reason": "High heterozygosity motivates a conservative purge setting.",
        **entry_overrides,
    }
    return {
        "sample_diagnosis": {
            "effect_areas": ["purging"],
            "summary": "The sample is heterozygous.",
        },
        "parameter_patch": {"purge_level": entry},
        "summary": "Use a conservative purge setting.",
    }


def test_normalizes_sparse_patch_and_keeps_cards_by_reference() -> None:
    result = normalize_parameter_recommendation(_response(), _prompt())

    assert result["parameter_patch"] == {"purge_level": 1}
    assert result["params"] == {"purge_level": 1}
    assert result["effective_params"] == {"purge_level": 1}
    assert result["execution_params"] == {"purge_level": 1}
    assert result["used_literature_case_ids"] == ["paper.1"]
    assert "quote" not in result["parameter_reasoning"]["purge_level"]


def test_mechanistic_extrapolation_does_not_require_a_paper_case() -> None:
    response = _response(literature_anchors=[])

    result = normalize_parameter_recommendation(response, _prompt())

    assert result["used_literature_case_ids"] == []
    assert result["parameter_patch"] == {"purge_level": 1}


def test_retry_carries_previous_explicit_overrides_and_applies_sparse_patch() -> None:
    prompt = _prompt(task="retry_after_review_failure")
    prompt["adjustable_params"]["kmer_length"] = {"default": 51}
    prompt["baseline_params"] = {"purge_level": 1, "kmer_length": 51}
    prompt["baseline_overrides"] = {"purge_level": 1}
    response = _response(value=2)
    response["retry_disposition"] = "retry"

    result = normalize_parameter_recommendation(response, prompt)

    assert result["effective_params"] == {"purge_level": 2, "kmer_length": 51}
    assert result["execution_params"] == {"purge_level": 2}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"observation_ids": ["missing"]}, "observations absent"),
        ({"guidance_ids": ["missing"]}, "guidance absent"),
        (
            {"literature_anchors": [{"case_id": "paper.1", "quote_ids": ["missing"]}]},
            "quotes absent",
        ),
    ],
)
def test_rejects_invented_reference_ids(overrides, message) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_parameter_recommendation(_response(**overrides), _prompt())


def test_retry_stop_requires_empty_patch() -> None:
    response = _response()
    response["retry_disposition"] = "stop"

    with pytest.raises(ValueError, match="cannot include"):
        normalize_parameter_recommendation(
            response,
            _prompt(task="retry_after_execution_failure"),
        )


def test_model_unavailable_fallback_preserves_baseline_without_a_patch() -> None:
    result = default_parameter_recommendation(_prompt())

    assert result["parameter_patch"] == {}
    assert result["effective_params"] == {"purge_level": 2}
    assert result["execution_params"] == {}
