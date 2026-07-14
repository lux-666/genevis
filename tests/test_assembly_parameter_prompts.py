import json

import pytest

from app.domains.assembly.methods_evidence import normalize_hifiasm_methods_response
from app.llm.assembly_parameter_prompts import (
    parameter_prompt_bundle_name,
    render_assembly_parameter_messages,
)
from app.stages.assembly.parameter_catalog import load_parameter_catalog


METHODS = ("flye", "nextdenovo", "hifiasm", "hifiasm_ont")


def _payload(method_id: str, *, task: str = "recommend_initial") -> dict:
    catalog = load_parameter_catalog(method_id)
    adjustable = {
        name: spec.model_dump(mode="json")
        for name, spec in catalog.parameters.items()
        if spec.level == 2
    }
    return {
        "method_id": method_id,
        "task": task,
        "prompt_profile": {
            "read_qc": {"summary": {"n50": 30_000, "effective_coverage_5kb": 42}},
            "genome_survey": {"estimates": {"genome_size": 1_000_000_000}},
        },
        "adjustable_params": adjustable,
        "evidence_cases": [],
        "recommendation_policy": {
            "allowed_evidence_bases": [
                "case",
                "profile_fact",
                "retry_failure",
                "catalog_default",
            ]
        },
    }


@pytest.mark.parametrize("method_id", METHODS)
def test_each_method_has_a_prompt_bundle_and_positive_example(method_id):
    contract = "hifiasm_methods_v1" if method_id == "hifiasm" else "legacy"
    payload = _payload(method_id)

    messages = render_assembly_parameter_messages(payload, prompt_contract=contract)
    example = json.loads(messages[2]["content"])

    assert parameter_prompt_bundle_name(method_id) == f"assembly.parameter_{method_id}"
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert json.loads(messages[-1]["content"]) == payload
    assert "never use a list" not in messages[0]["content"].lower()
    assert "禁止" not in messages[0]["content"]
    assert isinstance(example, dict)


@pytest.mark.parametrize("method_id", ("flye", "nextdenovo", "hifiasm_ont"))
def test_legacy_method_examples_use_object_shaped_parameter_evidence(method_id):
    payload = _payload(method_id)
    messages = render_assembly_parameter_messages(payload, prompt_contract="legacy")
    example = json.loads(messages[2]["content"])

    assert set(example["params"]) <= set(payload["adjustable_params"])
    assert example["parameter_evidence"]
    assert set(example["parameter_evidence"]) == set(example["params"])
    assert all(isinstance(item, dict) for item in example["parameter_evidence"].values())


def test_hifiasm_methods_example_tracks_the_complete_current_l2_surface():
    payload = _payload("hifiasm")
    payload["case_selection"] = {
        "retrieval_backend": "hifiasm_numeric",
        "evidence_case_ids": [],
    }
    messages = render_assembly_parameter_messages(
        payload,
        prompt_contract="hifiasm_methods_v1",
    )
    example = json.loads(messages[2]["content"])
    decisions = example["recommendation"]["adjustable_params"]

    assert set(decisions) == set(payload["adjustable_params"])
    for parameter, spec in payload["adjustable_params"].items():
        expected = "keep_default" if spec.get("default") is not None else "omit"
        assert decisions[parameter]["action"] == expected

    normalized = normalize_hifiasm_methods_response(example, payload)
    assert normalized["params"] == {}
    assert set(normalized["parameter_decisions"]) == set(payload["adjustable_params"])


def test_full_method_id_selects_the_same_prompt_bundle():
    assert (
        parameter_prompt_bundle_name("assembly.candidate.hifiasm_ont")
        == "assembly.parameter_hifiasm_ont"
    )
