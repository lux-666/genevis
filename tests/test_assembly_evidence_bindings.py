"""Reject internally unbound requests even when artifact hashes are all valid."""

import json
from copy import deepcopy

import pytest

from app.control.errors import ResumeValidationError
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
    load_approved_parameter_chain_v3,
    load_parameter_recommendation_chain_v3,
)
from tests import test_parameter_persistence_v2 as persistence
from tests import test_phase10_parameter_catalog as fixtures

FAULTS = (
    "missing_input",
    "missing_basis",
    "wrong_input",
    "wrong_basis",
    "empty_messages",
    "wrong_roles",
    "invalid_json",
    "wrong_method",
    "wrong_facts",
    "wrong_context",
    "old_basis",
)


def corrupt(filename, payload, fault):
    payload = deepcopy(payload)
    if fault == "old_basis" and filename == "basis_snapshot.json":
        payload["consumer_version"] = 4
    if filename != "model_request.json":
        return payload
    key = "input_snapshot_sha256" if fault.endswith("input") else "basis_sha256"
    if fault.startswith("missing_"):
        payload.pop(key)
    elif fault.startswith("wrong_") and fault in ("wrong_input", "wrong_basis"):
        payload[key] = "0" * 64
    elif fault == "empty_messages":
        payload["messages"] = []
    elif fault == "wrong_roles":
        payload["messages"][1]["role"] = "assistant"
    elif fault == "invalid_json":
        payload["messages"][1]["content"] = "invalid JSON"
    elif fault in ("wrong_method", "wrong_facts", "wrong_context"):
        dto = json.loads(payload["messages"][1]["content"])
        if fault == "wrong_method":
            dto["method_id"] = "assembly.candidate.flye"
        elif fault == "wrong_facts":
            dto["facts"]["species"]["scientific_name"] = "Unbound species"
        else:
            dto["assembly_context"]["target_level"] = "near_t2t"
        payload["messages"][1]["content"] = json.dumps(dto)
    return payload


@pytest.mark.parametrize("fault", FAULTS)
def test_shared_chain_and_execution_reject_unbound_evidence(
    tmp_path, monkeypatch, fault
):
    state = fixtures.ParameterRecommendationNodeTests()._state(str(tmp_path))
    write = fixtures.write_recommendation_json_artifact

    def publish(**kwargs):
        kwargs["payload"] = corrupt(kwargs["filename"], kwargs["payload"], fault)
        return write(**kwargs)

    monkeypatch.setattr(fixtures, "write_recommendation_json_artifact", publish)
    attempt = fixtures._persist_parameter_attempt(
        state,
        method_id="assembly.candidate.hifiasm_ont",
    )
    common = dict(expected_stage="assembly", expected_method_id=attempt["method_id"])
    with pytest.raises(ParameterEvidenceError):
        load_parameter_recommendation_chain_v3(
            state,
            manifest_ref=attempt["recommendation_manifest_ref"],
            **common,
        )
    with pytest.raises(ParameterEvidenceError):
        load_approved_parameter_chain_v3(state, attempt=attempt, **common)


@pytest.mark.parametrize("fault", FAULTS[:-1])
def test_recovery_and_approval_reject_the_same_request(tmp_path, monkeypatch, fault):
    from app.stages.assembly.parameters import workflow

    state = persistence._state(tmp_path)
    write = workflow.write_artifact

    def publish(state, identifier, filename, payload, **kwargs):
        return write(
            state, identifier, filename, corrupt(filename, payload, fault), **kwargs
        )

    monkeypatch.setattr(workflow, "write_artifact", publish)
    monkeypatch.setattr(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        lambda: persistence._Model(persistence._response()),
    )
    first = workflow.recommend_assembly_parameters(state)
    blocked = persistence._blocked_state(state, first)
    with pytest.raises(ResumeValidationError, match="manifest is invalid"):
        persistence._apply_review_application(
            blocked,
            persistence._decision_request(blocked, "approve_parameters"),
        )
    monkeypatch.setattr(
        workflow,
        "invoke_assembly_parameter_model",
        lambda *a, **k: pytest.fail("unbound request invoked model"),
    )
    replay = workflow.recommend_assembly_parameters(state)
    assert replay["stage_outputs"]["assembly"]["status"] == "failed"
