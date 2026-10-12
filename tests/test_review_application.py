from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.review.application import (
    REVIEW_APPLICATION_ARTIFACT_TYPE,
    EmptyReviewMetadataV1,
    PreparedReviewApplication,
    ReviewApplicationConflictError,
    ReviewApplicationIdentity,
    ReviewApplicationIntegrityError,
    ReviewIntentConflictError,
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
    review_application_id,
)
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.review.ledger import record_review_applied, record_review_decision
from app.review.persistence import (
    persist_review_decision,
    persist_review_request,
    publish_review_json_record,
)
from app.review.status import project_review_history, project_review_status
from app.schemas.artifacts import ArtifactRef, ParameterAttempt
from app.schemas.state import initial_state
from app.storage.artifacts import publish_or_recover_work_artifact
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class _InventoryContinueEffectV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    decision: str


class _NestedParameterAttemptEffectV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    decision_ref: ArtifactRef
    approved_attempt: ParameterAttempt


class _OpaqueMetadataEffectV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    paths: tuple[str, ...]
    metadata: dict[str, Any]


def _blocked_inventory(tmp_path: Path):
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
    )
    state.update(
        {
            "entered_workflow_step_index": 0,
            "supervision_state": "blocked_human",
            "stage_outputs": {"inventory": {"status": "blocked_human"}},
        }
    )
    persisted_request = persist_review_request(
        state,
        stage="inventory",
        gate_id="inventory_review",
        reason="inventory_review_required",
        payload=ReviewPayload(
            stage="inventory",
            gate_id="inventory_review",
            reason="inventory_review_required",
            decisions_allowed=["continue", "stop_human_review"],
        ),
    )
    state["active_interrupt"] = persisted_request.compact_interrupt(
        stage="inventory",
        gate_id="inventory_review",
        reason="inventory_review_required",
    )
    request = ResumeReviewRequest(
        session_id="s1",
        stage="inventory",
        gate_id="inventory_review",
        review_request_id=persisted_request.review_request_id,
        review_request_hash=persisted_request.request_hash,
        decision="continue",
    )
    return run_dir, state, persisted_request, request


def _checkpoint_with_application(
    state: dict[str, Any], application_ref: ArtifactRef
) -> dict[str, Any]:
    return {
        **state,
        "supervision_state": "running",
        "active_interrupt": None,
        "review_application_ref": application_ref.model_dump(mode="json"),
    }


def _fake_gate(
    effect_kind: str = "inventory.review_application.v1",
    effect_model: type[BaseModel] = _InventoryContinueEffectV1,
):
    return SimpleNamespace(
        effect_kind=effect_kind,
        decode_effect=lambda value: effect_model.model_validate(value),
        decode_intent_metadata=lambda value: EmptyReviewMetadataV1.model_validate(
            value
        ),
        load_validate_intent_request=lambda: lambda _state, request, _record: request,
    )


def _publish_test_artifact(run_dir: Path, name: str) -> ArtifactRef:
    paths = SessionPaths.from_run_dir(run_dir)
    source = paths.workflow_step_work_path(
        "step-000-inventory",
        "inventory",
        "review_application_tests",
        name,
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"test artifact: {name}\n", encoding="utf-8")
    return publish_or_recover_work_artifact(
        run_dir,
        "s1",
        source,
        stage="inventory",
        method_name="tests.review_application",
        artifact_type="tests.nested_parameter_ref",
    ).ref


def _nested_parameter_effect(
    decision_ref: ArtifactRef,
    nested_ref: ArtifactRef,
) -> _NestedParameterAttemptEffectV1:
    return _NestedParameterAttemptEffectV1(
        decision_ref=decision_ref,
        approved_attempt=ParameterAttempt(
            schema_version=3,
            attempt_id=0,
            method_id="tests.method",
            status="recommended",
            task="recommend_initial",
            recommendation_manifest_ref=nested_ref,
            approval_decision_ref=decision_ref,
        ),
    )


def test_intent_replay_is_idempotent_and_conflicting_decision_fails_closed(
    tmp_path: Path,
) -> None:
    _run_dir, state, _persisted_request, request = _blocked_inventory(tmp_path)

    first = persist_review_intent(state, request, source="review")
    replay = persist_review_intent(state, request, source="review")

    assert replay.ref == first.ref
    assert replay.intent == first.intent
    assert first.intent.request.decision == "continue"
    assert "timestamp" not in json.loads(
        (Path(state["data_dir"]) / first.ref.path_ref).read_text(encoding="utf-8")
    )

    conflicting = request.model_copy(update={"decision": "stop_human_review"})
    with pytest.raises(ReviewIntentConflictError, match="intent"):
        persist_review_intent(state, conflicting, source="review")


def test_application_id_excludes_itself_and_verified_loader_decodes_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )

    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=_InventoryContinueEffectV1(decision="continue"),
            decision_ref=decision_ref,
        ),
    )
    identity = ReviewApplicationIdentity.from_intent(persisted_intent.intent)
    verified = load_verified_review_application(
        run_dir,
        application_ref,
        expected_identity=identity,
    )

    assert verified.ref == application_ref
    assert isinstance(verified.effect, _InventoryContinueEffectV1)
    assert verified.effect.decision == "continue"
    record = verified.application.model_dump(mode="json")
    identifier = record.pop("application_id")
    assert identifier == review_application_id(record)
    assert "application_id" not in record


def test_application_replay_reuses_ref_and_conflicts_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )
    prepared = PreparedReviewApplication(
        effect_kind="inventory.review_application.v1",
        effect=_InventoryContinueEffectV1(decision="continue"),
        decision_ref=decision_ref,
    )

    first = persist_review_application(
        state, intent=persisted_intent, prepared=prepared
    )
    replay = persist_review_application(
        state, intent=persisted_intent, prepared=prepared
    )
    assert replay == first

    conflicting = PreparedReviewApplication(
        effect_kind="inventory.review_application.v1",
        effect=_InventoryContinueEffectV1(decision="stop_human_review"),
        decision_ref=decision_ref,
    )
    with pytest.raises(ReviewApplicationConflictError, match="application"):
        persist_review_application(
            state,
            intent=persisted_intent,
            prepared=conflicting,
        )


def test_verified_loader_recursively_verifies_nested_parameter_attempt_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    nested_ref = _publish_test_artifact(run_dir, "nested-manifest.json")
    effect_kind = "tests.nested_parameter_attempt.v1"
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(
            effect_kind,
            _NestedParameterAttemptEffectV1,
        ),
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind=effect_kind,
            effect=_nested_parameter_effect(decision_ref, nested_ref),
            decision_ref=decision_ref,
        ),
    )
    verified = load_verified_review_application(run_dir, application_ref)
    assert nested_ref in verified.application.required_refs

    nested_path = SessionPaths.from_run_dir(run_dir).resolve_ref(nested_ref.path_ref)
    nested_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ReviewApplicationIntegrityError, match="integrity mismatch"):
        load_verified_review_application(run_dir, application_ref)


def test_application_rejects_external_absolute_path_in_dynamic_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    effect_kind = "tests.opaque_metadata.v1"
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(effect_kind, _OpaqueMetadataEffectV1),
    )

    with pytest.raises(
        ReviewApplicationIntegrityError,
        match="external absolute path in dynamic metadata",
    ):
        persist_review_application(
            state,
            intent=persisted_intent,
            prepared=PreparedReviewApplication(
                effect_kind=effect_kind,
                effect=_OpaqueMetadataEffectV1(
                    paths=("/external/input.fastq",),
                    metadata={"external_tool_output": "/external/output.fa"},
                ),
                decision_ref=decision_ref,
            ),
        )

    application_path = SessionPaths.from_run_dir(run_dir).workflow_step_artifact_path(
        persisted_intent.intent.workflow_step_id,
        persisted_intent.intent.stage,
        "reviews",
        persisted_intent.intent.review_request_id,
        "application.json",
    )
    assert not application_path.exists()


def test_application_keeps_external_typed_paths_and_tracks_dynamic_metadata_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    nested_ref = _publish_test_artifact(run_dir, "dynamic-metadata-ref.json")
    effect_kind = "tests.opaque_metadata.v1"
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(effect_kind, _OpaqueMetadataEffectV1),
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind=effect_kind,
            effect=_OpaqueMetadataEffectV1(
                paths=("/external/input.fastq",),
                metadata={
                    "provenance": {"artifact": nested_ref.model_dump(mode="json")}
                },
            ),
            decision_ref=decision_ref,
        ),
    )

    verified = load_verified_review_application(run_dir, application_ref)
    assert nested_ref in verified.application.required_refs
    assert verified.effect.paths == ("/external/input.fastq",)


def test_application_rejects_incomplete_serialized_dynamic_metadata_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    nested_ref = _publish_test_artifact(run_dir, "incomplete-dynamic-metadata-ref.json")
    effect_kind = "tests.opaque_metadata.v1"
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(effect_kind, _OpaqueMetadataEffectV1),
    )

    incomplete_ref = nested_ref.model_dump(mode="json")
    incomplete_ref.pop("artifact_id")
    with pytest.raises(
        ReviewApplicationIntegrityError,
        match="invalid serialized ArtifactRef",
    ):
        persist_review_application(
            state,
            intent=persisted_intent,
            prepared=PreparedReviewApplication(
                effect_kind=effect_kind,
                effect=_OpaqueMetadataEffectV1(
                    paths=("/external/input.fastq",),
                    metadata={"provenance": incomplete_ref},
                ),
                decision_ref=decision_ref,
            ),
        )


def test_verified_loader_rejects_application_omitting_dynamic_metadata_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    nested_ref = _publish_test_artifact(run_dir, "omitted-dynamic-metadata-ref.json")
    effect_kind = "tests.opaque_metadata.v1"
    effect = _OpaqueMetadataEffectV1(
        paths=("/external/input.fastq",),
        metadata={"provenance": {"artifact": nested_ref.model_dump(mode="json")}},
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(effect_kind, _OpaqueMetadataEffectV1),
    )
    identity = ReviewApplicationIdentity.from_intent(persisted_intent.intent)
    payload = {
        "schema_version": 1,
        **identity.model_dump(mode="json"),
        "intent_ref": persisted_intent.ref.model_dump(mode="json"),
        "decision_ref": decision_ref.model_dump(mode="json"),
        "effect_kind": effect_kind,
        "effect": effect.model_dump(mode="json"),
        "required_refs": [],
    }
    application_payload = {
        **payload,
        "application_id": review_application_id(payload),
    }
    paths = SessionPaths.from_run_dir(run_dir)
    application_ref = publish_review_json_record(
        paths,
        session_id="s1",
        destination=paths.workflow_step_artifact_path(
            identity.workflow_step_id,
            identity.stage,
            "reviews",
            identity.review_request_id,
            "application.json",
        ),
        payload=application_payload,
        stage=identity.stage,
        method_name="tests.review_application.invalid",
        artifact_type=REVIEW_APPLICATION_ARTIFACT_TYPE,
        metadata={
            "workflow_step_id": identity.workflow_step_id,
            "review_request_id": identity.review_request_id,
        },
    )

    with pytest.raises(
        ReviewApplicationIntegrityError,
        match="required_refs omit dynamic metadata refs",
    ):
        load_verified_review_application(run_dir, application_ref)


def test_verified_loader_rejects_application_omitting_typed_effect_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    nested_ref = _publish_test_artifact(run_dir, "omitted-manifest.json")
    effect_kind = "tests.nested_parameter_attempt.v1"
    effect = _nested_parameter_effect(decision_ref, nested_ref)
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(
            effect_kind,
            _NestedParameterAttemptEffectV1,
        ),
    )
    identity = ReviewApplicationIdentity.from_intent(persisted_intent.intent)
    payload = {
        "schema_version": 1,
        **identity.model_dump(mode="json"),
        "intent_ref": persisted_intent.ref.model_dump(mode="json"),
        "decision_ref": decision_ref.model_dump(mode="json"),
        "effect_kind": effect_kind,
        "effect": effect.model_dump(mode="json"),
        "required_refs": [decision_ref.model_dump(mode="json")],
    }
    application_payload = {
        **payload,
        "application_id": review_application_id(payload),
    }
    paths = SessionPaths.from_run_dir(run_dir)
    application_ref = publish_review_json_record(
        paths,
        session_id="s1",
        destination=paths.workflow_step_artifact_path(
            identity.workflow_step_id,
            identity.stage,
            "reviews",
            identity.review_request_id,
            "application.json",
        ),
        payload=application_payload,
        stage=identity.stage,
        method_name="tests.review_application.invalid",
        artifact_type=REVIEW_APPLICATION_ARTIFACT_TYPE,
        metadata={
            "workflow_step_id": identity.workflow_step_id,
            "review_request_id": identity.review_request_id,
        },
    )

    with pytest.raises(
        ReviewApplicationIntegrityError,
        match="required_refs omit typed effect refs",
    ):
        load_verified_review_application(run_dir, application_ref)


def test_verified_loader_rejects_wrong_occurrence_or_effect_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=_InventoryContinueEffectV1(decision="continue"),
            decision_ref=decision_ref,
        ),
    )

    wrong_identity = ReviewApplicationIdentity.from_intent(
        persisted_intent.intent
    ).model_copy(update={"workflow_step_id": "step-999-inventory"})
    with pytest.raises(ReviewApplicationIntegrityError, match="workflow_step_id"):
        load_verified_review_application(
            run_dir,
            application_ref,
            expected_identity=wrong_identity,
        )

    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate("inventory.wrong.v1"),
    )
    with pytest.raises(ReviewApplicationIntegrityError, match="effect_kind"):
        load_verified_review_application(
            run_dir,
            application_ref,
            expected_identity=ReviewApplicationIdentity.from_intent(
                persisted_intent.intent
            ),
        )


def test_review_applied_event_is_typed_verified_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=_InventoryContinueEffectV1(decision="continue"),
            decision_ref=decision_ref,
        ),
    )
    identity = ReviewApplicationIdentity.from_intent(persisted_intent.intent)
    ledger = Ledger(run_dir)
    checkpoint_state = _checkpoint_with_application(state, application_ref)

    with pytest.raises(
        ReviewApplicationIntegrityError,
        match="verified review_decision",
    ):
        record_review_applied(
            ledger,
            "s1",
            application_ref=application_ref,
            expected_identity=identity,
            checkpoint_state=checkpoint_state,
        )
    assert not any(
        event["event_type"] == "review_applied" for event in ledger.read_events("s1")
    )

    record_review_decision(
        ledger,
        "s1",
        application_ref=application_ref,
        expected_identity=identity,
    )
    with pytest.raises(
        ReviewApplicationIntegrityError,
        match="valid application ref",
    ):
        record_review_applied(
            ledger,
            "s1",
            application_ref=application_ref,
            expected_identity=identity,
            checkpoint_state=state,
        )
    blocked_checkpoint = {
        **state,
        "review_application_ref": application_ref.model_dump(mode="json"),
    }
    with pytest.raises(
        ReviewApplicationIntegrityError,
        match="has not progressed beyond the review gate",
    ):
        record_review_applied(
            ledger,
            "s1",
            application_ref=application_ref,
            expected_identity=identity,
            checkpoint_state=blocked_checkpoint,
        )

    first = record_review_applied(
        ledger,
        "s1",
        application_ref=application_ref,
        expected_identity=identity,
        checkpoint_state=checkpoint_state,
    )
    replay = record_review_applied(
        ledger,
        "s1",
        application_ref=application_ref,
        expected_identity=identity,
        checkpoint_state=checkpoint_state,
    )

    assert replay == first
    assert first["event_type"] == "review_applied"
    assert first["workflow_step_id"] == "step-000-inventory"
    assert first["subject_id"] == persisted_request.review_request_id
    assert first["summary"]["application_id"].startswith("review_app_")
    assert first["summary"]["request_hash"] == persisted_request.request_hash
    assert first["refs"][0]["path_ref"] == application_ref.path_ref


def test_review_status_projects_all_four_durable_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    identity = ReviewApplicationIdentity(
        session_id="s1",
        workflow_step_id="step-000-inventory",
        stage="inventory",
        gate_id="inventory_review",
        review_request_id=persisted_request.review_request_id,
        request_hash=persisted_request.request_hash,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )

    awaiting = project_review_status(state, identity=identity)
    assert awaiting.status == "awaiting_decision"
    assert awaiting.integrity_error is None

    persisted_intent = persist_review_intent(state, request, source="review")
    committed = project_review_status(state, identity=identity)
    assert committed.status == "decision_committed"
    assert committed.intent_ref == persisted_intent.ref

    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=_InventoryContinueEffectV1(decision="continue"),
            decision_ref=decision_ref,
        ),
    )
    prepared = project_review_status(state, identity=identity)
    assert prepared.status == "application_prepared"
    assert prepared.application_ref == application_ref
    assert prepared.decision_sequence is None

    decision_event = record_review_decision(
        Ledger(run_dir),
        "s1",
        application_ref=application_ref,
        expected_identity=identity,
    )
    decided = project_review_status(state, identity=identity)
    assert decided.status == "application_prepared"
    assert decided.decision_sequence == decision_event["sequence"]

    checkpoint_applied = _checkpoint_with_application(state, application_ref)
    applied = project_review_status(checkpoint_applied, identity=identity)
    assert applied.status == "applied"
    assert applied.applied_sequence is None

    applied_event = record_review_applied(
        Ledger(run_dir),
        "s1",
        application_ref=application_ref,
        expected_identity=identity,
        checkpoint_state=checkpoint_applied,
    )
    event_applied = project_review_status(state, identity=identity)
    assert event_applied.status == "applied"
    assert event_applied.integrity_error is None
    assert event_applied.decision_sequence == decision_event["sequence"]
    assert event_applied.applied_sequence == applied_event["sequence"]


def test_review_applied_without_verified_decision_is_integrity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=_InventoryContinueEffectV1(decision="continue"),
            decision_ref=decision_ref,
        ),
    )
    identity = ReviewApplicationIdentity.from_intent(persisted_intent.intent)
    application = load_verified_review_application(run_dir, application_ref)
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_review_requested(
        "s1",
        "inventory",
        reason="inventory_review_required",
        review_request_artifact_ref=persisted_request.request_ref,
        review_request_id=persisted_request.review_request_id,
    )

    # Corrupt-row injection deliberately bypasses the public typed writer.
    ledger._record_event(
        "s1",
        "review_applied",
        {
            "review_request_id": identity.review_request_id,
            "request_hash": identity.request_hash,
            "gate_id": identity.gate_id,
            "application_id": application.application.application_id,
        },
        stage=identity.stage,
        workflow_step_id=identity.workflow_step_id,
        subject_id=identity.review_request_id,
        refs=[
            {
                **application.ref.model_dump(mode="json"),
                "role": "application_ref",
            }
        ],
        idempotency_key="test:corrupt-review-applied-without-decision",
    )

    status = project_review_status(state, identity=identity)

    assert status.status == "application_prepared"
    assert status.decision_sequence is None
    assert "without a verified review_decision" in str(status.integrity_error)

    from app.control.inspection import inspect_session

    summary = inspect_session("s1", run_dir)
    occurrence = summary["review_occurrences"][0]
    assert occurrence["status"] == "application_prepared"
    assert occurrence["integrity_error"] is not None
    assert summary["latest_review_lifecycle_event"]["event_type"] == "review_requested"


@pytest.mark.parametrize(
    ("case", "error_field"),
    [
        ("application_id", "application_id"),
        ("application_ref", "application_ref"),
        ("gate_id", "gate_id"),
        ("decision", "decision"),
        ("decided_by", "decided_by"),
        ("candidate_id", "candidate_id"),
        ("method_name", "method_name"),
    ],
)
def test_review_status_rejects_decision_not_bound_to_verified_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    error_field: str,
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=_InventoryContinueEffectV1(decision="continue"),
            decision_ref=decision_ref,
        ),
    )
    verified = load_verified_review_application(run_dir, application_ref)
    application = verified.application
    payload = {
        "review_request_id": application.review_request_id,
        "request_hash": application.request_hash,
        "gate_id": application.gate_id,
        "decision": request.decision,
        "candidate_id": request.candidate_id,
        "rejected_candidates": request.rejected_candidates,
        "rejected_candidate_count": len(request.rejected_candidates),
        "note_count": len(request.notes),
        "method_name": request.method_name,
        "decided_by": request.decided_by,
        "application_id": application.application_id,
        "effect_kind": application.effect_kind,
        "source": persisted_intent.intent.source,
    }
    refs = [
        {**application.decision_ref.model_dump(mode="json"), "role": "artifact_ref"},
        {**application.intent_ref.model_dump(mode="json"), "role": "intent_ref"},
        {**verified.ref.model_dump(mode="json"), "role": "application_ref"},
    ]
    if case == "application_ref":
        refs[-1] = {
            **application.decision_ref.model_dump(mode="json"),
            "role": "application_ref",
        }
    else:
        payload[case] = {
            "application_id": "review_app_000000000000000000000000",
            "gate_id": "wrong_gate",
            "decision": "stop_human_review",
            "decided_by": "auto_rule",
            "candidate_id": "wrong_candidate",
            "method_name": "wrong_method",
        }[case]
    Ledger(run_dir)._record_event(
        "s1",
        "review_decision",
        payload,
        stage=application.stage,
        workflow_step_id=application.workflow_step_id,
        subject_id=application.review_request_id,
        refs=refs,
        idempotency_key=f"test:malformed-review-decision:{case}",
    )
    identity = ReviewApplicationIdentity.from_intent(persisted_intent.intent)

    status = project_review_status(state, identity=identity)

    assert status.status == "application_prepared"
    assert error_field in str(status.integrity_error)
    assert status.decision_sequence is None


def test_review_status_rejects_multiple_fully_bound_decision_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    persisted_intent = persist_review_intent(state, request, source="review")
    decision_ref = persist_review_decision(
        run_dir,
        session_id="s1",
        review_request_id=persisted_request.review_request_id,
        stage="inventory",
        gate_id="inventory_review",
        decision=request,
        request_hash=persisted_request.request_hash,
        request_ref=persisted_request.request_ref,
    )
    monkeypatch.setattr(
        "app.review.application.get_review_gate",
        lambda _stage, _gate_id: _fake_gate(),
    )
    application_ref = persist_review_application(
        state,
        intent=persisted_intent,
        prepared=PreparedReviewApplication(
            effect_kind="inventory.review_application.v1",
            effect=_InventoryContinueEffectV1(decision="continue"),
            decision_ref=decision_ref,
        ),
    )
    identity = ReviewApplicationIdentity.from_intent(persisted_intent.intent)
    ledger = Ledger(run_dir)
    first = record_review_decision(
        ledger,
        "s1",
        application_ref=application_ref,
        expected_identity=identity,
    )
    ledger._record_event(
        "s1",
        "review_decision",
        first["summary"],
        stage=first["stage"],
        workflow_step_id=first["workflow_step_id"],
        subject_id=first["subject_id"],
        refs=first["refs"],
        idempotency_key="test:duplicate-fully-bound-review-decision",
    )

    status = project_review_status(state, identity=identity)

    assert status.status == "application_prepared"
    assert "multiple review_decision" in str(status.integrity_error)
    assert status.decision_sequence is None


def test_partial_review_evidence_never_implies_applied(tmp_path: Path) -> None:
    from app.control.inspection import inspect_session

    run_dir, state, persisted_request, _request = _blocked_inventory(tmp_path)
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_review_requested(
        "s1",
        "inventory",
        reason="inventory_review_required",
        review_request_artifact_ref=persisted_request.request_ref,
        review_request_id=persisted_request.review_request_id,
    )
    ledger._record_event(
        "s1",
        "review_decision",
        {
            "review_request_id": persisted_request.review_request_id,
            "request_hash": persisted_request.request_hash,
            "decision": "continue",
        },
        stage="inventory",
        workflow_step_id="step-000-inventory",
        subject_id=persisted_request.review_request_id,
    )
    identity = ReviewApplicationIdentity(
        session_id="s1",
        workflow_step_id="step-000-inventory",
        stage="inventory",
        gate_id="inventory_review",
        review_request_id=persisted_request.review_request_id,
        request_hash=persisted_request.request_hash,
    )

    partial = project_review_status(state, identity=identity)
    assert partial.status == "awaiting_decision"
    assert partial.accepts_new_decision is False
    assert "without a verified intent/application" in str(partial.integrity_error)
    partial_summary = inspect_session("s1", run_dir)
    assert partial_summary["next_action"]["kind"] == "review_integrity_error"

    ledger.record_stage_completed(
        "s1",
        "inventory",
        workflow_step_id="step-000-inventory",
    )
    still_partial = project_review_status(state, identity=identity)
    assert still_partial.status == "awaiting_decision"
    assert still_partial.accepts_new_decision is False
    assert "without a verified intent/application" in str(still_partial.integrity_error)


def test_review_status_rejects_partial_identity_hash_mismatch(tmp_path: Path) -> None:
    run_dir, state, persisted_request, _request = _blocked_inventory(tmp_path)
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_review_requested(
        "s1",
        "inventory",
        reason="inventory_review_required",
        review_request_artifact_ref=persisted_request.request_ref,
        review_request_id=persisted_request.review_request_id,
    )
    ledger._record_event(
        "s1",
        "review_decision",
        {
            "review_request_id": persisted_request.review_request_id,
            "request_hash": "0" * 64,
            "decision": "continue",
        },
        stage="inventory",
        workflow_step_id="step-000-inventory",
        subject_id=persisted_request.review_request_id,
    )
    identity = ReviewApplicationIdentity(
        session_id="s1",
        workflow_step_id="step-000-inventory",
        stage="inventory",
        gate_id="inventory_review",
        review_request_id=persisted_request.review_request_id,
        request_hash=persisted_request.request_hash,
    )

    status = project_review_status(state, identity=identity)

    assert status.status == "awaiting_decision"
    assert status.accepts_new_decision is False
    assert "request_hash does not match" in str(status.integrity_error)


def test_post_terminal_progression_is_diagnostic_not_review_resolution(
    tmp_path: Path,
) -> None:
    run_dir, state, persisted_request, _request = _blocked_inventory(tmp_path)
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_review_requested(
        "s1",
        "inventory",
        reason="inventory_review_required",
        review_request_artifact_ref=persisted_request.request_ref,
        review_request_id=persisted_request.review_request_id,
    )
    ledger.record_session_terminal("s1", outcome="failed", stage="bootstrap")
    ledger.record_stage_entered(
        "s1",
        "read_qc",
        workflow_step_id="step-001-read_qc",
    )
    identity = ReviewApplicationIdentity(
        session_id="s1",
        workflow_step_id="step-000-inventory",
        stage="inventory",
        gate_id="inventory_review",
        review_request_id=persisted_request.review_request_id,
        request_hash=persisted_request.request_hash,
    )

    status = project_review_status(state, identity=identity)

    assert status.status == "awaiting_decision"
    assert status.accepts_new_decision is False
    assert status.resolution_basis == "none"
    assert "after the canonical session terminal" in str(status.integrity_error)


def test_committed_intent_projects_same_decision_recovery_action(
    tmp_path: Path,
) -> None:
    from app.control.inspection import inspect_session

    run_dir, state, persisted_request, request = _blocked_inventory(tmp_path)
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_review_requested(
        "s1",
        "inventory",
        reason="inventory_review_required",
        review_request_artifact_ref=persisted_request.request_ref,
        review_request_id=persisted_request.review_request_id,
    )
    persisted_intent = persist_review_intent(state, request, source="review")

    summary = inspect_session("s1", run_dir)

    assert summary["latest_review_status"]["status"] == "decision_committed"
    assert summary["decision_requirements"] is None
    assert summary["next_action"]["kind"] == "review_recovery_required"
    assert "--decision-file" not in summary["next_action"]["command"]
    assert persisted_intent.ref.path_ref in summary["next_action"]["relevant_refs"]


def test_post_terminal_review_request_is_diagnostic_and_cannot_reopen(
    tmp_path: Path,
) -> None:
    from app.control.inspection import inspect_session

    run_dir, _state, persisted_request, _request = _blocked_inventory(tmp_path)
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_session_terminal("s1", outcome="completed", stage="done")
    ledger.record_review_requested(
        "s1",
        "inventory",
        reason="malformed_post_terminal_request",
        review_request_artifact_ref=persisted_request.request_ref,
        review_request_id=persisted_request.review_request_id,
    )

    history = project_review_history(run_dir, "s1")

    assert history.canonical_terminal is not None
    assert history.latest_unapplied is None
    assert len(history.occurrences) == 1
    occurrence = history.occurrences[0]
    assert occurrence.status == "applied"
    assert occurrence.resolution_basis == "terminal_precedes_request"
    assert "after the canonical" in str(occurrence.integrity_error)
    summary = inspect_session("s1", run_dir)
    assert summary["supervision_state"] == "completed"
    assert summary["latest_unresolved_review_request"] is None
    assert summary["next_action"]["kind"] == "completed"


def test_later_occurrence_does_not_close_an_unverified_review(tmp_path: Path) -> None:
    run_dir = v2_run_dir(tmp_path / "data", "s1")
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
        workflow_steps=[
            {
                "selected_method_names": [],
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "selected_method_names": [],
                "step_id": "step-001-read_qc",
                "stage": "read_qc",
                "allow_local_execution": False,
            },
            {
                "selected_method_names": [],
                "step_id": "step-002-read_qc",
                "stage": "read_qc",
                "allow_local_execution": False,
            },
        ],
    )
    state.update(
        {
            "workflow_step_index": 1,
            "entered_workflow_step_index": 1,
            "current_stage": "read_qc",
            "supervision_state": "blocked_human",
            "stage_outputs": {"read_qc": {"status": "blocked_human"}},
        }
    )
    persisted_request = persist_review_request(
        state,
        stage="read_qc",
        gate_id="legacy_gate",
        reason="legacy_review_required",
        payload=ReviewPayload(
            stage="read_qc",
            gate_id="legacy_gate",
            reason="legacy_review_required",
            decisions_allowed=["continue", "stop_human_review"],
        ),
    )
    state["active_interrupt"] = persisted_request.compact_interrupt(
        stage="read_qc",
        gate_id="legacy_gate",
        reason="legacy_review_required",
    )
    ledger = Ledger(run_dir)
    ledger.create_session("s1", "control/manifest.yaml")
    ledger.record_review_requested(
        "s1",
        "read_qc",
        reason="legacy_review_required",
        review_request_artifact_ref=persisted_request.request_ref,
        review_request_id=persisted_request.review_request_id,
    )
    ledger._record_event(
        "s1",
        "review_decision",
        {"review_request_id": persisted_request.review_request_id},
        stage="read_qc",
        workflow_step_id="step-001-read_qc",
        subject_id=persisted_request.review_request_id,
    )
    identity = ReviewApplicationIdentity(
        session_id="s1",
        workflow_step_id="step-001-read_qc",
        stage="read_qc",
        gate_id="legacy_gate",
        review_request_id=persisted_request.review_request_id,
        request_hash=persisted_request.request_hash,
    )

    ledger.record_stage_entered(
        "s1",
        "read_qc",
        workflow_step_id="step-999-read_qc",
    )
    unrelated = project_review_status(state, identity=identity)
    assert unrelated.status == "awaiting_decision"
    assert unrelated.integrity_error is not None

    ledger.record_stage_entered(
        "s1",
        "read_qc",
        workflow_step_id="step-002-read_qc",
    )
    still_unverified = project_review_status(state, identity=identity)
    assert still_unverified.status == "awaiting_decision"
    assert still_unverified.accepts_new_decision is False
    assert "request_hash does not match" in str(still_unverified.integrity_error)
