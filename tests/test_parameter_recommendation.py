from __future__ import annotations

import pytest

from app.domains.assembly.parameter_recommendation import (
    PARAMETER_RECOMMENDATION_CONTRACT,
    default_parameter_recommendation,
    normalize_parameter_recommendation,
)


def _prompt(*, task: str = "recommend_initial", contract: str | None = None) -> dict:
    payload = {
        "recommendation_contract": contract or PARAMETER_RECOMMENDATION_CONTRACT,
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
        "literature_cases": [{"case_id": "paper.1"}],
        "adjustable_params": {"purge_level": {"default": 2}},
        "baseline_params": {"purge_level": 2},
        "baseline_overrides": {},
    }
    if task != "recommend_initial":
        payload["baseline_overrides"] = {"purge_level": 1}
    return payload


def test_minimal_patch_is_the_canonical_model_contract() -> None:
    result = normalize_parameter_recommendation(
        {
            "parameter_patch": {"purge_level": 1},
            "summary": "Use conservative purging.",
        },
        _prompt(),
    )

    assert result["parameter_patch"] == {"purge_level": 1}
    assert result["params"] == {"purge_level": 1}
    assert result["effective_params"] == {"purge_level": 1}
    assert result["execution_params"] == {"purge_level": 1}
    assert result["parameter_reasoning"] == {"purge_level": {}}
    assert result["sample_diagnosis"]["summary"] == "Use conservative purging."


def test_old_rich_response_is_accepted_but_evidence_references_are_not_a_contract() -> None:
    result = normalize_parameter_recommendation(
        {
            "sample_diagnosis": {"effect_areas": ["purging"]},
            "parameter_patch": {
                "purge_level": {
                    "value": 1,
                    "observation_ids": ["pf_truncated_or_invented"],
                    "guidance_ids": ["invented.guidance"],
                    "literature_anchors": [{"case_id": "invented.case", "quote_ids": ["q"]}],
                    "expected_effect": "Preserve divergent sequence.",
                    "tradeoffs": "May retain alternate sequence.",
                    "confidence": "medium",
                    "reason": "Use a cautious first attempt.",
                }
            },
            "summary": "Use conservative purging.",
        },
        _prompt(contract="literature_patch_v1"),
    )

    assert result["parameter_patch"] == {"purge_level": 1}
    assert result["used_literature_case_ids"] == []
    assert result["parameter_reasoning"]["purge_level"] == {
        "expected_effect": "Preserve divergent sequence.",
        "tradeoffs": ["May retain alternate sequence."],
        "confidence": "medium",
        "reason": "Use a cautious first attempt.",
    }


def test_summary_and_sample_diagnosis_are_optional() -> None:
    result = normalize_parameter_recommendation(
        {"parameter_patch": {"purge_level": 1}},
        _prompt(),
    )

    assert result["summary"] == "LLM proposed 1 parameter change(s) for human review."
    assert result["sample_diagnosis"] == {
        "effect_areas": [],
        "summary": result["summary"],
    }


@pytest.mark.parametrize("field", ["params", "parameters", "recommended_params"])
def test_common_patch_labels_are_accepted(field: str) -> None:
    result = normalize_parameter_recommendation({field: {"purge_level": 1}}, _prompt())

    assert result["parameter_patch"] == {"purge_level": 1}


def test_direct_top_level_parameter_mapping_is_accepted() -> None:
    result = normalize_parameter_recommendation(
        {"purge_level": 1, "summary": "Use conservative purging."},
        _prompt(),
    )

    assert result["parameter_patch"] == {"purge_level": 1}


def test_retry_carries_previous_overrides_and_defaults_disposition() -> None:
    prompt = _prompt(task="retry_after_review_failure")
    prompt["adjustable_params"]["kmer_length"] = {"default": 51}
    prompt["baseline_params"] = {"purge_level": 1, "kmer_length": 51}

    result = normalize_parameter_recommendation(
        {"parameter_patch": {"purge_level": 2}},
        prompt,
    )

    assert result["effective_params"] == {"purge_level": 2, "kmer_length": 51}
    assert result["execution_params"] == {"purge_level": 2}
    assert result["retry_disposition"] == "retry"


def test_retry_without_patch_defaults_to_expert_review() -> None:
    result = normalize_parameter_recommendation(
        {"parameter_patch": {}},
        _prompt(task="retry_after_execution_failure"),
    )

    assert result["retry_disposition"] == "expert_review"


def test_non_adjustable_parameter_is_still_rejected() -> None:
    with pytest.raises(ValueError, match="non-L2 parameters"):
        normalize_parameter_recommendation(
            {"parameter_patch": {"threads": 128}},
            _prompt(),
        )


def test_rich_entry_without_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="containing value"):
        normalize_parameter_recommendation(
            {"parameter_patch": {"purge_level": {"reason": "missing value"}}},
            _prompt(),
        )


def test_model_unavailable_fallback_preserves_baseline_without_a_patch() -> None:
    result = default_parameter_recommendation(_prompt())

    assert result["parameter_patch"] == {}
    assert result["effective_params"] == {"purge_level": 2}
    assert result["execution_params"] == {}
