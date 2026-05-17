"""Pure review policy evaluation."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel

from app.review.base import ReviewPayload

PolicyAction = Literal[
    "BLOCK_INDEFINITE",
    "BLOCK_WITH_TIMEOUT",
    "AUTO_DECIDE_NOW",
    "BLOCK_PENDING_CONFIRM",
]
PolicyMode = Literal["strict", "auto"]


class PolicyOutcome(BaseModel):
    """Decision made by review policy before a gate blocks."""

    action: PolicyAction
    mode: PolicyMode
    stage: str
    auto_decidable: bool
    max_blocked_seconds: int | None = None
    reason: str


def evaluate(
    stage: str,
    request: ReviewPayload,
    effective_policy: Mapping[str, Any] | None = None,
) -> PolicyOutcome:
    policy = dict(effective_policy or {})
    mode: PolicyMode = "auto" if policy.get("mode") == "auto" else "strict"
    max_blocked_seconds = policy.get("max_blocked_seconds")
    if max_blocked_seconds is not None:
        max_blocked_seconds = int(max_blocked_seconds)

    if mode == "auto":
        if request.auto_decidable:
            return PolicyOutcome(
                action="AUTO_DECIDE_NOW",
                mode=mode,
                stage=stage,
                auto_decidable=True,
                max_blocked_seconds=max_blocked_seconds,
                reason="auto_policy_auto_decidable",
            )
        return PolicyOutcome(
            action="BLOCK_PENDING_CONFIRM",
            mode=mode,
            stage=stage,
            auto_decidable=False,
            max_blocked_seconds=max_blocked_seconds,
            reason="auto_policy_requires_human_confirmation",
        )

    if max_blocked_seconds is not None:
        return PolicyOutcome(
            action="BLOCK_WITH_TIMEOUT",
            mode=mode,
            stage=stage,
            auto_decidable=request.auto_decidable,
            max_blocked_seconds=max_blocked_seconds,
            reason="strict_policy_timeout_configured",
        )
    return PolicyOutcome(
        action="BLOCK_INDEFINITE",
        mode=mode,
        stage=stage,
        auto_decidable=request.auto_decidable,
        reason="strict_policy_no_timeout",
    )
