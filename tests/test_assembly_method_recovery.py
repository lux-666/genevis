"""One method-record locator, scoped before byte resolution."""

import json
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
def test_publication_rejects_replacement_and_recovery_rejects_corruption(
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
    with pytest.raises(FileExistsError):
        publish(state, artifact_type, {"value": 2, **binding})
    path = resolve_artifact_ref(state["data_dir"], state["session_id"], first)
    path.write_text("tampered")
    with pytest.raises(FileExistsError):
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
    response = publish(
        state, ASSEMBLY_METHOD_MODEL_RESPONSE, {"request_sha256": "wrong"}
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        _recover_assembly_method_artifact(
            state, ASSEMBLY_METHOD_MODEL_RESPONSE, request_ref=request
        )
    assert response.artifact_type == ASSEMBLY_METHOD_MODEL_RESPONSE


@pytest.mark.parametrize(
    "artifact_type", [ASSEMBLY_METHOD_MODEL_REQUEST, ASSEMBLY_METHOD_MODEL_RESPONSE]
)
def test_recovery_repairs_publish_before_index_crash(
    tmp_path, monkeypatch, artifact_type
):
    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    request = (
        publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, {"request": True})
        if artifact_type == ASSEMBLY_METHOD_MODEL_RESPONSE
        else None
    )
    body = {"request_sha256": request.sha256} if request else {"request": True}
    with monkeypatch.context() as crash:

        def fail(*args, **kwargs):
            raise OSError("crash before indexing")

        crash.setattr("app.storage.artifacts.register_artifact_records", fail)
        with pytest.raises(OSError, match="before indexing"):
            publish(state, artifact_type, body)
    recovered = _recover_assembly_method_artifact(
        state, artifact_type, request_ref=request
    )
    assert recovered is not None and recovered[1] == body
    assert (
        _recover_assembly_method_artifact(state, artifact_type, request_ref=request)
        == recovered
    )
    resolve_artifact_ref(state["data_dir"], state["session_id"], recovered[0]).unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        _recover_assembly_method_artifact(state, artifact_type, request_ref=request)


def test_recovery_rejects_non_object_json(tmp_path):
    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, [])
    with pytest.raises(ValueError, match="not an object"):
        _recover_assembly_method_artifact(state, ASSEMBLY_METHOD_MODEL_REQUEST)


def test_old_method_record_blocks_redispatch(tmp_path, monkeypatch):
    from app.stages.assembly.nodes import _METHOD_RECORD_FILENAMES

    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    with monkeypatch.context() as old:
        old.setitem(
            _METHOD_RECORD_FILENAMES,
            ASSEMBLY_METHOD_MODEL_REQUEST,
            "method_model_request.legacyhash.json",
        )
        publish(state, ASSEMBLY_METHOD_MODEL_REQUEST, {"old": True})
    with pytest.raises(ValueError, match="start a new run"):
        _recover_assembly_method_artifact(state, ASSEMBLY_METHOD_MODEL_REQUEST)


@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize("mismatch", [False, True])
def test_response_replay_uses_frozen_contract_and_published_winner(
    tmp_path, monkeypatch, race, mismatch
):
    from app.llm.json_response import model_response_record
    from app.stages.assembly.decision import AssemblyDecisionResult

    state = fixtures.InputProfilerTests()._state(str(tmp_path))
    prompt = {
        "allowed_methods": [],
        "allowed_actions": ["ont"] if mismatch else ["insufficient_data"],
    }
    request = publish(
        state,
        ASSEMBLY_METHOD_MODEL_REQUEST,
        {
            "kind": "route_selection",
            "session_id": state["session_id"],
            "allowed_actions": ["insufficient_data"],
            "allowed_methods": [],
            "prompt_payload": prompt,
            "messages": [
                {"role": "system", "content": "old system"},
                {"role": "user", "content": json.dumps(prompt)},
            ],
        },
    )
    body = {
        "request_sha256": request.sha256,
        **model_response_record(
            schema_version=1,
            raw_response=(
                '{"recommended_route":"insufficient_data", "reasoning":"winner"}'
            ),
            valid=True,
            error=None,
            model_id=None,
        ),
    }
    if race:

        def competitor(request, *, messages):
            assert request.allowed_actions == ["insufficient_data"]
            assert request.system_prompt == "old system"
            publish(state, ASSEMBLY_METHOD_MODEL_RESPONSE, body)
            return AssemblyDecisionResult(
                kind="route_selection", raw_response="{}", error="loser"
            )

        monkeypatch.setattr(
            "app.stages.assembly.nodes.make_assembly_decision", competitor
        )
    else:
        with monkeypatch.context() as crash:

            def fail(*args, **kwargs):
                raise OSError("crash before indexing")

            crash.setattr("app.storage.artifacts.register_artifact_records", fail)
            with pytest.raises(OSError):
                publish(state, ASSEMBLY_METHOD_MODEL_RESPONSE, body)
        monkeypatch.setattr(
            "app.stages.assembly.nodes.make_assembly_decision",
            lambda *args, **kwargs: pytest.fail("must reuse published model response"),
        )
    monkeypatch.setattr(
        "app.stages.assembly.nodes._route_selector_payload",
        lambda *args, **kwargs: pytest.fail("must reuse published model request"),
    )
    result = _recommend_route_with_decision_boundary(
        state, {}, {"allowed_actions": ["ont"], "skill_text": "new skill"}
    )
    if mismatch:
        assert result["source"] == "llm_unavailable"
        assert "binding mismatch" in result["uncertainty_notes"][0]
        return
    assert result["source"] == "llm"
    assert result["reasoning"] == "winner"
    assert result["model_response_ref"] is not None


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
