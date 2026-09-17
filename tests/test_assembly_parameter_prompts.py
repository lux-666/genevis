from __future__ import annotations

import json
from hashlib import sha256

import pytest

from app.llm.parameters.assembly.prompt import (
    parameter_prompt_bundle_hash,
    parameter_prompt_bundle_name,
    render_assembly_parameter_messages,
)
from app.llm.parameters.assembly.response import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.llm.prompts import load_prompt_bundle


def _request_dto(method_id: str = "hifiasm") -> dict:
    return {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "method_id": method_id,
        "task": "recommend_initial",
        "facts": {"genome": {"heterozygosity": {"value": 0.01, "unit": "fraction"}}},
        "material_context": {},
        "method_decision": {"method_id": method_id},
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
        "knowledge_evidence": {
            "cases": [
                {
                    "case_id": "paper.1",
                    "source_method": {
                        "tool": "NextDenovo",
                        "settings": ["read_cutoff=10000"],
                        "setting_explanations": {"settings": {}, "scope": "unknown"},
                    },
                    "target_method": "hifiasm",
                    "relation": "cross_method",
                    "limitations": ["Reported parameter rationale."],
                }
            ],
        },
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
    assert rendered_request["knowledge_evidence"]["cases"][0]["case_id"] == "paper.1"


@pytest.mark.parametrize("method", ["flye", "nextdenovo", "hifiasm", "hifiasm_ont"])
def test_renderer_and_hash_use_the_same_method_bundle(method):
    full_id = "assembly.candidate." + method
    bundle = load_prompt_bundle(parameter_prompt_bundle_name(full_id))
    canonical = json.dumps(
        {
            "name": bundle.name,
            "system": bundle.system,
            "user_template": bundle.user_template,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert parameter_prompt_bundle_hash(full_id) == sha256(canonical).hexdigest()
    assert parameter_prompt_bundle_name(full_id) == parameter_prompt_bundle_name(method)
    assert (
        render_assembly_parameter_messages(_request_dto(full_id))[0]["content"]
        == bundle.system
    )


@pytest.mark.parametrize("method", ["unknown", "", None])
def test_renderer_and_hash_reject_the_same_unknown_methods(method):
    for call in (
        lambda: render_assembly_parameter_messages(_request_dto(method)),
        lambda: parameter_prompt_bundle_hash(method),
        lambda: parameter_prompt_bundle_name(method),
    ):
        with pytest.raises(ValueError, match="Unsupported assembly parameter prompt"):
            call()


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
    assert "cross-method cases" in messages[0]["content"]
    assert "never foreign-tool keys" in messages[0]["content"]


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
