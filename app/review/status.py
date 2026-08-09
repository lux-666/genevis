"""Single durable projector for review request application status."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict

from app.control.workflow_plan import workflow_steps_from_run_config
from app.review.application import (
    REVIEW_APPLICATION_ARTIFACT_TYPE,
    REVIEW_INTENT_ARTIFACT_TYPE,
    ReviewApplicationIdentity,
    ReviewApplicationIntegrityError,
    ReviewIntentV1,
    VerifiedReviewApplication,
    load_verified_review_application,
    load_verified_review_intent,
)
from app.review.persistence import load_review_request
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import (
    DomainStage,
    TopLevelState,
    WorkflowStep,
    validate_workflow_steps,
)
from app.storage.artifacts import artifact_ref_for_path
from app.storage.frozen_inputs import validate_frozen_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths

ReviewLifecycleStatus = Literal[
    "awaiting_decision",
    "decision_committed",
    "application_prepared",
    "applied",
]
ReviewResolutionBasis = Literal[
    "none",
    "checkpoint",
    "review_applied",
    "historical_progression",
    "terminal_precedes_request",
]


class ReviewStatusProjection(BaseModel):
    """Bounded read model for one exact review occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ReviewLifecycleStatus
    identity: ReviewApplicationIdentity
    intent_ref: ArtifactRef | None = None
    application_ref: ArtifactRef | None = None
    application_id: str | None = None
    request_event: dict[str, Any] | None = None
    request_sequence: int | None = None
    decision_sequence: int | None = None
    progression_sequence: int | None = None
    resolution_basis: ReviewResolutionBasis = "none"
    integrity_error: str | None = None

    def require_healthy(self) -> "ReviewStatusProjection":
        if self.integrity_error is not None:
            raise ReviewApplicationIntegrityError(self.integrity_error)
        return self

    @property
    def accepts_new_decision(self) -> bool:
        return self.status == "awaiting_decision" and self.integrity_error is None

    @property
    def requires_recovery(self) -> bool:
        return (
            self.status in {"decision_committed", "application_prepared"}
            and self.integrity_error is None
        )

    def to_record(self) -> dict[str, Any]:
        """Return the flattened durable read-model shape used by all consumers."""

        record: dict[str, Any] = {
            **self.identity.model_dump(mode="json"),
            "status": self.status,
            "resolved": self.status == "applied",
            "accepts_new_decision": self.accepts_new_decision,
            "requires_recovery": self.requires_recovery,
            "request_sequence": self.request_sequence,
            "decision_sequence": self.decision_sequence,
            "progression_sequence": self.progression_sequence,
            "resolution_basis": self.resolution_basis,
            "integrity_error": self.integrity_error,
        }
        if self.intent_ref is not None:
            record["intent_ref"] = self.intent_ref.model_dump(mode="json")
        if self.application_ref is not None:
            record["application_ref"] = self.application_ref.model_dump(mode="json")
        if self.application_id is not None:
            record["application_id"] = self.application_id
        if self.request_event is not None:
            record["request_event"] = dict(self.request_event)
            for key in (
                "timestamp",
                "reason",
                "auto_decidable",
                "review_request_artifact_ref",
            ):
                if self.request_event.get(key) is not None:
                    record[key] = self.request_event[key]
        return {key: value for key, value in record.items() if value is not None}


class ReviewHistoryProjection(BaseModel):
    """Canonical terminal plus every verified durable review occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occurrences: tuple[ReviewStatusProjection, ...] = ()
    canonical_terminal: dict[str, Any] | None = None
    terminal_integrity: dict[str, Any] | None = None
    integrity_errors: tuple[str, ...] = ()

    @property
    def latest_unapplied(self) -> ReviewStatusProjection | None:
        if self.canonical_terminal is not None:
            return None
        return next(
            (
                occurrence
                for occurrence in reversed(self.occurrences)
                if occurrence.status != "applied"
            ),
            None,
        )

    @property
    def latest_occurrence(self) -> ReviewStatusProjection | None:
        return self.occurrences[-1] if self.occurrences else None

    def records(self) -> list[dict[str, Any]]:
        return [occurrence.to_record() for occurrence in self.occurrences]


def project_review_status(
    state: TopLevelState,
    *,
    identity: ReviewApplicationIdentity,
) -> ReviewStatusProjection:
    """Project one exact occurrence from immutable records and its checkpoint."""

    paths = SessionPaths.from_run_dir(state.get("data_dir", ""))
    try:
        paths.require_session_id(identity.session_id)
    except Exception as exc:
        return _error("awaiting_decision", identity, f"invalid session: {exc}")
    if state.get("session_id") != identity.session_id:
        return _error(
            "awaiting_decision",
            identity,
            "checkpoint session_id does not match review occurrence",
        )

    ledger = Ledger(paths.run_dir)
    events = ledger.read_events(identity.session_id)
    terminal_read = ledger.read_canonical_terminal(identity.session_id)
    request_event = _request_event_for_identity(events, identity)
    request_sequence = _event_sequence(request_event)
    steps = _steps_from_state(state)
    return _project_identity_status(
        paths,
        identity,
        events=events,
        request_event=request_event,
        request_sequence=request_sequence,
        checkpoint_application_ref=state.get("review_application_ref"),
        canonical_terminal=(
            terminal_read.get("canonical") if terminal_read is not None else None
        ),
        workflow_steps=steps,
    )


def project_review_history(
    data_dir: str | Path,
    session_id: str,
    *,
    checkpoint_state: TopLevelState | None = None,
) -> ReviewHistoryProjection:
    """Project every request using full identity, plan order, and terminal order."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    events = ledger.read_events(session_id)
    terminal_read = ledger.read_canonical_terminal(session_id)
    canonical_terminal = (
        terminal_read.get("canonical") if terminal_read is not None else None
    )
    terminal_integrity = (
        terminal_read.get("terminal_integrity") if terminal_read is not None else None
    )
    workflow_steps, plan_error = _steps_from_frozen_inputs(paths)
    diagnostics: list[str] = []
    if terminal_integrity is not None:
        diagnostics.append(
            _bounded_error(
                "terminal ledger integrity diagnostic: "
                f"{terminal_integrity.get('kind')}"
            )
        )

    occurrences: list[ReviewStatusProjection] = []
    for event in events:
        if event.get("event_type") != "review_requested":
            continue
        try:
            identity = _identity_from_request_event(paths, session_id, event)
        except Exception as exc:
            diagnostics.append(
                _bounded_error(
                    "invalid review_requested event at sequence "
                    f"{_event_sequence(event)}: {type(exc).__name__}: {exc}"
                )
            )
            continue
        checkpoint_ref: Any = None
        if (
            checkpoint_state is not None
            and checkpoint_state.get("session_id") == session_id
        ):
            checkpoint_ref = checkpoint_state.get("review_application_ref")
        occurrence = _project_identity_status(
            paths,
            identity,
            events=events,
            request_event=event,
            request_sequence=_event_sequence(event),
            checkpoint_application_ref=checkpoint_ref,
            canonical_terminal=canonical_terminal,
            workflow_steps=workflow_steps,
        )
        occurrences.append(occurrence)
        if occurrence.integrity_error is not None:
            diagnostics.append(
                _bounded_error(
                    f"review {identity.workflow_step_id}/"
                    f"{identity.review_request_id}: {occurrence.integrity_error}"
                )
            )

    if plan_error is not None and any(
        occurrence.status != "applied" for occurrence in occurrences
    ):
        diagnostics.append(plan_error)
    occurrences.sort(key=lambda item: item.request_sequence or 0)
    return ReviewHistoryProjection(
        occurrences=tuple(occurrences),
        canonical_terminal=(
            _project_terminal_row(canonical_terminal)
            if canonical_terminal is not None
            else None
        ),
        terminal_integrity=terminal_integrity,
        integrity_errors=tuple(dict.fromkeys(diagnostics)),
    )


def _project_identity_status(
    paths: SessionPaths,
    identity: ReviewApplicationIdentity,
    *,
    events: list[dict[str, Any]],
    request_event: dict[str, Any] | None,
    request_sequence: int | None,
    checkpoint_application_ref: Any,
    canonical_terminal: Mapping[str, Any] | None,
    workflow_steps: tuple[WorkflowStep, ...],
) -> ReviewStatusProjection:
    intent_path = _record_path(paths, identity, "intent.json")
    application_path = _record_path(paths, identity, "application.json")
    decision_path = _record_path(paths, identity, "decision.json")
    terminal_sequence = _event_sequence(canonical_terminal)
    if (
        request_sequence is not None
        and terminal_sequence is not None
        and terminal_sequence < request_sequence
    ):
        return _error(
            "applied",
            identity,
            "review_requested occurs after the canonical session terminal",
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=terminal_sequence,
            resolution_basis="terminal_precedes_request",
        )

    post_terminal_diagnostic = _post_terminal_occurrence_diagnostic(
        events,
        terminal_sequence=terminal_sequence,
    )
    events_before_or_at_terminal = _events_through_canonical_terminal(
        events,
        terminal_sequence=terminal_sequence,
    )
    progression_sequence = _historical_progression_sequence(
        identity,
        request_sequence=request_sequence,
        events=events_before_or_at_terminal,
        canonical_terminal=canonical_terminal,
        workflow_steps=workflow_steps,
    )
    if post_terminal_diagnostic is not None:
        return _error(
            "applied" if progression_sequence is not None else "awaiting_decision",
            identity,
            post_terminal_diagnostic,
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=progression_sequence,
            resolution_basis=(
                "historical_progression" if progression_sequence is not None else "none"
            ),
        )

    identity_diagnostic = _review_event_identity_diagnostic(
        events_before_or_at_terminal,
        identity,
        after_sequence=request_sequence,
    )
    if identity_diagnostic is not None:
        return _error(
            "applied" if progression_sequence is not None else "awaiting_decision",
            identity,
            identity_diagnostic,
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=progression_sequence,
            resolution_basis=(
                "historical_progression" if progression_sequence is not None else "none"
            ),
        )

    try:
        intent_ref = _optional_record_ref(
            paths,
            intent_path,
            expected_type=REVIEW_INTENT_ARTIFACT_TYPE,
        )
    except Exception as exc:
        return _error(
            "applied" if progression_sequence is not None else "awaiting_decision",
            identity,
            f"invalid review intent: {exc}",
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=progression_sequence,
            resolution_basis=(
                "historical_progression" if progression_sequence is not None else "none"
            ),
        )

    matching_decisions = _matching_events(
        events_before_or_at_terminal,
        identity,
        "review_decision",
        after_sequence=request_sequence,
    )
    applied_events = _matching_events(
        events_before_or_at_terminal,
        identity,
        "review_applied",
        after_sequence=request_sequence,
    )
    if intent_ref is None:
        legacy_fact = bool(
            decision_path.exists()
            or matching_decisions
            or applied_events
            or application_path.exists()
        )
        if progression_sequence is not None:
            return ReviewStatusProjection(
                status="applied",
                identity=identity,
                request_event=request_event,
                request_sequence=request_sequence,
                progression_sequence=progression_sequence,
                resolution_basis="historical_progression",
            )
        if legacy_fact:
            return _error(
                "awaiting_decision",
                identity,
                "legacy partial review decision exists without intent/application "
                "or verified later progression; same-decision automatic recovery "
                "cannot be proven",
                request_event=request_event,
                request_sequence=request_sequence,
            )
        return ReviewStatusProjection(
            status="awaiting_decision",
            identity=identity,
            request_event=request_event,
            request_sequence=request_sequence,
        )

    try:
        verified_intent = load_verified_review_intent(
            paths.run_dir,
            intent_ref,
            expected_identity=identity,
        )
    except Exception as exc:
        return _error(
            "applied" if progression_sequence is not None else "decision_committed",
            identity,
            f"invalid committed review intent: {exc}",
            intent_ref=intent_ref,
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=progression_sequence,
            resolution_basis=(
                "historical_progression" if progression_sequence is not None else "none"
            ),
        )

    try:
        application_ref = _optional_record_ref(
            paths,
            application_path,
            expected_type=REVIEW_APPLICATION_ARTIFACT_TYPE,
        )
    except Exception as exc:
        return _error(
            "applied" if progression_sequence is not None else "decision_committed",
            identity,
            f"invalid review application: {exc}",
            intent_ref=intent_ref,
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=progression_sequence,
            resolution_basis=(
                "historical_progression" if progression_sequence is not None else "none"
            ),
        )
    if application_ref is None:
        if progression_sequence is not None:
            return ReviewStatusProjection(
                status="applied",
                identity=identity,
                intent_ref=intent_ref,
                request_event=request_event,
                request_sequence=request_sequence,
                progression_sequence=progression_sequence,
                resolution_basis="historical_progression",
            )
        if applied_events:
            return _error(
                "decision_committed",
                identity,
                "review_applied exists without application.json",
                intent_ref=intent_ref,
                request_event=request_event,
                request_sequence=request_sequence,
            )
        return ReviewStatusProjection(
            status="decision_committed",
            identity=identity,
            intent_ref=intent_ref,
            request_event=request_event,
            request_sequence=request_sequence,
        )

    try:
        verified = load_verified_review_application(
            paths.run_dir,
            application_ref,
            expected_identity=identity,
        )
    except Exception as exc:
        return _error(
            "applied" if progression_sequence is not None else "application_prepared",
            identity,
            f"invalid prepared review application: {exc}",
            intent_ref=intent_ref,
            application_ref=application_ref,
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=progression_sequence,
            resolution_basis=(
                "historical_progression" if progression_sequence is not None else "none"
            ),
        )

    application_id = verified.application.application_id
    decision_sequence, decision_diagnostic = _verified_decision_sequence(
        matching_decisions,
        application=verified,
        intent=verified_intent,
    )
    if decision_diagnostic is not None:
        return _error(
            "applied" if progression_sequence is not None else "application_prepared",
            identity,
            decision_diagnostic,
            intent_ref=intent_ref,
            application_ref=application_ref,
            application_id=application_id,
            request_event=request_event,
            request_sequence=request_sequence,
            progression_sequence=progression_sequence,
            resolution_basis=(
                "historical_progression" if progression_sequence is not None else "none"
            ),
        )
    checkpoint_ref: ArtifactRef | None = None
    if checkpoint_application_ref is not None:
        try:
            checkpoint_ref = ArtifactRef.model_validate(checkpoint_application_ref)
        except Exception as exc:
            return _error(
                "application_prepared",
                identity,
                f"invalid checkpoint review_application_ref: {exc}",
                intent_ref=intent_ref,
                application_ref=application_ref,
                application_id=application_id,
                request_event=request_event,
                request_sequence=request_sequence,
            )

    matching_applied = [
        event
        for event in applied_events
        if event.get("application_id") == application_id
        and _event_has_role_ref(event, "application_ref", application_ref)
    ]
    if applied_events and len(matching_applied) != len(applied_events):
        return _error(
            "application_prepared",
            identity,
            "review_applied does not match the verified application",
            intent_ref=intent_ref,
            application_ref=application_ref,
            application_id=application_id,
            request_event=request_event,
            request_sequence=request_sequence,
        )
    if len(matching_applied) > 1:
        return _error(
            "applied",
            identity,
            "multiple review_applied events exist for one application",
            intent_ref=intent_ref,
            application_ref=application_ref,
            application_id=application_id,
            request_event=request_event,
            request_sequence=request_sequence,
            resolution_basis="review_applied",
        )
    if checkpoint_ref == application_ref:
        return ReviewStatusProjection(
            status="applied",
            identity=identity,
            intent_ref=intent_ref,
            application_ref=application_ref,
            application_id=application_id,
            request_event=request_event,
            request_sequence=request_sequence,
            decision_sequence=decision_sequence,
            resolution_basis="checkpoint",
        )
    if matching_applied:
        return ReviewStatusProjection(
            status="applied",
            identity=identity,
            intent_ref=intent_ref,
            application_ref=application_ref,
            application_id=application_id,
            request_event=request_event,
            request_sequence=request_sequence,
            decision_sequence=decision_sequence,
            progression_sequence=_event_sequence(matching_applied[0]),
            resolution_basis="review_applied",
        )
    if progression_sequence is not None:
        return ReviewStatusProjection(
            status="applied",
            identity=identity,
            intent_ref=intent_ref,
            application_ref=application_ref,
            application_id=application_id,
            request_event=request_event,
            request_sequence=request_sequence,
            decision_sequence=decision_sequence,
            progression_sequence=progression_sequence,
            resolution_basis="historical_progression",
        )
    return ReviewStatusProjection(
        status="application_prepared",
        identity=identity,
        intent_ref=intent_ref,
        application_ref=application_ref,
        application_id=application_id,
        request_event=request_event,
        request_sequence=request_sequence,
        decision_sequence=decision_sequence,
    )


def _identity_from_request_event(
    paths: SessionPaths,
    session_id: str,
    event: Mapping[str, Any],
) -> ReviewApplicationIdentity:
    if event.get("event_type") != "review_requested":
        raise ReviewApplicationIntegrityError("event is not review_requested")
    request_ref = _request_ref_from_event(event)
    request = load_review_request(
        paths.run_dir,
        request_ref,
        session_id=session_id,
    )
    workflow_step_id = str(event.get("workflow_step_id") or "")
    stage = str(event.get("stage") or "")
    request_id = str(event.get("review_request_id") or event.get("subject_id") or "")
    expected = {
        "workflow_step_id": workflow_step_id,
        "stage": stage,
        "review_request_id": request_id,
    }
    for field, value in expected.items():
        if not value or request.get(field) != value:
            raise ReviewApplicationIntegrityError(
                f"review request {field} does not match its Ledger event"
            )
    gate_id = request.get("gate_id")
    if not isinstance(gate_id, str) or not gate_id:
        raise ReviewApplicationIntegrityError("review request gate_id is invalid")
    return ReviewApplicationIdentity(
        session_id=session_id,
        workflow_step_id=workflow_step_id,
        stage=cast(DomainStage, stage),
        gate_id=gate_id,
        review_request_id=request_id,
        request_hash=request_ref.sha256,
    )


def _request_ref_from_event(event: Mapping[str, Any]) -> ArtifactRef:
    for raw in event.get("refs") or []:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("role") != "review_request_artifact_ref":
            continue
        payload = dict(raw)
        payload.pop("role", None)
        return ArtifactRef.model_validate(payload)
    raise ReviewApplicationIntegrityError(
        "review_requested event has no verified request artifact ref"
    )


def _request_event_for_identity(
    events: list[dict[str, Any]],
    identity: ReviewApplicationIdentity,
) -> dict[str, Any] | None:
    matches = [
        event
        for event in events
        if event.get("event_type") == "review_requested"
        and event.get("stage") == identity.stage
        and event.get("workflow_step_id") == identity.workflow_step_id
        and str(event.get("review_request_id") or event.get("subject_id") or "")
        == identity.review_request_id
    ]
    return matches[0] if len(matches) == 1 else None


def _matching_events(
    events: list[dict[str, Any]],
    identity: ReviewApplicationIdentity,
    event_type: str,
    *,
    after_sequence: int | None,
) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("event_type") == event_type
        and event.get("stage") == identity.stage
        and event.get("workflow_step_id") == identity.workflow_step_id
        and str(event.get("review_request_id") or event.get("subject_id") or "")
        == identity.review_request_id
        and event.get("request_hash") == identity.request_hash
        and (after_sequence is None or (_event_sequence(event) or 0) > after_sequence)
    ]


def _verified_decision_sequence(
    events: list[dict[str, Any]],
    *,
    application: VerifiedReviewApplication,
    intent: ReviewIntentV1,
) -> tuple[int | None, str | None]:
    """Bind at most one decision event to verified application and intent truth."""

    if not events:
        return None, None
    if len(events) != 1:
        return None, "multiple review_decision events exist for one application"

    event = events[0]
    sequence = _event_sequence(event)
    if sequence is None:
        return None, "review_decision has no valid Ledger sequence"

    request = intent.request
    envelope = application.application
    expected_fields = {
        "application_id": envelope.application_id,
        "gate_id": envelope.gate_id,
        "effect_kind": envelope.effect_kind,
        "decision": request.decision,
        "candidate_id": request.candidate_id,
        "method_name": request.method_name,
        "decided_by": request.decided_by,
        "source": intent.source,
        "rejected_candidates": request.rejected_candidates,
        "rejected_candidate_count": len(request.rejected_candidates),
        "note_count": len(request.notes),
    }
    for field, expected_value in expected_fields.items():
        if event.get(field) != expected_value:
            return (
                None,
                f"review_decision {field} does not match the verified application "
                "and intent",
            )

    expected_refs = {
        "artifact_ref": envelope.decision_ref,
        "intent_ref": envelope.intent_ref,
        "application_ref": application.ref,
    }
    for role, expected_ref in expected_refs.items():
        if not _event_has_role_ref(event, role, expected_ref):
            return (
                None,
                f"review_decision {role} does not match the verified application",
            )

    rationale_ref = request.rationale_artifact_ref
    if rationale_ref is None:
        if _event_has_role(event, "rationale_artifact_ref"):
            return None, "review_decision has an unexpected rationale_artifact_ref"
    elif not _event_has_role_ref(
        event,
        "rationale_artifact_ref",
        rationale_ref,
    ):
        return (
            None,
            "review_decision rationale_artifact_ref does not match immutable intent",
        )
    return sequence, None


def _review_event_identity_diagnostic(
    events: list[dict[str, Any]],
    identity: ReviewApplicationIdentity,
    *,
    after_sequence: int | None,
) -> str | None:
    """Report decision/application rows that collide on a partial identity."""

    for event in events:
        if event.get("event_type") not in {"review_decision", "review_applied"}:
            continue
        if event.get("stage") != identity.stage:
            continue
        if event.get("workflow_step_id") != identity.workflow_step_id:
            continue
        if str(event.get("review_request_id") or event.get("subject_id") or "") != (
            identity.review_request_id
        ):
            continue
        sequence = _event_sequence(event)
        if after_sequence is not None and (
            sequence is None or sequence <= after_sequence
        ):
            continue
        if event.get("request_hash") != identity.request_hash:
            return (
                f"{event.get('event_type')} request_hash does not match the "
                "review occurrence identity"
            )
    return None


def _events_through_canonical_terminal(
    events: list[dict[str, Any]],
    *,
    terminal_sequence: int | None,
) -> list[dict[str, Any]]:
    if terminal_sequence is None:
        return events
    return [
        event
        for event in events
        if (sequence := _event_sequence(event)) is not None
        and sequence <= terminal_sequence
    ]


def _post_terminal_occurrence_diagnostic(
    events: list[dict[str, Any]],
    *,
    terminal_sequence: int | None,
) -> str | None:
    """Treat work/review rows after the canonical terminal only as diagnostics."""

    if terminal_sequence is None:
        return None
    for event in events:
        sequence = _event_sequence(event)
        event_type = event.get("event_type")
        if (
            sequence is not None
            and sequence > terminal_sequence
            and isinstance(event_type, str)
            and event_type.startswith(("stage_", "attempt_", "review_"))
        ):
            return (
                "occurrence event "
                f"{event_type} at sequence {sequence} occurs after the canonical "
                "session terminal"
            )
    return None


def _historical_progression_sequence(
    identity: ReviewApplicationIdentity,
    *,
    request_sequence: int | None,
    events: list[dict[str, Any]],
    canonical_terminal: Mapping[str, Any] | None,
    workflow_steps: tuple[WorkflowStep, ...],
) -> int | None:
    if request_sequence is None or not workflow_steps:
        return None
    indices = {step["step_id"]: index for index, step in enumerate(workflow_steps)}
    request_index = indices.get(identity.workflow_step_id)
    if request_index is None:
        return None
    request_step = workflow_steps[request_index]
    if request_step["stage"] != identity.stage:
        return None

    for event in events:
        sequence = _event_sequence(event)
        if sequence is None or sequence <= request_sequence:
            continue
        step_id = event.get("workflow_step_id")
        stage = event.get("stage")
        event_type = event.get("event_type")
        if step_id == identity.workflow_step_id and stage == identity.stage:
            if event_type in {"stage_completed", "stage_skipped", "stage_failed"}:
                return sequence
            continue
        later_index = indices.get(str(step_id))
        if later_index is None or later_index <= request_index:
            continue
        if workflow_steps[later_index]["stage"] != stage:
            continue
        if event_type in {
            "stage_entered",
            "stage_completed",
            "stage_skipped",
            "stage_failed",
            "attempt_started",
            "attempt_succeeded",
            "attempt_failed",
        }:
            return sequence

    terminal = canonical_terminal
    terminal_sequence = _event_sequence(terminal)
    if terminal_sequence is None or terminal_sequence <= request_sequence:
        return None
    assert terminal is not None
    summary = terminal.get("summary")
    outcome = summary.get("outcome") if isinstance(summary, Mapping) else None
    if outcome == "completed":
        return terminal_sequence
    terminal_stage = terminal.get("stage")
    matching_terminal_indices = [
        index
        for index, step in enumerate(workflow_steps)
        if step["stage"] == terminal_stage
    ]
    if (
        outcome == "failed"
        and len(matching_terminal_indices) == 1
        and matching_terminal_indices[0] >= request_index
    ):
        return terminal_sequence
    return None


def _steps_from_state(state: TopLevelState) -> tuple[WorkflowStep, ...]:
    try:
        return tuple(validate_workflow_steps(state.get("workflow_steps")))
    except (TypeError, ValueError):
        return ()


def _steps_from_frozen_inputs(
    paths: SessionPaths,
) -> tuple[tuple[WorkflowStep, ...], str | None]:
    try:
        bundle = validate_frozen_inputs(paths)
        return tuple(workflow_steps_from_run_config(bundle.run_config)), None
    except (OSError, ValueError, TypeError) as exc:
        return (), _bounded_error(
            "verified frozen workflow plan is unavailable for review progression: "
            f"{type(exc).__name__}: {exc}"
        )


def _optional_record_ref(
    paths: SessionPaths,
    path: Path,
    *,
    expected_type: str,
) -> ArtifactRef | None:
    if not path.exists():
        return None
    return artifact_ref_for_path(
        paths.run_dir,
        paths.session_id,
        path,
        expected_type=expected_type,
    )


def _record_path(
    paths: SessionPaths,
    identity: ReviewApplicationIdentity,
    filename: str,
) -> Path:
    return paths.workflow_step_artifact_path(
        identity.workflow_step_id,
        identity.stage,
        "reviews",
        identity.review_request_id,
        filename,
    )


def _event_has_role(event: Mapping[str, Any], role: str) -> bool:
    return any(
        isinstance(ref, Mapping) and ref.get("role") == role
        for ref in event.get("refs") or []
    )


def _event_has_role_ref(
    event: Mapping[str, Any],
    role: str,
    expected: ArtifactRef,
) -> bool:
    matches = [
        ref
        for ref in event.get("refs") or []
        if isinstance(ref, Mapping) and ref.get("role") == role
    ]
    if len(matches) != 1:
        return False
    payload = dict(matches[0])
    payload.pop("role", None)
    try:
        return ArtifactRef.model_validate(payload) == expected
    except Exception:
        return False


def _event_sequence(event: Mapping[str, Any] | None) -> int | None:
    if event is None:
        return None
    sequence = event.get("sequence")
    return sequence if type(sequence) is int and sequence > 0 else None


def _project_terminal_row(row: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(row)
    summary = projected.pop("summary", None)
    if isinstance(summary, Mapping):
        projected.update(dict(summary))
    return projected


def _error(
    status: ReviewLifecycleStatus,
    identity: ReviewApplicationIdentity,
    message: str,
    *,
    intent_ref: ArtifactRef | None = None,
    application_ref: ArtifactRef | None = None,
    application_id: str | None = None,
    request_event: dict[str, Any] | None = None,
    request_sequence: int | None = None,
    progression_sequence: int | None = None,
    resolution_basis: ReviewResolutionBasis = "none",
) -> ReviewStatusProjection:
    return ReviewStatusProjection(
        status=status,
        identity=identity,
        intent_ref=intent_ref,
        application_ref=application_ref,
        application_id=application_id,
        request_event=request_event,
        request_sequence=request_sequence,
        progression_sequence=progression_sequence,
        resolution_basis=resolution_basis,
        integrity_error=_bounded_error(message),
    )


def _bounded_error(message: str) -> str:
    return " ".join(str(message).split())[:1024]
