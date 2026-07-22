"""Shared ownership helpers for parameterized execution attempts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.schemas.artifacts import ParameterAttempt

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
    """Return the typed attempt selected by the stage cursor.

    ``method_id`` is required at method execution boundaries. Generic retry and
    routing code may omit it when the selected attempt itself is the source of
    method identity. This boundary intentionally validates only the current
    cursor and attempt; append/retry/seal/timeline owners validate history.
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
    raw = output.get("parameter_attempts")
    if not isinstance(raw, list) or not raw:
        raise ParameterAttemptStateError(f"{label} has no parameter attempt chain")
    if attempt_id >= len(raw):
        raise ParameterAttemptStateError(
            f"{label} current_parameter_attempt_id={attempt_id} has no matching attempt"
        )
    item = raw[attempt_id]
    if not isinstance(item, Mapping):
        raise ParameterAttemptStateError(
            f"{label} current parameter attempt at index {attempt_id} is not a mapping"
        )
    try:
        attempt = ParameterAttempt.model_validate(item)
    except ValidationError as exc:
        raise ParameterAttemptStateError(
            f"{label} current parameter attempt is invalid: {exc}"
        ) from exc
    if attempt.attempt_id != attempt_id:
        raise ParameterAttemptStateError(
            f"{label} current parameter attempt identity does not match cursor "
            f"{attempt_id}"
        )
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
) -> list[Any]:
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

    raw = output.get("parameter_attempts")
    if not isinstance(raw, list) or not raw:
        raise ParameterAttemptStateError(f"{label} has no parameter attempt chain")
    # Preserve historical entries byte-for-byte. Their chain semantics belong
    # to append/retry/seal/timeline boundaries, not execution cursor validation.
    attempts = list(raw)
    attempts[attempt.attempt_id] = updated
    return attempts
