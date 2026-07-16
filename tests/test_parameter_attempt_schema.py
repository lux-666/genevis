"""Data-v2 schema tests for compact parameter cursors."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schemas.artifacts import (
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)


def _ref(name: str, artifact_type: str = "test.artifact") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"art_{name:0<32}"[:36],
        path_ref=f"artifacts/assembly/{name}.json",
        sha256=(name[0] if name else "a") * 64,
        size_bytes=128,
        artifact_type=artifact_type,
    )


def _attempt_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "attempt_id": 0,
        "method_id": "assembly.candidate.hifiasm",
        "status": "recommended",
        "task": "recommend_initial",
        "recommendation_manifest_ref": _ref("a").model_dump(mode="json"),
        "approval_decision_ref": _ref("b").model_dump(mode="json"),
    }
    payload.update(updates)
    return payload


def test_parameter_attempt_is_strict_compact_v2() -> None:
    dumped = ParameterAttempt.model_validate(_attempt_payload()).model_dump(mode="json")

    assert dumped["schema_version"] == 2
    assert len(json.dumps(dumped, separators=(",", ":"))) < 2_048
    forbidden = {
        "catalog_snapshot",
        "recommended_params",
        "validation",
        "argv",
        "execution_result",
        "stdout_tail",
        "stderr_tail",
    }
    assert forbidden.isdisjoint(dumped)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 1),
        ("method_id", ""),
        ("status", "typo"),
        ("task", "retry_sometime"),
        ("attempt_id", -1),
        ("catalog_snapshot", {"large": True}),
        ("execution_result", {"status": "succeeded"}),
    ],
)
def test_parameter_attempt_rejects_legacy_or_large_fields(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        ParameterAttempt.model_validate(_attempt_payload(**{field: value}))


def test_retry_requires_immediate_predecessor_and_seal() -> None:
    with pytest.raises(ValidationError):
        ParameterAttempt.model_validate(
            _attempt_payload(
                attempt_id=1,
                task="retry_after_review_failure",
                previous_attempt_id=0,
            )
        )

    retry = ParameterAttempt.model_validate(
        _attempt_payload(
            attempt_id=1,
            task="retry_after_review_failure",
            previous_attempt_id=0,
            previous_attempt_seal_hash="c" * 64,
        )
    )
    assert retry.previous_attempt_id == 0


def test_initial_attempt_cannot_bind_a_predecessor() -> None:
    with pytest.raises(ValidationError):
        ParameterAttempt.model_validate(
            _attempt_payload(previous_attempt_id=0, previous_attempt_seal_hash="c" * 64)
        )


def test_recommendation_cursor_binds_manifest_hash_and_request_ref() -> None:
    manifest = _ref("d", "assembly.parameter.recommendation_manifest")
    request = _ref("e", "assembly.parameter_approval.request")
    cursor = ParameterRecommendationCursor(
        recommendation_id=0,
        method_id="assembly.candidate.hifiasm",
        task="recommend_initial",
        status="pending_approval",
        manifest_ref=manifest,
        recommendation_hash=manifest.sha256,
        review_request_id="review_123",
        review_request_ref=request,
    )
    assert cursor.recommendation_hash == cursor.manifest_ref.sha256

    with pytest.raises(ValidationError):
        ParameterRecommendationCursor.model_validate(
            {**cursor.model_dump(mode="json"), "recommendation_hash": "f" * 64}
        )


@pytest.mark.parametrize("path_ref", ["data/artifacts/a.json", "/tmp/a.json", "../a.json"])
def test_artifact_ref_rejects_nonportable_paths(path_ref: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id="art_" + "a" * 32,
            path_ref=path_ref,
            sha256="a" * 64,
            size_bytes=1,
            artifact_type="test",
        )
