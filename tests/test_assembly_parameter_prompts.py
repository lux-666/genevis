from __future__ import annotations

import json

import pytest

from app.llm.parameters.assembly.prompt import render_assembly_parameter_messages
from app.llm.parameters.assembly.response import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)


def _request_dto(method_id: str = "hifiasm") -> dict:
    return {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "method_id": method_id,
        "task": "recommend_initial",
        "observations": [
            {
                "id": "pf_1",
                "section": "genome_survey",
                "name": "heterozygosity",
                "value": 0.01,
            }
        ],
        "method_guidance": [
            {
                "guidance_id": "g1",
                "methods": [method_id],
                "parameters": ["purge_level"],
                "relevant_metrics": ["genome_survey.heterozygosity"],
                "guidance": "Consider haplotypic divergence.",
                "caveats": [],
            }
        ],
        "adjustable_parameters": {
            "purge_level": {
                "value_type": "int",
                "allowed_values": [0, 1, 2, 3],
            }
        },
        "literature_selection": {
            "selected_case_ids": ["paper.1"],
            "selected_count": 1,
        },
        "literature_cases": [
            {
                "schema_version": 3,
                "case_id": "paper.1",
                "case_type": "initial",
                "source": {"kind": "paper", "doi": "10.example/one"},
                "applicability": {"read_type": "hifi"},
                "observed_facts": {},
                "intervention": {"tool": "hifiasm", "parameters": {}},
                "rationale_claims": [],
                "outcomes": {},
                "source_excerpts": [
                    {"span_id": "q1", "quote": "Reported parameter rationale."}
                ],
                "numeric_match": {"numeric_similarity": 0.8},
            }
        ],
    }


def test_renderer_serializes_the_direct_dto_without_a_second_projection() -> None:
    request = _request_dto()
    messages = render_assembly_parameter_messages(request)
    rendered_request = json.loads(messages[1]["content"])

    assert rendered_request == request
    assert messages[1]["content"] == json.dumps(
        request,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert rendered_request["literature_cases"][0]["case_id"] == "paper.1"


@pytest.mark.parametrize("method_id", ["flye", "nextdenovo", "hifiasm", "hifiasm_ont"])
def test_prompt_is_one_system_user_pair_without_response_example(
    method_id: str,
) -> None:
    messages = render_assembly_parameter_messages(_request_dto(method_id))

    assert [message["role"] for message in messages] == ["system", "user"]
    model_payload = json.loads(messages[1]["content"])
    assert model_payload["recommendation_contract"] == PARAMETER_RECOMMENDATION_CONTRACT
    assert "baseline_params" not in model_payload
    assert "rules" not in model_payload
    assert messages[1]["content"].count("Reported parameter rationale.") == 1
    assert "Representative" not in json.dumps(messages)
    assert "explicit L2 parameters" in messages[0]["content"]
    assert (
        "Omit an L2 parameter when the tool default should apply"
        in messages[0]["content"]
    )
    assert "Do not return observation IDs" in messages[0]["content"]


def test_retry_context_is_rendered_exactly_from_the_strict_dto() -> None:
    request = _request_dto()
    request["task"] = "retry_after_execution_failure"
    request["retry_context"] = {
        "task": request["task"],
        "previous_attempt": {
            "execution_status": "failed",
            "parameters": {"purge_level": 1},
        },
        "assembly_review": {},
        "signal_ids": ["execution.status"],
        "signals": [
            {
                "signal_id": "execution.status",
                "value": "failed",
            }
        ],
    }

    messages = render_assembly_parameter_messages(request)
    rendered_request = json.loads(messages[1]["content"])

    assert rendered_request == request
