from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.review.application import (
    ReviewApplicationV1,
    ReviewIntentV1,
    review_application_id,
)
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ArtifactRef, canonical_artifact_refs
from app.stages.assembly_review import nodes as assembly_review_nodes
from app.stages.t2t.review import T2TReviewEffectV1


def _ref(artifact_id: str, path_ref: str, sha256: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        path_ref=path_ref,
        sha256=sha256 * 64,
        size_bytes=1,
        artifact_type="tests.artifact",
    )


def _application(required_refs: tuple[ArtifactRef, ...]) -> ReviewApplicationV1:
    payload = {
        "schema_version": 1,
        "session_id": "s1",
        "workflow_step_id": "step-000-inventory",
        "stage": "inventory",
        "gate_id": "inventory_review",
        "review_request_id": "request-1",
        "request_hash": "a" * 64,
        "intent_ref": _ref("intent", "reviews/intent.json", "a").model_dump(
            mode="json"
        ),
        "decision_ref": _ref("decision", "reviews/decision.json", "b").model_dump(
            mode="json"
        ),
        "effect_kind": "tests.effect.v1",
        "effect": {"decision": "continue"},
        "required_refs": [ref.model_dump(mode="json") for ref in required_refs],
    }
    return ReviewApplicationV1(
        **payload,
        application_id=review_application_id(payload),
    )


def test_artifact_ref_canonical_order_is_identity_order_and_deduplicates() -> None:
    first = _ref("alpha", "z/first.json", "c")
    second = _ref("bravo", "a/second.json", "d")

    assert canonical_artifact_refs((second, first, second)) == (first, second)


def test_application_and_t2t_reject_noncanonical_artifact_ref_collections() -> None:
    first = _ref("alpha", "z/first.json", "c")
    second = _ref("bravo", "a/second.json", "d")
    ordered = canonical_artifact_refs((second, first, second))

    assert _application(ordered).required_refs == ordered
    with pytest.raises(
        ValidationError, match="required_refs must be sorted and unique"
    ):
        _application((second, first))
    with pytest.raises(
        ValidationError, match="required_refs must be sorted and unique"
    ):
        _application((first, second, second))

    effect = T2TReviewEffectV1(
        decision="stop_pipeline",
        review_request_id="request-1",
        review_request_hash="a" * 64,
        review_request_ref=first,
        decision_ref=second,
        artifact_refs=ordered,
    )
    assert effect.artifact_refs == ordered
    with pytest.raises(
        ValidationError, match="artifact_refs must be sorted and unique"
    ):
        T2TReviewEffectV1(
            decision="stop_pipeline",
            review_request_id="request-1",
            review_request_hash="a" * 64,
            review_request_ref=first,
            decision_ref=second,
            artifact_refs=(second, first),
        )
    with pytest.raises(
        ValidationError, match="artifact_refs must be sorted and unique"
    ):
        T2TReviewEffectV1(
            decision="stop_pipeline",
            review_request_id="request-1",
            review_request_hash="a" * 64,
            review_request_ref=first,
            decision_ref=second,
            artifact_refs=(first, second, second),
        )


def test_assembly_review_required_refs_use_shared_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rationale_ref = _ref("alpha", "reviews/rationale.json", "c")
    decision_ref = _ref("bravo", "reviews/decision.json", "d")
    request = ResumeReviewRequest(
        session_id="s1",
        stage="assembly_review",
        gate_id="assembly_review",
        review_request_id="request-1",
        review_request_hash="c" * 64,
        decision="stop_human_review",
        rationale_artifact_ref=rationale_ref,
    )
    intent = ReviewIntentV1(
        session_id="s1",
        workflow_step_id="step-000-assembly_review",
        stage="assembly_review",
        gate_id="assembly_review",
        review_request_id="request-1",
        request_hash="c" * 64,
        request_ref=rationale_ref,
        source="test",
        request=request,
    )
    monkeypatch.setattr(
        assembly_review_nodes,
        "_persist_assembly_review_decision",
        lambda *_args, **_kwargs: decision_ref,
    )

    prepared = assembly_review_nodes._prepare_stopped_assembly_review_application(
        {"session_id": "s1"}, intent, request
    )

    assert prepared.required_refs == canonical_artifact_refs(
        (decision_ref, rationale_ref)
    )
