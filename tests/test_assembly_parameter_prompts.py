import json

import pytest

from app.domains.assembly.methods_evidence import normalize_hifiasm_methods_response
from app.llm.assembly_parameter_context import build_assembly_parameter_context
from app.llm.assembly_parameter_prompts import (
    parameter_prompt_bundle_name,
    render_assembly_parameter_messages,
)
from app.stages.assembly.decision import AssemblyDecisionRequest, make_assembly_decision
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
        "observations": [
            {
                "id": "pf_read_n50",
                "section": "read_qc",
                "group": "summary",
                "name": "n50",
                "value": 30_000,
            }
        ],
        "evidence_registry": {"pf_read_n50": "read_qc.summary.n50"},
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
    model_context = build_assembly_parameter_context(payload)

    assert parameter_prompt_bundle_name(method_id) == f"assembly.parameter_{method_id}"
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert json.loads(messages[-1]["content"]) == model_context
    assert "prompt_profile" not in model_context
    assert "evidence_registry" not in model_context
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
    assert all(
        "fact_paths" not in item and "evidence_ids" in item
        for item in example["parameter_evidence"].values()
    )


def test_legacy_example_cites_an_observation_id_for_a_nondefault_choice():
    payload = _payload("flye")
    spec = payload["adjustable_params"]["min_overlap"]
    spec["default"] = None
    spec["min"] = 1_000

    messages = render_assembly_parameter_messages(payload, prompt_contract="legacy")
    example = json.loads(messages[2]["content"])

    evidence = example["parameter_evidence"]["min_overlap"]
    assert evidence["bases"] == ["profile_fact"]
    assert evidence["evidence_ids"] == ["pf_read_n50"]


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


def test_hifiasm_retry_example_recovers_previous_values_from_observations():
    payload = _payload("hifiasm", task="retry_after_execution_failure")
    payload["observations"].append(
        {
            "id": "pf_previous_purge_level",
            "section": "previous_attempt",
            "group": "params",
            "name": "purge_level",
            "value": 2,
        }
    )

    messages = render_assembly_parameter_messages(
        payload,
        prompt_contract="hifiasm_methods_v1",
    )
    example = json.loads(messages[2]["content"])
    decision = example["recommendation"]["adjustable_params"]["purge_level"]

    assert decision["action"] == "keep_previous"
    assert decision["value"] == 2


def test_full_method_id_selects_the_same_prompt_bundle():
    assert (
        parameter_prompt_bundle_name("assembly.candidate.hifiasm_ont")
        == "assembly.parameter_hifiasm_ont"
    )


def test_model_context_is_allowlisted_and_recursively_removes_audit_metadata():
    payload = _payload("hifiasm")
    payload.update(
        {
            "input_profile_ref": "/audit/input_profile.json",
            "input_profile_hash": "deadbeef",
            "prompt_payload_ref": "/audit/prompt_payload.json",
            "prompt_payload_hash": "cafebabe",
            "profile_facts": {"read_qc.summary.n50": 30_000},
            "session_id": "secret-session",
            "attempt_id": 3,
            "case_selection": {
                "query_id": "secret-session:parameter_attempt_3",
                "retrieval_backend": "hifiasm_numeric",
                "evidence_case_ids": ["case-1"],
                "evidence_available": True,
                "warning": None,
            },
            "evidence_cases": [
                {
                    "schema_version": 3,
                    "case_id": "case-1",
                    "numeric_match": {
                        "rank": 1,
                        "numeric_distance": 0.1,
                        "feature_distances": {"coverage": {"distance": 0.1}},
                    },
                    "source": {
                        "title": "Methods study",
                        "artifact_refs": ["/audit/source.pdf"],
                        "internal_run_id": "run-secret",
                    },
                    "applicability": {"read_type": "hifi"},
                    "observed_facts": {"coverage": {"value": 30}},
                    "intervention": {"parameters": {"purge_level": {"value": 1}}},
                    "rationale_claims": [{"statement": "Supported by the Methods text."}],
                    "source_excerpts": [
                        {
                            "span_id": "span-1",
                            "document": "/audit/source.md",
                            "quote": "A bounded Methods excerpt.",
                        }
                    ],
                }
            ],
            "methods_evidence": {
                "effect_areas": ["purging"],
                "response_contract": "case_assessments_and_recommendation",
                "assessment_rule": "cite visible spans",
                "recommendation_rule": "cover the L2 surface",
                "evidence_budget": {"case_usage": [{"case_id": "case-1"}]},
            },
        }
    )
    payload["adjustable_params"]["purge_level"].update(
        {
            "cli": "-l",
            "level": 2,
            "source": "llm_recommended",
            "source_priority": ["assembly.parameters.purge_level"],
            "fixed_value": None,
        }
    )

    context = build_assembly_parameter_context(payload)

    assert set(context) <= {
        "method_id",
        "task",
        "observations",
        "rules",
        "recommendation_policy",
        "skill_instructions",
        "readonly_params",
        "adjustable_params",
        "selected_cases",
        "case_selection",
        "evidence_cases",
        "methods_evidence",
        "retry_evidence",
    }
    assert context["case_selection"] == {"evidence_available": True}
    case = context["evidence_cases"][0]
    assert "numeric_match" not in case
    assert case["source"] == {"title": "Methods study"}
    assert case["source_excerpts"] == [
        {"span_id": "span-1", "quote": "A bounded Methods excerpt."}
    ]
    assert "evidence_budget" not in context["methods_evidence"]
    parameter_spec = context["adjustable_params"]["purge_level"]
    assert set(parameter_spec) <= {
        "value_type",
        "allowed_values",
        "min",
        "max",
        "default",
        "description",
        "notes",
    }
    serialized = json.dumps(context)
    assert "/audit/" not in serialized
    assert "secret-session" not in serialized
    assert "prompt_profile" not in serialized
    assert "profile_facts" not in serialized
    assert "evidence_registry" not in serialized


def test_model_context_fails_closed_without_observations():
    payload = _payload("flye")
    del payload["observations"]

    with pytest.raises(ValueError, match="requires a model-facing observations list"):
        build_assembly_parameter_context(payload)


def test_custom_system_prompt_uses_the_same_model_context_boundary(monkeypatch):
    payload = _payload("flye")
    payload["input_profile_ref"] = "/audit/input_profile.json"
    payload["prompt_payload_hash"] = "deadbeef"
    example = json.loads(
        render_assembly_parameter_messages(payload, prompt_contract="legacy")[2]["content"]
    )

    class _Response:
        content = json.dumps(example)

    class _Model:
        messages = None

        def invoke(self, messages):
            self.messages = messages
            return _Response()

    model = _Model()
    monkeypatch.setattr("app.stages.assembly.decision.build_chat_model", lambda: model)

    result = make_assembly_decision(
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="internal-session",
            prompt_contract="legacy",
            prompt_payload=payload,
            system_prompt="Recommend Flye parameters.",
        )
    )

    assert result.valid is True
    user_payload = json.loads(model.messages[1]["content"])
    assert user_payload == build_assembly_parameter_context(payload)
    assert "prompt_profile" not in user_payload
    assert "evidence_registry" not in user_payload
    assert all(not key.endswith(("_ref", "_hash")) for key in user_payload)
