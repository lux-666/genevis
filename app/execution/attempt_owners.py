"""Compact cursor ownership for the shared method execution lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from app.execution.evidence import (
    ExecutionEvidence,
    load_execution_evidence,
    load_frozen_attempt,
)
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
    update_current_parameter_attempt,
)
from app.schemas.artifacts import ArtifactRef, ParameterAttempt
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import MethodExecutionAttempt

if TYPE_CHECKING:
    from app.execution.lifecycle import MethodExecutionLifecycle
    from app.schemas.state import TopLevelState


DIRECT_ATTEMPTS_KEY = "execution_attempts"


@dataclass(frozen=True, kw_only=True)
class MethodExecution:
    """Verified execution evidence paired with its immutable runner input."""

    attempt: FrozenAttemptSpec
    execution_ref: ArtifactRef
    evidence: ExecutionEvidence


class AttemptOwner(Protocol):
    """Own the compact cursor that binds a method run to durable evidence."""

    def record_frozen_attempts(
        self,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
        attempts: Sequence[FrozenAttemptSpec],
        frozen_refs: Sequence[ArtifactRef],
        *,
        owner_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind newly persisted frozen attempts to this owner's cursor."""

    def load_frozen_attempts(
        self,
        state: TopLevelState,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
    ) -> list[FrozenAttemptSpec]:
        """Load only attempts whose owner cursor is ready to execute."""

    def record_execution(
        self,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
        attempt: FrozenAttemptSpec,
        execution_ref: ArtifactRef,
        *,
        success: bool,
    ) -> dict[str, Any]:
        """Advance one owner cursor after its execution evidence is durable."""

    def load_executions(
        self,
        state: TopLevelState,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
    ) -> list[MethodExecution]:
        """Load owner-completed attempts and their matching evidence."""


class DirectAttemptOwner:
    """Owner for methods whose recovery cursor is an execution-attempt batch."""

    def record_frozen_attempts(
        self,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
        attempts: Sequence[FrozenAttemptSpec],
        frozen_refs: Sequence[ArtifactRef],
        *,
        owner_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if owner_metadata:
            raise ValueError(
                f"{lifecycle.name} direct attempts cannot bind owner metadata"
            )
        return {
            DIRECT_ATTEMPTS_KEY: [
                MethodExecutionAttempt(
                    attempt_id=attempt.attempt_id,
                    method_id=attempt.method_name,
                    status="frozen",
                    frozen_attempt_ref=frozen_ref,
                ).model_dump(mode="json")
                for attempt, frozen_ref in zip(attempts, frozen_refs, strict=True)
            ]
        }

    def load_frozen_attempts(
        self,
        state: TopLevelState,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
    ) -> list[FrozenAttemptSpec]:
        cursors = _direct_attempt_cursors(output, lifecycle)
        if any(cursor.status != "frozen" for cursor in cursors):
            raise ValueError(f"{lifecycle.name} execution attempt cursor is not frozen")
        return [
            _load_frozen_attempt(state, cursor.frozen_attempt_ref, lifecycle, cursor)
            for cursor in cursors
        ]

    def record_execution(
        self,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
        attempt: FrozenAttemptSpec,
        execution_ref: ArtifactRef,
        *,
        success: bool,
    ) -> dict[str, Any]:
        updated: list[dict[str, Any]] = []
        matched = False
        for cursor in _direct_attempt_cursors(output, lifecycle):
            if cursor.attempt_id != attempt.attempt_id:
                updated.append(cursor.model_dump(mode="json"))
                continue
            if cursor.status != "frozen":
                raise ValueError(
                    f"{lifecycle.name} execution attempt cursor is not frozen"
                )
            updated.append(
                MethodExecutionAttempt(
                    **cursor.model_dump(
                        mode="python", exclude={"status", "execution_ref"}
                    ),
                    status="executed" if success else "execution_failed",
                    execution_ref=execution_ref,
                ).model_dump(mode="json")
            )
            matched = True
        if not matched:
            raise ValueError(
                f"{lifecycle.name} execution attempt has no matching owner cursor"
            )
        return {DIRECT_ATTEMPTS_KEY: updated}

    def load_executions(
        self,
        state: TopLevelState,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
    ) -> list[MethodExecution]:
        cursors = _direct_attempt_cursors(output, lifecycle)
        if any(
            cursor.status not in {"executed", "execution_failed"} for cursor in cursors
        ):
            raise ValueError(
                f"{lifecycle.name} execution attempt cursor is not completed"
            )
        executions: list[MethodExecution] = []
        for cursor in cursors:
            if cursor.execution_ref is None:
                raise ValueError(f"{lifecycle.name} attempt has no execution_ref")
            attempt = _load_frozen_attempt(
                state, cursor.frozen_attempt_ref, lifecycle, cursor
            )
            evidence = load_execution_evidence(
                state,
                cursor.execution_ref,
                expected_stage=lifecycle.stage,
                expected_attempt_id=cursor.attempt_id,
                expected_method_name=lifecycle.method_name,
            )
            executions.append(
                MethodExecution(
                    attempt=attempt,
                    execution_ref=cursor.execution_ref,
                    evidence=evidence,
                )
            )
        return executions


class ParameterAttemptOwner:
    """Owner for the current approved parameter recommendation cursor."""

    def record_frozen_attempts(
        self,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
        attempts: Sequence[FrozenAttemptSpec],
        frozen_refs: Sequence[ArtifactRef],
        *,
        owner_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if len(attempts) != 1 or len(frozen_refs) != 1:
            raise ParameterAttemptStateError(
                f"{lifecycle.name} parameter owner requires exactly one frozen attempt"
            )
        if owner_metadata and "frozen_attempt_ref" in owner_metadata:
            raise ParameterAttemptStateError(
                f"{lifecycle.name} frozen attempt ref is owned by the shared lifecycle"
            )
        current = current_parameter_attempt(
            output, method_id=lifecycle.method_name, label=lifecycle.name
        )
        if current.status != "recommended":
            raise ParameterAttemptStateError(
                f"{lifecycle.name} current parameter attempt is not recommended"
            )
        _validate_parameter_attempt_identity(attempts[0], current, lifecycle)
        updates = dict(owner_metadata or {})
        return {
            "parameter_attempts": update_current_parameter_attempt(
                output,
                method_id=lifecycle.method_name,
                label=lifecycle.name,
                status="frozen",
                frozen_attempt_ref=frozen_refs[0].model_dump(mode="json"),
                **updates,
            )
        }

    def load_frozen_attempts(
        self,
        state: TopLevelState,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
    ) -> list[FrozenAttemptSpec]:
        current = current_parameter_attempt(
            output, method_id=lifecycle.method_name, label=lifecycle.name
        )
        if current.status != "frozen":
            raise ParameterAttemptStateError(
                f"{lifecycle.name} current parameter attempt is not frozen"
            )
        _validate_parameter_attempt_ready_for_execution(current, lifecycle)
        if current.frozen_attempt_ref is None:
            raise ParameterAttemptStateError(
                f"{lifecycle.name} current parameter attempt has no frozen_attempt_ref"
            )
        attempt = load_frozen_attempt(
            state,
            current.frozen_attempt_ref,
            expected_stage=lifecycle.stage,
            expected_method_name=lifecycle.method_name,
        )
        _validate_parameter_attempt_identity(attempt, current, lifecycle)
        return [attempt]

    def record_execution(
        self,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
        attempt: FrozenAttemptSpec,
        execution_ref: ArtifactRef,
        *,
        success: bool,
    ) -> dict[str, Any]:
        current = current_parameter_attempt(
            output, method_id=lifecycle.method_name, label=lifecycle.name
        )
        if current.status != "frozen":
            raise ParameterAttemptStateError(
                f"{lifecycle.name} current parameter attempt is not frozen"
            )
        _validate_parameter_attempt_ready_for_execution(current, lifecycle)
        _validate_parameter_attempt_identity(attempt, current, lifecycle)
        return {
            "parameter_attempts": update_current_parameter_attempt(
                output,
                method_id=lifecycle.method_name,
                label=lifecycle.name,
                status="executed" if success else "execution_failed",
                execution_ref=execution_ref.model_dump(mode="json"),
            )
        }

    def load_executions(
        self,
        state: TopLevelState,
        output: Mapping[str, Any],
        lifecycle: MethodExecutionLifecycle,
    ) -> list[MethodExecution]:
        current = current_parameter_attempt(
            output, method_id=lifecycle.method_name, label=lifecycle.name
        )
        if current.status not in {"executed", "execution_failed"}:
            raise ParameterAttemptStateError(
                f"{lifecycle.name} current parameter attempt is not completed"
            )
        if current.frozen_attempt_ref is None or current.execution_ref is None:
            raise ParameterAttemptStateError(
                f"{lifecycle.name} current parameter attempt lacks execution evidence"
            )
        attempt = load_frozen_attempt(
            state,
            current.frozen_attempt_ref,
            expected_stage=lifecycle.stage,
            expected_method_name=lifecycle.method_name,
        )
        _validate_parameter_attempt_identity(attempt, current, lifecycle)
        evidence = load_execution_evidence(
            state,
            current.execution_ref,
            expected_stage=lifecycle.stage,
            expected_attempt_id=attempt.attempt_id,
            expected_method_name=lifecycle.method_name,
        )
        return [
            MethodExecution(
                attempt=attempt,
                execution_ref=current.execution_ref,
                evidence=evidence,
            )
        ]


def _validate_parameter_attempt_identity(
    attempt: FrozenAttemptSpec,
    current: ParameterAttempt,
    lifecycle: MethodExecutionLifecycle,
) -> None:
    parameter_attempt_id = (attempt.metadata or {}).get("parameter_attempt_id")
    if type(parameter_attempt_id) is not int or parameter_attempt_id < 0:
        raise ParameterAttemptStateError(
            f"{lifecycle.name} frozen attempt has no explicit parameter_attempt_id"
        )
    if parameter_attempt_id != current.attempt_id:
        raise ParameterAttemptStateError(
            f"{lifecycle.name} frozen attempt parameter_attempt_id "
            f"{parameter_attempt_id} does not match current cursor "
            f"{current.attempt_id}"
        )


def _validate_parameter_attempt_ready_for_execution(
    current: ParameterAttempt,
    lifecycle: MethodExecutionLifecycle,
) -> None:
    if current.execution_ref is not None or current.review_ref is not None:
        raise ParameterAttemptStateError(
            f"{lifecycle.name} current frozen parameter attempt already has "
            "completion evidence"
        )
    if (
        current.seal_hash is not None
        or current.sealed_at is not None
        or current.attempt_manifest_ref is not None
    ):
        raise ParameterAttemptStateError(
            f"{lifecycle.name} current frozen parameter attempt is sealed"
        )


def _direct_attempt_cursors(
    output: Mapping[str, Any],
    lifecycle: MethodExecutionLifecycle,
) -> list[MethodExecutionAttempt]:
    raw = output.get(DIRECT_ATTEMPTS_KEY)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{lifecycle.name} has no direct execution attempts")
    cursors = [MethodExecutionAttempt.model_validate(item) for item in raw]
    if any(cursor.method_id != lifecycle.method_name for cursor in cursors):
        raise ValueError(
            f"{lifecycle.name} attempt cursor has the wrong method identity"
        )
    if len({cursor.attempt_id for cursor in cursors}) != len(cursors):
        raise ValueError(
            f"{lifecycle.name} attempt cursors contain duplicate identities"
        )
    return cursors


def _load_frozen_attempt(
    state: TopLevelState,
    frozen_ref: ArtifactRef,
    lifecycle: MethodExecutionLifecycle,
    cursor: MethodExecutionAttempt,
) -> FrozenAttemptSpec:
    attempt = load_frozen_attempt(
        state,
        frozen_ref,
        expected_stage=lifecycle.stage,
        expected_method_name=lifecycle.method_name,
    )
    if attempt.attempt_id != cursor.attempt_id:
        raise ValueError(f"{lifecycle.name} frozen attempt does not match its cursor")
    return attempt
