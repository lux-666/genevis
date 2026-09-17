"""One method-record locator, scoped before byte resolution."""

from copy import deepcopy

import pytest

from app.schemas.artifacts import (
    ASSEMBLY_METHOD_MODEL_REQUEST,
    ASSEMBLY_METHOD_MODEL_RESPONSE,
    ArtifactRef,
)
from app.stages.assembly.nodes import (
    _recommend_route_with_decision_boundary,
    _recover_assembly_method_artifact,
    _write_assembly_json_artifact,
)
from app.storage.artifacts import resolve_artifact_ref
from tests import test_phase8_slice1 as fixtures


def publish(state, artifact_type, payload, producer="assembly.method_recommender"):
    return ArtifactRef.model_validate(
        _write_assembly_json_artifact(
            state,
            "record.json",
            payload,
            artifact_type=artifact_type,
            method_name=producer,
        )
    )


@pytest.mark.parametrize(
    "artifact_type", [ASSEMBLY_METHOD_MODEL_REQUEST, ASSEMBLY_METHOD_MODEL_RESPONSE]
)
def test_recovery_filters_step_and_producer_before_resolving_files(
    tmp_path, artifact_type
):
    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    request = (
        publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, {"request": True})
        if artifact_type == ASSEMBLY_METHOD_MODEL_RESPONSE
        else None
    )
    assert (
        _recover_assembly_method_artifact(state, artifact_type, request_ref=request)
        is None
    )
    other = deepcopy(state)
    other["workflow_steps"].append(
        {
            "step_id": "step-003-assembly",
            "stage": "assembly",
            "allow_local_execution": True,
        }
    )
    other["workflow_step_index"] = 3
    other["entered_workflow_step_index"] = 3
    foreign_step = publish(other, artifact_type, {"other": "step"})
    foreign_producer = publish(
        state, artifact_type, {"other": "producer"}, "another.advisor"
    )
    for ref in (foreign_step, foreign_producer):
        resolve_artifact_ref(state["data_dir"], state["session_id"], ref).unlink()
    body = {"current": True}
    if request is not None:
        body["request_sha256"] = request.sha256
    ref = publish(state, artifact_type, body)
    assert _recover_assembly_method_artifact(
        state, artifact_type, request_ref=request
    ) == (ref, body)


@pytest.mark.parametrize(
    "artifact_type", [ASSEMBLY_METHOD_MODEL_REQUEST, ASSEMBLY_METHOD_MODEL_RESPONSE]
)
def test_recovery_rejects_duplicate_records_and_corrupt_current_bytes(
    tmp_path, artifact_type
):
    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    request = (
        publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, {"request": True})
        if artifact_type == ASSEMBLY_METHOD_MODEL_RESPONSE
        else None
    )
    binding = {"request_sha256": request.sha256} if request else {}
    first = publish(state, artifact_type, {"value": 1, **binding})
    publish(state, artifact_type, {"value": 2, **binding})
    with pytest.raises(ValueError, match="multiple"):
        _recover_assembly_method_artifact(state, artifact_type, request_ref=request)
    path = resolve_artifact_ref(state["data_dir"], state["session_id"], first)
    path.write_text("tampered")
    with pytest.raises(ValueError):
        _recover_assembly_method_artifact(state, artifact_type, request_ref=request)


def test_response_recovery_binds_request_hash(tmp_path):
    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    request = publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, {"request": True})
    with pytest.raises(ValueError, match="bound request"):
        _recover_assembly_method_artifact(state, ASSEMBLY_METHOD_MODEL_RESPONSE)
    with pytest.raises(ValueError, match="bound request"):
        _recover_assembly_method_artifact(
            state, ASSEMBLY_METHOD_MODEL_REQUEST, request_ref=request
        )
    publish(state, ASSEMBLY_METHOD_MODEL_RESPONSE, {"request_sha256": "wrong"})
    assert (
        _recover_assembly_method_artifact(
            state, ASSEMBLY_METHOD_MODEL_RESPONSE, request_ref=request
        )
        is None
    )
    body = {"request_sha256": request.sha256, "response": True}
    response = publish(state, ASSEMBLY_METHOD_MODEL_RESPONSE, body)
    assert _recover_assembly_method_artifact(
        state, ASSEMBLY_METHOD_MODEL_RESPONSE, request_ref=request
    ) == (response, body)


def test_recovery_rejects_non_object_json(tmp_path):
    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, [])
    with pytest.raises(ValueError, match="not an object"):
        _recover_assembly_method_artifact(state, ASSEMBLY_METHOD_MODEL_REQUEST)


def test_corrupt_request_blocks_new_retrieval_and_model_call(tmp_path, monkeypatch):
    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    ref = publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, {"request": True})
    path = resolve_artifact_ref(state["data_dir"], state["session_id"], ref)
    path.write_text("tampered")
    monkeypatch.setattr(
        "app.stages.assembly.nodes._route_selector_payload",
        lambda *args, **kwargs: pytest.fail("must not replace corrupt request"),
    )
    monkeypatch.setattr(
        "app.stages.assembly.nodes.make_assembly_decision",
        lambda *args, **kwargs: pytest.fail("must not call model after corruption"),
    )
    result = _recommend_route_with_decision_boundary(state, {}, {})
    assert result["source"] == "llm_unavailable"
    assert "recovery failed" in result["uncertainty_notes"][0]
