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

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.control.errors import ResumeValidationError
from app.control.workflow_plan import current_workflow_step_id
from app.review.base import ResumeReviewRequest
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_decision,
)
from app.schemas.artifacts import (
    ArtifactRef,
    ParameterAttempt,
    ParameterAttemptTask,
    ParameterRecommendationCursor,
)
from app.schemas.parameter_recommendation import ParameterRecommendationManifestV3
from app.schemas.state import TopLevelState
from app.stages.base import absolute_data_dir

# The four bindings that every parameter_approval request must carry.
PARAMETER_APPROVAL_BINDINGS: tuple[str, ...] = (
    "attempt_id",
    "task",
    "previous_attempt_id",
    "recommendation_hash",
)


ParameterApprovalDecision = Literal["approve_parameters", "reject_parameters"]


class ParameterApprovalAutomaticResolutionV1(BaseModel):
    """Validated audit facts for one automatic persisted-recommendation approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["foreground_countdown", "policy"]
    configured_seconds: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    outcome: Literal["approve_persisted_recommendation"]

    @field_validator("configured_seconds", mode="before")
    @classmethod
    def reject_boolean_countdown_seconds(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("configured_seconds must be an integer")
        return value

    @field_validator("elapsed_seconds", mode="before")
    @classmethod
    def reject_boolean_elapsed_seconds(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("elapsed_seconds must be numeric")
        return value

    @model_validator(mode="after")
    def validate_elapsed_seconds(self) -> "ParameterApprovalAutomaticResolutionV1":
        if self.elapsed_seconds < self.configured_seconds:
            raise ValueError("elapsed_seconds must not precede configured_seconds")
        return self


class ParameterApprovalMetadataV1(BaseModel):
    """Strict request metadata accepted by every parameter approval gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: int = Field(ge=0)
    task: ParameterAttemptTask
    previous_attempt_id: int | None = Field(default=None, ge=0)
    recommendation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["foreground_countdown", "policy"] | None = None
    configured_seconds: int | None = Field(default=None, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    automatic_resolution: ParameterApprovalAutomaticResolutionV1 | None = None

    @field_validator(
        "attempt_id", "previous_attempt_id", "configured_seconds", mode="before"
    )
    @classmethod
    def reject_boolean_integer_fields(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("parameter approval integer metadata must be an integer")
        return value

    @field_validator("elapsed_seconds", mode="before")
    @classmethod
    def reject_boolean_elapsed_field(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("elapsed_seconds must be numeric")
        return value

    @model_validator(mode="after")
    def validate_countdown_metadata(self) -> "ParameterApprovalMetadataV1":
        countdown_values = (
            self.source,
            self.configured_seconds,
            self.elapsed_seconds,
        )
        if any(value is not None for value in countdown_values) and not all(
            value is not None for value in countdown_values
        ):
            raise ValueError("foreground countdown metadata is incomplete")
        if (
            self.configured_seconds is not None
            and self.elapsed_seconds is not None
            and self.elapsed_seconds < self.configured_seconds
        ):
            raise ValueError("elapsed_seconds must not precede configured_seconds")
        if self.automatic_resolution is not None:
            if (
                self.source != self.automatic_resolution.source
                or self.configured_seconds
                != self.automatic_resolution.configured_seconds
                or self.elapsed_seconds != self.automatic_resolution.elapsed_seconds
            ):
                raise ValueError(
                    "automatic_resolution must match foreground countdown metadata"
                )
        return self


class ParameterApprovalEffectV1(BaseModel):
    """Typed, replay-safe result of preparing a parameter-approval decision.

    The effect carries every artifact identity and compact cursor needed by a
    stage's pure checkpoint projection.  It deliberately never embeds a
    ``StateUpdate``: each stage owns its own state-shape projection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision: ParameterApprovalDecision
    decision_ref: ArtifactRef
    manifest_ref: ArtifactRef
    cursor: ParameterRecommendationCursor
    approved_attempt: ParameterAttempt | None = None
    rejection_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_projection_inputs(self) -> "ParameterApprovalEffectV1":
        if self.cursor.status != "pending_approval":
            raise ValueError("parameter approval effect cursor must be pending")
        if self.cursor.manifest_ref != self.manifest_ref:
            raise ValueError("parameter approval effect cursor manifest mismatch")
        if self.decision == "reject_parameters":
            if self.approved_attempt is not None:
                raise ValueError("rejected parameter effect cannot contain an attempt")
            return self
        attempt = self.approved_attempt
        if attempt is None:
            raise ValueError("approved parameter effect requires an attempt")
        if (
            attempt.attempt_id != self.cursor.recommendation_id
            or attempt.method_id != self.cursor.method_id
            or attempt.task != self.cursor.task
            or attempt.previous_attempt_id != self.cursor.previous_attempt_id
            or attempt.previous_attempt_seal_hash
            != self.cursor.previous_attempt_seal_hash
            or attempt.recommendation_manifest_ref != self.manifest_ref
            or attempt.approval_decision_ref != self.decision_ref
        ):
            raise ValueError("approved parameter effect attempt does not match cursor")
        return self


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
            "review request artifact ID does not match "
            "active_interrupt.review_request_id"
        )
    if record.get("stage") != interrupt.get("stage"):
        raise ResumeValidationError(
            "review request artifact stage does not match active_interrupt.stage"
        )
    if record.get("workflow_step_id") != interrupt.get("workflow_step_id"):
        raise ResumeValidationError(
            "review request artifact workflow_step_id does not match "
            "active_interrupt.workflow_step_id"
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


def parameter_approval_decision(
    request: ResumeReviewRequest,
) -> ParameterApprovalDecision:
    """Validate and narrow one shared parameter-approval decision."""

    if request.decision == "approve_parameters":
        return "approve_parameters"
    if request.decision == "reject_parameters":
        return "reject_parameters"
    raise ResumeValidationError("unsupported parameter approval decision")


# ---------------------------------------------------------------------------
# New: parameter_approval-specific helpers
# ---------------------------------------------------------------------------


def validate_parameter_approval_bindings(
    interrupt: Mapping[str, Any],
    request: ResumeReviewRequest,
) -> ParameterApprovalMetadataV1:
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
        raise ResumeValidationError("parameter approval decision request hash is stale")
    try:
        metadata = ParameterApprovalMetadataV1.model_validate(request.metadata)
    except Exception as exc:
        raise ResumeValidationError("parameter approval metadata is invalid") from exc
    for key in PARAMETER_APPROVAL_BINDINGS:
        if getattr(metadata, key) != interrupt.get(key):
            raise ResumeValidationError(f"stale parameter approval binding: {key}")
    return metadata


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


def normalize_parameter_approval_timeout_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    timeout: dict[str, Any],
) -> ResumeReviewRequest:
    """Return the bound automatic request without dispatching or persisting it."""

    if request.gate_id != "parameter_approval":
        raise ResumeValidationError(
            "automatic parameter approval requires gate_id=parameter_approval"
        )
    if request.decision != "approve_parameters":
        raise ResumeValidationError(
            "automatic parameter approval can only approve persisted recommendations"
        )
    if request.decided_by != "auto_rule":
        raise ResumeValidationError(
            "automatic parameter approval requires decided_by=auto_rule"
        )

    try:
        metadata = ParameterApprovalMetadataV1.model_validate(request.metadata)
    except Exception as exc:
        raise ResumeValidationError(
            "automatic parameter approval metadata is invalid"
        ) from exc
    configured_seconds = metadata.configured_seconds
    elapsed_seconds = metadata.elapsed_seconds
    if (
        metadata.source != "foreground_countdown"
        or configured_seconds is None
        or elapsed_seconds is None
        or timeout.get("configured_seconds") != configured_seconds
        or timeout.get("expired") is not True
    ):
        raise ResumeValidationError(
            "automatic parameter approval is not bound to the foreground countdown"
        )

    automatic_resolution = {
        "source": "foreground_countdown",
        "configured_seconds": configured_seconds,
        "elapsed_seconds": elapsed_seconds,
        "outcome": "approve_persisted_recommendation",
    }
    timeout_request = request.model_copy(
        update={
            "notes": [
                *request.notes,
                "foreground countdown expired; approved persisted parameters",
            ],
            "metadata": {
                **request.metadata,
                "automatic_resolution": automatic_resolution,
            },
        }
    )
    try:
        ParameterApprovalMetadataV1.model_validate(timeout_request.metadata)
    except Exception as exc:
        raise ResumeValidationError(
            "automatic parameter approval metadata is invalid"
        ) from exc
    return timeout_request


def _parameter_approval_audit_metadata(
    request: ResumeReviewRequest,
) -> dict[str, Any]:
    """Keep decision-ledger metadata bounded to bindings and auto facts."""

    try:
        request_metadata = ParameterApprovalMetadataV1.model_validate(request.metadata)
    except Exception as exc:
        raise ResumeValidationError("parameter approval metadata is invalid") from exc
    metadata = {
        key: getattr(request_metadata, key) for key in PARAMETER_APPROVAL_BINDINGS
    }
    automatic_resolution = request_metadata.automatic_resolution
    if request.decided_by == "auto_rule":
        if automatic_resolution is None:
            raise ResumeValidationError(
                "automatic parameter approval has no countdown audit metadata"
            )
        metadata.update(
            {
                "automatic_source": automatic_resolution.source,
                "automatic_configured_seconds": automatic_resolution.configured_seconds,
                "automatic_elapsed_seconds": automatic_resolution.elapsed_seconds,
                "automatic_outcome": automatic_resolution.outcome,
            }
        )
    return metadata


def _automatic_resolution_from_audit_metadata(
    audit_metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    if "automatic_source" not in audit_metadata:
        return None
    return {
        "source": audit_metadata["automatic_source"],
        "configured_seconds": audit_metadata["automatic_configured_seconds"],
        "elapsed_seconds": audit_metadata["automatic_elapsed_seconds"],
        "outcome": audit_metadata["automatic_outcome"],
    }


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
    """Write or recover the immutable approval decision artifact.

    Returns the published :class:`~app.schemas.artifacts.ArtifactRef`.
    Raises :exc:`~app.control.lifecycle.ResumeValidationError` on persistence
    failure.
    """

    audit_metadata = _parameter_approval_audit_metadata(request)
    automatic_resolution = _automatic_resolution_from_audit_metadata(audit_metadata)
    try:
        workflow_step_id = current_workflow_step_id(state, expected_stage=stage)
        request_record = load_review_request(
            absolute_data_dir(state), request_ref, session_id=state["session_id"]
        )
        if (
            request_record.get("stage") != stage
            or request_record.get("workflow_step_id") != workflow_step_id
        ):
            raise ReviewPersistenceError(
                "parameter approval request targets another workflow step"
            )
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
                    key: audit_metadata[key] for key in PARAMETER_APPROVAL_BINDINGS
                },
                "rationale_artifact_ref": (
                    request.rationale_artifact_ref.model_dump(mode="json")
                    if request.rationale_artifact_ref is not None
                    else None
                ),
                **(
                    {"automatic_resolution": automatic_resolution}
                    if automatic_resolution is not None
                    else {}
                ),
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

    return decision_ref
