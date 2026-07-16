from __future__ import annotations

import pytest

from app.llm.parameters.assembly.response import (
    PARAMETER_RECOMMENDATION_CONTRACT,
    normalize_parameter_recommendation,
)


def _request(*, task: str = "recommend_initial") -> dict:
    return {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "method_id": "hifiasm",
        "task": task,
        "adjustable_parameters": {
            "purge_level": {"value_type": "int"},
        },
    }


def test_exact_parameter_mapping_is_the_only_model_contract() -> None:
    result = normalize_parameter_recommendation(
        {
            "parameters": {"purge_level": 1},
            "summary": "Use conservative purging.",
        },
        _request(),
    )

    assert result == {
        "parameters": {"purge_level": 1},
        "summary": "Use conservative purging.",
        "retry_disposition": None,
    }


def test_parser_does_not_judge_parameter_names_or_values() -> None:
    result = normalize_parameter_recommendation(
        {
            "parameters": {
                "model_invented_name": "definitely-not-a-catalog-value",
                "purge_level": 999,
            }
        },
        _request(),
    )

    assert result["parameters"] == {
        "model_invented_name": "definitely-not-a-catalog-value",
        "purge_level": 999,
    }


@pytest.mark.parametrize(
    "response",
    [
        {"parameter_patch": {"purge_level": 1}},
        {"params": {"purge_level": 1}},
        {"recommended_params": {"purge_level": 1}},
        {"purge_level": 1},
        {
            "parameters": {"purge_level": 1},
            "sample_diagnosis": {"summary": "legacy envelope"},
        },
    ],
)
def test_compatibility_response_shapes_are_rejected(response: dict) -> None:
    with pytest.raises(ValueError):
        normalize_parameter_recommendation(response, _request())


def test_rich_parameter_wrappers_are_rejected() -> None:
    with pytest.raises(ValueError, match="not wrapper objects"):
        normalize_parameter_recommendation(
            {
                "parameters": {
                    "purge_level": {
                        "value": 1,
                        "confidence": "medium",
                    }
                }
            },
            _request(),
        )


def test_retry_requires_the_model_to_state_its_disposition() -> None:
    with pytest.raises(ValueError, match="requires retry_disposition"):
        normalize_parameter_recommendation(
            {"parameters": {"purge_level": 1}},
            _request(task="retry_after_execution_failure"),
        )

    result = normalize_parameter_recommendation(
        {
            "parameters": {"purge_level": 1},
            "retry_disposition": "retry",
        },
        _request(task="retry_after_execution_failure"),
    )
    assert result["retry_disposition"] == "retry"


def test_summary_is_optional_but_never_synthesized() -> None:
    result = normalize_parameter_recommendation(
        {"parameters": {}},
        _request(),
    )

    assert result["summary"] is None
