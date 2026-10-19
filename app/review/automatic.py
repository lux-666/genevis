"""Explicit zero-wait actions; quality observations never veto valid products.

These rules select an action, not an execution result. The ordinary stage
validators and durable application transaction still verify and adopt it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.control.errors import ResumeValidationError
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ArtifactRef
from app.storage.artifacts import resolve_artifact_ref


def zero_wait_selection(
    policy: str, record: Mapping[str, Any], data_dir: str | Path, *, session_id: str
) -> dict[str, Any]:
    """Select only from a verified request, never from menu ordering."""
    payload = record["payload"]
    candidates = payload.get("candidates") or []
    allowed = payload.get("decisions_allowed") or []
    selected: dict[str, Any]
    if policy == "continue":
        selected = {"decision": "continue"}
    elif policy == "adopt_result":
        # These gates are opened only after their single product is collected.
        if not candidates:
            raise ResumeValidationError("result adoption request has no product")
        selected = {"decision": "accept_candidate"}
    elif policy == "finish_review":
        # resolve_assembly_candidates puts the current delivered assembly first;
        # subsequent entries are comparison baselines, not preferred outputs.
        if not candidates or not candidates[0].get("candidate_id"):
            raise ResumeValidationError("final review has no delivered assembly")
        if any(s.get("status") == "failed" for s in payload.get("signals", [])):
            raise ResumeValidationError("final evaluation contains a failed tool")
        selected = {
            "decision": "accept",
            "candidate_id": candidates[0]["candidate_id"],
            "method_name": candidates[0].get("method_name"),
        }
    elif policy == "adopt_patches":
        ids = sorted({c["candidate_id"] for c in candidates if c.get("candidate_id")})
        selected = (
            {"decision": "accept_selected_candidates", "candidate_ids": ids}
            if ids
            else {"decision": "continue_without_patch"}
        )
    elif policy == "finishing":
        context = payload["candidate_comparison_context"]
        recommendation_path = resolve_artifact_ref(
            data_dir,
            session_id,
            ArtifactRef.model_validate(context["recommendation_ref"]),
        )
        response = json.loads(recommendation_path.read_text())
        error = payload.get("recommendation_error") or response.get("error")
        if error:
            raise ResumeValidationError(f"parameter recommendation failed: {error}")
        if candidates:
            # Attempts are immutable, chronological, and independent of each
            # other. Adopt the latest materialized candidate, not a quality vote.
            selected = {
                "decision": "accept_candidate",
                "candidate_id": candidates[-1]["candidate_id"],
            }
        elif context.get("history_refs"):
            selected = {"decision": "keep_prior_assembly"}
        elif "follow_recommendation" in allowed:
            selected = {"decision": "follow_recommendation"}
        else:
            selected = {"decision": "keep_prior_assembly"}
    else:
        raise ResumeValidationError(f"unknown zero-wait policy: {policy}")
    if selected["decision"] not in allowed:
        raise ResumeValidationError("zero-wait action is not allowed by this request")
    return selected


def validate_zero_wait_request(
    request: ResumeReviewRequest,
    record: Mapping[str, Any],
    *,
    data_dir: str | Path,
) -> None:
    """Recheck a rule decision at the intent and stage validation boundaries."""
    from app.review.application import ForegroundReviewMetadataV1
    from app.review.gates import get_review_gate

    gate = get_review_gate(request.stage, request.gate_id)
    metadata = ForegroundReviewMetadataV1.model_validate(request.metadata)
    if (
        not gate.zero_wait_policy
        or request.decided_by != "auto_rule"
        or metadata.source != "foreground_countdown"
        or metadata.configured_seconds != 0
        or record["payload"].get("max_blocked_seconds") != 0
        or request.rationale_artifact_ref is None
        or request.rationale_artifact_ref.sha256 != request.review_request_hash
    ):
        raise ResumeValidationError(
            "automatic adoption requires a bound zero-wait rule"
        )
    expected = zero_wait_selection(
        gate.zero_wait_policy, record, data_dir, session_id=request.session_id
    )
    for key, default in (
        ("decision", None),
        ("candidate_id", None),
        ("method_name", None),
        ("candidate_ids", []),
        ("rejected_candidates", []),
    ):
        if getattr(request, key) != expected.get(key, default):
            raise ResumeValidationError(f"zero-wait action differs in {key}")


def completed_attempt_proposal(
    *, completed_attempts: int, candidate_attempts: list[int], maximum: int
) -> dict[str, Any] | None:
    """Zero-wait completion: no further model call after a completed attempt."""
    if not completed_attempts and completed_attempts < maximum:
        return None
    selected = candidate_attempts[-1] if candidate_attempts else None
    return {
        "action": "accept" if selected is not None else "keep_source",
        "selected_attempt": selected,
        "parameters": None,
        "reason": (
            "zero_wait_adopt_valid_result"
            if selected is not None
            else "zero_wait_no_new_result"
            if completed_attempts
            else "attempt_budget_exhausted"
        ),
        "evidence_gaps": [],
    }
