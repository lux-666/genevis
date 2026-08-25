"""Shared parsing for immutable review-request candidate collections."""

from __future__ import annotations

from typing import Any, Mapping, cast

from app.control.errors import ResumeValidationError


def candidate_ids_from_review_request_payload(payload: Any) -> set[str]:
    """Extract candidate IDs committed by an immutable review request."""

    if not isinstance(payload, Mapping):
        raise ResumeValidationError("review request payload is invalid")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ResumeValidationError("review request candidate collection is invalid")
    return {
        cast(str, candidate.get("candidate_id"))
        for candidate in candidates
        if isinstance(candidate, Mapping)
        and isinstance(candidate.get("candidate_id"), str)
    }
