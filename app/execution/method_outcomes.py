"""Typed method dispositions and their shared ledger settlement boundary."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from langchain_core.runnables import RunnableLambda

from app.control.workflow_plan import current_workflow_step, current_workflow_step_id
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base import absolute_data_dir
from app.storage.ledger import Ledger

MethodDisposition: TypeAlias = Literal["not_started", "skipped", "completed", "failed"]
_METHOD_DISPOSITIONS = frozenset({"not_started", "skipped", "completed", "failed"})


@dataclass(frozen=True, slots=True)
class MethodOutcome:
    """One immutable disposition for a method in the active occurrence."""

    method_name: str
    disposition: MethodDisposition
    reason: str | None = None
    refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.method_name, str) or not self.method_name.strip():
            raise ValueError("method outcome requires a non-empty method_name")
        if self.disposition not in _METHOD_DISPOSITIONS:
            raise ValueError(
                "method outcome disposition must be one of: "
                "not_started, skipped, completed, failed"
            )
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("method outcome reason must be a non-empty string or None")
        if self.disposition in {"skipped", "failed"} and self.reason is None:
            raise ValueError(f"method outcome {self.disposition!r} requires a reason")
        if not isinstance(self.refs, tuple):
            raise TypeError("method outcome refs must be an immutable tuple")
        if not all(isinstance(ref, ArtifactRef) for ref in self.refs):
            raise TypeError("method outcome refs must contain ArtifactRef values")


OutcomePolicy: TypeAlias = Callable[
    [TopLevelState], MethodOutcome | Sequence[MethodOutcome] | None
]


def method_is_selected(state: TopLevelState, method_name: str) -> bool:
    """Return whether a method is rostered for the active workflow occurrence."""

    step = current_workflow_step(state)
    roster = step.get("selected_method_names") if step is not None else None
    return isinstance(roster, list) and method_name in roster


def has_execution_result(output: Mapping[str, object], method_name: str) -> bool:
    """Return whether a method has crossed the execution boundary."""

    attempts = output.get("execution_attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        attempt.get("method_id") == method_name
        and attempt.get("status") in {"executed", "execution_failed"}
        for attempt in attempts
        if isinstance(attempt, Mapping)
    )


def settle_method_outcomes(
    state: TopLevelState,
    *,
    stage: str,
    outcomes: MethodOutcome | Iterable[MethodOutcome] | None,
    expected_method_names: Iterable[str] | None = None,
) -> StateUpdate:
    """Persist policy-owned method dispositions for the active occurrence.

    This is intentionally the sole execution-layer writer of
    ``method_disposition_recorded``.  It validates the complete batch before
    writing anything, while :class:`Ledger` owns exact replay and conflict
    detection under its append lock.
    """

    settled = _normalize_outcomes(outcomes)
    if not settled:
        return {}

    workflow_step_id = current_workflow_step_id(state, expected_stage=stage)
    expected = _normalize_expected_method_names(expected_method_names)
    step = current_workflow_step(state)
    if step is None:  # guarded by current_workflow_step_id, retained for typing.
        raise ValueError("method outcomes require an active workflow occurrence")
    raw_roster = step.get("selected_method_names")
    roster = None if raw_roster is None else frozenset(raw_roster)
    seen_method_names: set[str] = set()
    for outcome in settled:
        if not isinstance(outcome, MethodOutcome):
            raise TypeError("method outcome policy must return MethodOutcome values")
        if outcome.method_name in seen_method_names:
            raise ValueError(
                "duplicate method outcome for "
                f"{outcome.method_name!r} in one occurrence"
            )
        seen_method_names.add(outcome.method_name)
        if roster is not None and outcome.method_name not in roster:
            raise ValueError(
                f"method outcome {outcome.method_name!r} is not selected for "
                f"workflow occurrence {workflow_step_id!r}"
            )
        if expected is not None and outcome.method_name not in expected:
            raise ValueError(
                f"method outcome {outcome.method_name!r} is not registered for "
                f"stage {stage!r}"
            )

    ledger = Ledger(absolute_data_dir(state))
    for outcome in settled:
        ledger.record_method_disposition(
            state["session_id"],
            stage,
            workflow_step_id,
            outcome.method_name,
            outcome.disposition,
            reason=outcome.reason,
            refs=outcome.refs,
        )
    return {}


def method_outcome_settlement_node(
    *,
    stage: str,
    method_name: str | None = None,
    outcome_policy: OutcomePolicy | None,
) -> RunnableLambda[TopLevelState, dict[str, object]]:
    """Build a reusable no-state-write node for a method outcome policy.

    A ``None`` policy or a policy returning ``None`` deliberately performs no
    settlement.  This lets retry-continuation paths reach the common graph
    exit without prematurely recording a terminal method disposition.
    """

    def settle(state: TopLevelState) -> dict[str, object]:
        if outcome_policy is None:
            return {}
        return dict(
            settle_method_outcomes(
                state,
                stage=stage,
                outcomes=outcome_policy(state),
                expected_method_names=(
                    (method_name,) if method_name is not None else None
                ),
            )
        )

    return RunnableLambda(settle)


def _normalize_outcomes(
    outcomes: MethodOutcome | Iterable[MethodOutcome] | None,
) -> tuple[MethodOutcome, ...]:
    if outcomes is None:
        return ()
    if isinstance(outcomes, MethodOutcome):
        return (outcomes,)
    try:
        return tuple(outcomes)
    except TypeError as exc:
        raise TypeError(
            "method outcome policy must return MethodOutcome, an iterable of "
            "MethodOutcome values, or None"
        ) from exc


def _normalize_expected_method_names(
    names: Iterable[str] | None,
) -> frozenset[str] | None:
    if names is None:
        return None
    expected = frozenset(names)
    if not expected or any(
        not isinstance(name, str) or not name.strip() for name in expected
    ):
        raise ValueError("expected method names must be non-empty strings")
    return expected
