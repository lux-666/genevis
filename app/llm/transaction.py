"""Immutable recommendation exchange, independent of stage-specific semantics."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from app.schemas.artifacts import ArtifactRef

Decision = TypeVar("Decision")


def recover_or_invoke_response(
    *,
    recover: Callable[[], tuple[ArtifactRef, dict[str, Any]] | None],
    invoke: Callable[[], Decision],
    restore: Callable[[Mapping[str, Any]], Decision],
    publish: Callable[[Decision], ArtifactRef],
) -> tuple[ArtifactRef, Decision]:
    """Recover exact committed bytes before any nondeterministic model call.

    Publishing remains the caller's immutable storage operation. Once published,
    a stale checkpoint re-parses that response without calling the model again.
    Request validation, predecessor binding and biological policy stay with the
    stage, as do invalid-response handling and approval.
    """
    recovered = recover()
    if recovered is not None:
        ref, record = recovered
        return ref, restore(record)
    decision = invoke()
    return publish(decision), decision
