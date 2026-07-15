"""Compatibility and validation tests for durable parameter-attempt artifacts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.artifacts import ArtifactRef, ParameterAttempt


def _attempt_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "attempt_id": 0,
        "method_id": "assembly.candidate.hifiasm",
        "status": "recommended",
        "input_profile_ref": {
            "path_ref": "profile.json",
            "sha256": "a" * 64,
            "artifact_type": "assembly.parameter.input_profile",
        },
        "prompt_payload_ref": {
            "path_ref": "prompt.json",
            "sha256": "b" * 64,
            "artifact_type": "assembly.parameter.prompt_payload",
        },
    }
    payload.update(updates)
    return payload


def test_unversioned_legacy_attempt_round_trip_does_not_invent_version() -> None:
    raw = _attempt_payload(legacy_extension={"producer": "phase10"})

    dumped = ParameterAttempt.model_validate(raw).model_dump(mode="json")

    assert "schema_version" not in dumped
    assert dumped["legacy_extension"] == {"producer": "phase10"}


def test_explicit_schema_version_is_preserved() -> None:
    dumped = ParameterAttempt.model_validate(
        _attempt_payload(schema_version=1)
    ).model_dump(mode="json")

    assert dumped["schema_version"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("method_id", ""),
        ("status", "typo"),
        ("task", "retry_sometime"),
        ("failure_class", ""),
        ("attempt_id", -1),
        ("retrieval_eligible_case_count", -1),
    ],
)
def test_obvious_scalar_contracts_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ParameterAttempt.model_validate(_attempt_payload(**{field: value}))


@pytest.mark.parametrize(
    ("task", "attempt_kind", "retry_origin"),
    [
        ("recommend_initial", "retry", None),
        ("retry_after_execution_failure", "initial", "assembly_execution"),
        ("retry_after_execution_failure", "retry", "assembly_review"),
        ("retry_after_review_failure", "retry", "assembly_execution"),
    ],
)
def test_explicit_task_lifecycle_contradictions_are_rejected(
    task: str,
    attempt_kind: str,
    retry_origin: str | None,
) -> None:
    with pytest.raises(ValidationError):
        ParameterAttempt.model_validate(
            _attempt_payload(
                task=task,
                attempt_kind=attempt_kind,
                retry_origin=retry_origin,
            )
        )


def test_legacy_retry_without_lifecycle_discriminators_remains_readable() -> None:
    attempt = ParameterAttempt.model_validate(
        _attempt_payload(task="retry_after_execution_failure")
    )

    assert attempt.task == "retry_after_execution_failure"
    assert attempt.attempt_kind == "initial"
    assert attempt.retry_origin is None


def test_current_retry_contract_is_accepted() -> None:
    attempt = ParameterAttempt(
        **_attempt_payload(
            attempt_id=1,
            task="retry_after_review_failure",
            attempt_kind="retry",
            retry_origin="assembly_review",
            previous_attempt_id=0,
        )
    )

    assert isinstance(attempt.input_profile_ref, ArtifactRef)
