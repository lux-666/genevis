"""Shared validation and persistence for parameter-approval gates.

Every stage that uses the ``parameter_approval`` gate runs the same seven
verification steps before writing a decision.  This module owns those shared
steps; stage-specific attempt-chain checks and cursor state updates remain in
each stage's ``parameters/workflow.py``.

Functions that were previously private to ``lifecycle.py`` —
``_load_verified_interrupt_request`` and ``_ensure_decision_allowed_by_request``
— are published here so both the lifecycle control layer and the stage workflow
nodes share one implementation.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.control.errors import ResumeValidationError
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
)
from app.schemas.artifacts import ArtifactRef, ParameterRecommendationCursor
from app.schemas.parameter_recommendation import ParameterRecommendationManifestV3
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir
from app.storage.ledger import Ledger

# The four bindings that every parameter_approval request must carry.
PARAMETER_APPROVAL_BINDINGS: tuple[str, ...] = (
    "attempt_id",
    "task",
    "previous_attempt_id",
    "recommendation_hash",
)


# ---------------------------------------------------------------------------
# Moved from lifecycle._load_verified_interrupt_request
# ---------------------------------------------------------------------------


def load_verified_interrupt_request(
    state: Mapping[str, Any],
    interrupt: Mapping[str, Any],
) -> tuple[ArtifactRef, dict[str, Any]]:
    """Load a review-request artifact and verify its identity against the interrupt.

    Checks: sha256 == request_hash, review_request_id, stage, gate_id, bindings.
    Returns ``(request_ref, request_record)`` on success; raises
    :exc:`~app.control.lifecycle.ResumeValidationError` on any mismatch.
    """

    data_dir = state.get("data_dir")
    session_id = state.get("session_id")
    if not data_dir or not session_id:
        raise ResumeValidationError(
            "review request verification requires state data_dir and session_id"
        )

    request_ref_value = interrupt.get("request_ref")
    try:
        ref = ArtifactRef.model_validate(request_ref_value)
    except Exception as exc:
        raise ResumeValidationError("active_interrupt.request_ref is invalid") from exc

    request_hash = str(interrupt.get("request_hash") or "")
    if ref.sha256 != request_hash:
        raise ResumeValidationError(
            "active_interrupt.request_hash does not match request_ref.sha256"
        )

    try:
        record = load_review_request(data_dir, ref, session_id=str(session_id))
    except ReviewPersistenceError as exc:
        raise ResumeValidationError(
            f"review request verification failed: {exc}"
        ) from exc

    request_id = str(interrupt.get("review_request_id") or "")
    if record.get("review_request_id") != request_id:
        raise ResumeValidationError(
            "review request artifact ID does not match active_interrupt.review_request_id"
        )
    if record.get("stage") != interrupt.get("stage"):
        raise ResumeValidationError(
            "review request artifact stage does not match active_interrupt.stage"
        )
    if record.get("gate_id") != interrupt.get("gate_id"):
        raise ResumeValidationError(
            "review request artifact gate_id does not match active_interrupt.gate_id"
        )

    bindings = record.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise ResumeValidationError("review request artifact bindings are invalid")
    for key in PARAMETER_APPROVAL_BINDINGS:
        record_has = key in bindings
        interrupt_has = key in interrupt
        if record_has != interrupt_has or (
            record_has and bindings.get(key) != interrupt.get(key)
        ):
            raise ResumeValidationError(
                f"review request binding {key!r} does not match active_interrupt"
            )

    return ref, record


# ---------------------------------------------------------------------------
# Moved from lifecycle._ensure_decision_allowed_by_request
# ---------------------------------------------------------------------------


def ensure_decision_allowed(
    request_record: dict[str, Any],
    request: ResumeReviewRequest,
) -> None:
    """Raise if ``request.decision`` is not in the request's ``decisions_allowed``."""

    payload = request_record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("review request artifact payload is invalid")
    allowed = payload.get("decisions_allowed")
    if not isinstance(allowed, list) or not all(
        isinstance(decision, str) for decision in allowed
    ):
        raise ResumeValidationError(
            "review request artifact does not contain a valid decisions_allowed list"
        )
    if request.decision not in allowed:
        raise ResumeValidationError(
            f"decision {request.decision!r} is not allowed by the bound review request"
        )


# ---------------------------------------------------------------------------
# New: parameter_approval-specific helpers
# ---------------------------------------------------------------------------


def validate_parameter_approval_bindings(
    interrupt: Mapping[str, Any],
    request: ResumeReviewRequest,
) -> None:
    """Check that all four required bindings are present and not stale,
    and that the request's review_request_id and review_request_hash match
    the interrupt.

    Raises :exc:`~app.control.errors.ResumeValidationError` if any binding
    is missing from ``request.metadata`` or disagrees with ``interrupt``.
    """

    request_id = interrupt.get("review_request_id")
    if request.review_request_id != request_id:
        raise ResumeValidationError(
            "parameter approval decision request identity is stale"
        )
    request_hash = interrupt.get("request_hash")
    if request.review_request_hash != request_hash:
        raise ResumeValidationError(
            "parameter approval decision request hash is stale"
        )
    missing = [k for k in PARAMETER_APPROVAL_BINDINGS if k not in request.metadata]
    if missing:
        raise ResumeValidationError(
            f"parameter approval metadata missing bindings: {missing}"
        )
    for key in PARAMETER_APPROVAL_BINDINGS:
        if request.metadata.get(key) != interrupt.get(key):
            raise ResumeValidationError(
                f"stale parameter approval binding: {key}"
            )


def validate_parameter_recommendation_manifest(
    cursor: ParameterRecommendationCursor,
    manifest: ParameterRecommendationManifestV3,
) -> None:
    """Assert the six fields that tie a pending manifest to its cursor.

    Raises :exc:`~app.control.errors.ResumeValidationError` on any mismatch.
    """

    if (
        manifest.status != "pending_approval"
        or manifest.recommendation_id != cursor.recommendation_id
        or manifest.method_id != cursor.method_id
        or manifest.task != cursor.task
        or manifest.previous_attempt_id != cursor.previous_attempt_id
        or manifest.previous_attempt_seal_hash != cursor.previous_attempt_seal_hash
    ):
        raise ResumeValidationError(
            "parameter recommendation cursor does not match its manifest"
        )


def persist_parameter_approval_decision(
    state: TopLevelState,
    *,
    stage: str,
    request_id: str,
    request_ref: ArtifactRef,
    request: ResumeReviewRequest,
    manifest_ref: ArtifactRef,
    method_name: str,
    artifact_type: str,
) -> ArtifactRef:
    """Write the immutable approval decision and append a ledger row.

    Returns the published :class:`~app.schemas.artifacts.ArtifactRef`.
    Raises :exc:`~app.control.lifecycle.ResumeValidationError` on persistence
    failure.
    """

    try:
        decision_ref = persist_review_decision(
            absolute_data_dir(state),
            session_id=state["session_id"],
            review_request_id=request_id,
            stage=stage,
            gate_id="parameter_approval",
            request_hash=request_ref.sha256,
            request_ref=request_ref,
            decision={
                "schema_version": 2,
                "decision": request.decision,
                "decided_by": request.decided_by,
                "notes": list(request.notes),
                "bindings": {
                    key: request.metadata[key]
                    for key in PARAMETER_APPROVAL_BINDINGS
                },
                "request_hash": request_ref.sha256,
                "validated_recommendation_manifest_ref": manifest_ref.model_dump(
                    mode="json"
                ),
                "validated_recommendation_hash": manifest_ref.sha256,
            },
            method_name=method_name,
            artifact_type=artifact_type,
        )
    except (OSError, ValueError, ReviewPersistenceError) as exc:
        raise ResumeValidationError(
            f"{stage} parameter approval decision could not be persisted"
        ) from exc

    Ledger(absolute_data_dir(state)).record_review_decision(
        state["session_id"],
        stage,
        gate_id="parameter_approval",
        decision=request.decision,
        notes=list(request.notes),
        artifact_ref=decision_ref.path_ref,
        metadata={key: request.metadata[key] for key in PARAMETER_APPROVAL_BINDINGS},
        decided_by=request.decided_by,
        review_request_id=request_id,
    )
    return decision_ref
