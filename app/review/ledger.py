"""Verify review business evidence before committing its causal Ledger facts."""

from __future__ import annotations

from typing import Any, Mapping

from app.review.application import ReviewApplicationIdentity
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


def record_review_decision(
    ledger: Ledger,
    session_id: str,
    *,
    application_ref: Any,
    expected_identity: "ReviewApplicationIdentity",
) -> dict[str, Any]:
    """Record the committed decision from one verified application marker."""

    from app.review.application import (
        load_verified_review_application,
        load_verified_review_intent,
    )

    ledger.paths.require_session_id(session_id)
    if expected_identity.session_id != session_id:
        raise ValueError("review application identity does not match session_id")
    verified = load_verified_review_application(
        ledger.paths.run_dir,
        application_ref,
        expected_identity=expected_identity,
    )
    application = verified.application
    intent = load_verified_review_intent(
        ledger.paths.run_dir,
        application.intent_ref,
        expected_identity=expected_identity,
    )
    request = intent.request
    refs: list[dict[str, Any]] = [
        {
            **application.decision_ref.model_dump(mode="json"),
            "role": "artifact_ref",
        },
        {
            **application.intent_ref.model_dump(mode="json"),
            "role": "intent_ref",
        },
        {
            **verified.ref.model_dump(mode="json"),
            "role": "application_ref",
        },
        *(
            {
                **required_ref.model_dump(mode="json"),
                "role": "application_required_ref",
            }
            for required_ref in application.required_refs
        ),
    ]
    if request.rationale_artifact_ref:
        refs.append(
            ledger._path_ref(
                "rationale_artifact_ref",
                request.rationale_artifact_ref,
            )
        )
    payload = {
        "review_request_id": application.review_request_id,
        "request_hash": application.request_hash,
        "gate_id": application.gate_id,
        "decision": request.decision,
        "candidate_id": request.candidate_id,
        "rejected_candidates": request.rejected_candidates,
        "rejected_candidate_count": len(request.rejected_candidates),
        "note_count": len(request.notes),
        "method_name": request.method_name,
        "decided_by": request.decided_by,
        "application_id": application.application_id,
        "effect_kind": application.effect_kind,
        "source": intent.source,
    }
    return ledger._record_event(
        session_id,
        "review_decision",
        payload,
        stage=application.stage,
        workflow_step_id=application.workflow_step_id,
        subject_id=application.review_request_id,
        refs=refs,
        idempotency_key=f"review:{application.review_request_id}:decision",
    )


def record_review_applied(
    ledger: Ledger,
    session_id: str,
    *,
    application_ref: Any,
    expected_identity: "ReviewApplicationIdentity",
    checkpoint_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Record application only after independently verifying checkpoint proof."""

    from app.review.application import (
        ReviewApplicationIntegrityError,
        load_verified_review_application,
    )
    from app.review.status import project_review_status
    from app.schemas.artifacts import ArtifactRef
    from app.schemas.state import validate_top_level_state

    ledger.paths.require_session_id(session_id)
    if expected_identity.session_id != session_id:
        raise ValueError("review application identity does not match session_id")
    verified = load_verified_review_application(
        ledger.paths.run_dir,
        application_ref,
        expected_identity=expected_identity,
    )
    application = verified.application
    try:
        checkpoint = validate_top_level_state(dict(checkpoint_state))
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"review_applied checkpoint proof is invalid: {exc}"
        ) from exc
    if checkpoint.get("session_id") != session_id:
        raise ReviewApplicationIntegrityError(
            "review_applied checkpoint session_id does not match application"
        )
    try:
        checkpoint_paths = SessionPaths.from_run_dir(checkpoint.get("data_dir", ""))
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"review_applied checkpoint data_dir is invalid: {exc}"
        ) from exc
    if checkpoint_paths.run_dir != ledger.paths.run_dir:
        raise ReviewApplicationIntegrityError(
            "review_applied checkpoint data_dir does not match Ledger run"
        )
    raw_checkpoint_ref = checkpoint.get("review_application_ref")
    try:
        checkpoint_ref = ArtifactRef.model_validate(raw_checkpoint_ref)
    except Exception as exc:
        raise ReviewApplicationIntegrityError(
            f"review_applied checkpoint has no valid application ref: {exc}"
        ) from exc
    if checkpoint_ref != verified.ref:
        raise ReviewApplicationIntegrityError(
            "review_applied checkpoint application ref does not match application"
        )
    interrupt = checkpoint.get("active_interrupt")
    if checkpoint.get("supervision_state") == "blocked_human" or (
        isinstance(interrupt, Mapping)
        and interrupt.get("review_request_id") == application.review_request_id
    ):
        raise ReviewApplicationIntegrityError(
            "review_applied checkpoint has not progressed beyond the review gate"
        )

    projection = project_review_status(checkpoint, identity=expected_identity)
    if projection.integrity_error is not None:
        raise ReviewApplicationIntegrityError(
            "review_applied checkpoint status is not healthy: "
            f"{projection.integrity_error}"
        )
    if (
        projection.status != "applied"
        or projection.resolution_basis != "checkpoint"
        or projection.application_ref != verified.ref
        or projection.decision_sequence is None
    ):
        raise ReviewApplicationIntegrityError(
            "review_applied requires a verified decision and checkpoint "
            "application proof"
        )

    return ledger._record_event(
        session_id,
        "review_applied",
        {
            "review_request_id": application.review_request_id,
            "request_hash": application.request_hash,
            "gate_id": application.gate_id,
            "application_id": application.application_id,
        },
        stage=application.stage,
        workflow_step_id=application.workflow_step_id,
        subject_id=application.review_request_id,
        refs=[
            {
                **verified.ref.model_dump(mode="json"),
                "role": "application_ref",
            }
        ],
        idempotency_key=(
            f"review:{application.review_request_id}:"
            f"applied:{application.application_id}"
        ),
    )
