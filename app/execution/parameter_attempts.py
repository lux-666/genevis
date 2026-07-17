"""Shared ownership helpers for parameterized execution attempts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.execution.evidence import load_frozen_attempt
from app.schemas.artifacts import ParameterAttempt
from app.schemas.descriptors import FrozenAttemptSpec

_APPEND_ONCE_FIELDS = frozenset(
    {
        "frozen_parameters_ref",
        "command_ref",
        "frozen_attempt_ref",
        "execution_ref",
        "review_ref",
    }
)
_MUTABLE_FIELDS = frozenset({"status", *_APPEND_ONCE_FIELDS})


class ParameterAttemptStateError(ValueError):
    """Raised when a checkpoint's parameter-attempt relation is invalid."""


def current_parameter_attempt(
    output: Mapping[str, Any],
    *,
    method_id: str | None = None,
    label: str,
) -> ParameterAttempt:
    """Return the unique typed attempt selected by the stage cursor.

    ``method_id`` is required at method execution boundaries. Generic retry and
    routing code may omit it when the selected attempt itself is the source of
    method identity.
    """

    if "current_parameter_attempt_id" not in output:
        raise ParameterAttemptStateError(
            f"{label} current_parameter_attempt_id must be an explicit "
            "non-negative integer"
        )
    attempt_id = output["current_parameter_attempt_id"]
    if type(attempt_id) is not int or attempt_id < 0:
        raise ParameterAttemptStateError(
            f"{label} current_parameter_attempt_id must be an explicit "
            f"non-negative integer, got {attempt_id!r}"
        )
    matches = [
        item
        for item in (output.get("parameter_attempts") or [])
        if isinstance(item, dict) and item.get("attempt_id") == attempt_id
    ]
    if len(matches) != 1:
        raise ParameterAttemptStateError(
            f"{label} requires exactly one current ParameterAttempt "
            f"with attempt_id={attempt_id}"
        )
    try:
        attempt = ParameterAttempt.model_validate(matches[0])
    except ValidationError as exc:
        raise ParameterAttemptStateError(
            f"{label} current parameter attempt is invalid: {exc}"
        ) from exc
    if method_id is not None and attempt.method_id != method_id:
        raise ParameterAttemptStateError(
            f"{label} current parameter attempt method_id {attempt.method_id!r} "
            f"does not match {method_id!r}"
        )
    return attempt


def update_current_parameter_attempt(
    output: Mapping[str, Any],
    *,
    method_id: str,
    label: str,
    **updates: Any,
) -> list[dict[str, Any]]:
    """Update the current compact cursor while preserving the attempt chain."""

    attempt = current_parameter_attempt(output, method_id=method_id, label=label)
    if attempt.seal_hash:
        raise ParameterAttemptStateError(
            f"{label} current parameter attempt is sealed and cannot be mutated"
        )
    unexpected = set(updates).difference(_MUTABLE_FIELDS)
    if unexpected:
        raise ParameterAttemptStateError(
            f"{label} attempted to embed non-cursor fields in ParameterAttempt: "
            + ", ".join(sorted(unexpected))
        )
    for field in _APPEND_ONCE_FIELDS:
        if field not in updates or getattr(attempt, field) is None:
            continue
        if getattr(attempt, field) != updates[field]:
            raise ParameterAttemptStateError(
                f"{label} current parameter attempt {field} is append-once"
            )
    try:
        updated = ParameterAttempt.model_validate(
            {**attempt.model_dump(mode="json"), **updates}
        ).model_dump(mode="json")
    except ValidationError as exc:
        raise ParameterAttemptStateError(
            f"{label} parameter attempt update is invalid: {exc}"
        ) from exc

    attempts: list[dict[str, Any]] = []
    for index, item in enumerate(output.get("parameter_attempts") or []):
        if not isinstance(item, dict):
            raise ParameterAttemptStateError(
                f"{label} parameter attempt chain contains a non-mapping entry"
            )
        if item.get("attempt_id") == attempt.attempt_id:
            attempts.append(updated)
        else:
            try:
                historical = ParameterAttempt.model_validate(item)
            except ValidationError as exc:
                raise ParameterAttemptStateError(
                    f"{label} historical parameter attempt at index {index} "
                    f"is invalid: {exc}"
                ) from exc
            attempts.append(historical.model_dump(mode="json"))
    return attempts


def load_current_frozen_attempt(
    state: Mapping[str, Any],
    output: Mapping[str, Any],
    *,
    stage: str,
    method_id: str,
    label: str,
) -> FrozenAttemptSpec:
    """Resolve the runner input owned by the current ParameterAttempt."""

    attempt = current_parameter_attempt(output, method_id=method_id, label=label)
    if attempt.frozen_attempt_ref is None:
        raise ParameterAttemptStateError(
            f"{label} current parameter attempt has no frozen_attempt_ref"
        )
    return load_frozen_attempt(
        state,
        attempt.frozen_attempt_ref,
        expected_stage=stage,
        expected_method_name=method_id,
    )
