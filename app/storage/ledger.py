"""Append-only Data Architecture v2 lifecycle ledger.

The ledger owns causal ordering only.  Evidence bodies live in immutable
artifacts and are referenced from the bounded event envelope written here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping
from uuid import uuid4

from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.storage.artifacts import read_artifact_records
from app.storage.paths import DataLayoutError, SessionPaths, ensure_run_layout

if TYPE_CHECKING:
    from app.review.application import ReviewApplicationIdentity

LEDGER_SCHEMA_VERSION = 2
SUMMARY_MAX_BYTES = 12 * 1024
MAX_REFS = 64
MAX_LIST_ITEMS = 32
MAX_TEXT_CHARS = 4096

_COLLECTION_FACTS = frozenset({"artifacts", "assets", "candidates", "signals"})
_RESERVED_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "sequence",
        "event_id",
        "idempotency_key",
        "session_id",
        "timestamp",
        "event_type",
        "workflow_step_id",
        "subject_id",
    }
)
_REF_KEYS = frozenset(
    {
        "artifact_ref",
        "blocked_input_ref",
        "bundle_ref",
        "comparison_artifact_ref",
        "decision_artifact_ref",
        "draft_assembly_ref",
        "input_manifest_ref",
        "rationale_artifact_ref",
        "registry_ref",
        "review_request_artifact_ref",
    }
)
_OCCURRENCE_EVENT_TYPES = frozenset(
    {
        "artifact_registered",
        "assets_registered",
        "attempt_recorded",
        "attempt_started",
        "attempt_succeeded",
        "attempt_failed",
        "method_disposition_recorded",
        "review_requested",
        "review_decision",
        "review_applied",
        "resource_delivered",
        "stage_entered",
        "stage_skipped",
        "stage_completed",
        "stage_failed",
        "env_preflight_blocked",
        "candidate_comparison_generated",
        "registered_assembly_candidate_bound",
        "automatic_review_failed",
    }
)
_TYPED_REVIEW_EVENT_WRITERS = {
    "review_requested": "record_review_requested",
    "review_decision": "record_review_decision",
    "review_applied": "record_review_applied",
    "method_disposition_recorded": "record_method_disposition",
    "resource_delivered": "record_resource_delivered",
}
_STAGE_TERMINAL_PRECEDENCE = {
    "stage_completed": 0,
    "stage_skipped": 1,
    "stage_failed": 2,
}
_ATTEMPT_TERMINAL_PRECEDENCE = {
    "attempt_succeeded": 0,
    "attempt_failed": 1,
}
_METHOD_DISPOSITIONS = frozenset({"not_started", "skipped", "completed", "failed"})


class LedgerFormatError(ValueError):
    """Raised when a ledger contains a non-v2 or malformed row."""


class LedgerConflictError(RuntimeError):
    """Raised when one idempotency key is reused for different facts."""


def project_stage_terminal_events(
    events: Iterable[Mapping[str, Any]],
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    list[dict[str, Any]],
]:
    """Project one terminal outcome per workflow occurrence.

    Immutable legacy ledgers may contain more than one terminal row for an
    occurrence. Such history is never silently resolved by row order: the
    read model applies ``failed > skipped > completed`` and returns a bounded
    integrity diagnostic describing every duplicate or conflicting row.
    """

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_workflow_step: dict[str, list[dict[str, Any]]] = {}
    for raw_event in events:
        event_type = raw_event.get("event_type")
        if event_type not in _STAGE_TERMINAL_PRECEDENCE:
            continue
        workflow_step_id = raw_event.get("workflow_step_id")
        stage = raw_event.get("stage")
        if not isinstance(workflow_step_id, str) or not workflow_step_id:
            continue
        if not isinstance(stage, str) or not stage:
            continue
        event = dict(raw_event)
        grouped.setdefault((workflow_step_id, stage), []).append(event)
        by_workflow_step.setdefault(workflow_step_id, []).append(event)

    canonical: dict[tuple[str, str], dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    for (workflow_step_id, stage), terminal_events in grouped.items():
        selected_type = max(
            (str(event["event_type"]) for event in terminal_events),
            key=_STAGE_TERMINAL_PRECEDENCE.__getitem__,
        )
        selected = next(
            event
            for event in terminal_events
            if event.get("event_type") == selected_type
        )
        canonical[(workflow_step_id, stage)] = selected
        if len(terminal_events) == 1:
            continue
        event_types = [str(event["event_type"]) for event in terminal_events]
        diagnostics.append(
            {
                "kind": (
                    "duplicate_stage_terminal"
                    if all(item == event_types[0] for item in event_types[1:])
                    else "conflicting_stage_terminal"
                ),
                "workflow_step_id": workflow_step_id,
                "stage": stage,
                "terminal_count": len(terminal_events),
                "selected_event_type": selected_type,
                "selected_sequence": selected.get("sequence"),
                "terminal_events": [
                    {
                        "event_type": event.get("event_type"),
                        "sequence": event.get("sequence"),
                        "event_id": event.get("event_id"),
                    }
                    for event in terminal_events
                ],
            }
        )
    for workflow_step_id, terminal_events in by_workflow_step.items():
        stages = sorted({str(event["stage"]) for event in terminal_events})
        if len(stages) <= 1:
            continue
        selected_type = max(
            (str(event["event_type"]) for event in terminal_events),
            key=_STAGE_TERMINAL_PRECEDENCE.__getitem__,
        )
        selected = next(
            event
            for event in terminal_events
            if event.get("event_type") == selected_type
        )
        diagnostics.append(
            {
                "kind": "conflicting_stage_terminal_stage_binding",
                "workflow_step_id": workflow_step_id,
                "stage": None,
                "stages": stages,
                "terminal_count": len(terminal_events),
                "selected_event_type": selected_type,
                "selected_sequence": selected.get("sequence"),
                "terminal_events": [
                    {
                        "event_type": event.get("event_type"),
                        "stage": event.get("stage"),
                        "sequence": event.get("sequence"),
                        "event_id": event.get("event_id"),
                    }
                    for event in terminal_events
                ],
            }
        )
    return canonical, diagnostics


def project_attempt_terminal_events(
    events: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Project one conservative terminal outcome per immutable attempt ID."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_event in events:
        if raw_event.get("event_type") not in _ATTEMPT_TERMINAL_PRECEDENCE:
            continue
        attempt_id = raw_event.get("subject_id") or raw_event.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id:
            grouped.setdefault(attempt_id, []).append(dict(raw_event))
    canonical: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    for attempt_id, terminal_events in grouped.items():
        selected = max(
            terminal_events,
            key=lambda event: _ATTEMPT_TERMINAL_PRECEDENCE[str(event["event_type"])],
        )
        canonical[attempt_id] = selected
        if len(terminal_events) > 1:
            event_types = [str(event["event_type"]) for event in terminal_events]
            diagnostics.append(
                {
                    "kind": (
                        "duplicate_attempt_terminal"
                        if all(item == event_types[0] for item in event_types[1:])
                        else "conflicting_attempt_terminal"
                    ),
                    "attempt_id": attempt_id,
                    "terminal_count": len(terminal_events),
                    "selected_event_type": selected.get("event_type"),
                    "selected_sequence": selected.get("sequence"),
                    "terminal_events": [
                        {
                            "event_type": event.get("event_type"),
                            "sequence": event.get("sequence"),
                            "event_id": event.get("event_id"),
                            "workflow_step_id": event.get("workflow_step_id"),
                            "stage": event.get("stage"),
                            "method_name": event.get("method_name"),
                        }
                        for event in terminal_events
                    ],
                }
            )
    return canonical, diagnostics


class Ledger:
    """One self-contained run's append-only causal event stream."""

    def __init__(self, data_dir: Path | str) -> None:
        self.paths = SessionPaths.from_run_dir(data_dir)
        self.data_dir = self.paths.run_dir

    @property
    def events_path(self) -> Path:
        return self.paths.ledger_events_path

    def create_session(self, session_id: str, input_manifest_ref: str) -> None:
        """Idempotently record creation in the event stream.

        V2 deliberately has no ``ledger/session.json``.  ``session_created`` is
        the sole durable owner of the session creation timestamp and manifest
        reference, and :meth:`read_session` is its read-model projection.
        """
        self.paths.require_session_id(session_id)
        refs = []
        if input_manifest_ref:
            refs.append(self._path_ref("input_manifest_ref", input_manifest_ref))
        self.record_event(
            session_id,
            "session_created",
            {},
            subject_id=session_id,
            refs=refs,
            idempotency_key=f"session:{session_id}:created",
        )

    def record_attempt_started(
        self, session_id: str, attempt: FrozenAttemptSpec
    ) -> None:
        """Append an idempotent attempt-start event."""
        workflow_step_id = _workflow_step_id_from_attempt(attempt)
        self.record_event(
            session_id,
            "attempt_started",
            {"method_name": attempt.method_name},
            stage=attempt.stage,
            workflow_step_id=workflow_step_id,
            subject_id=attempt.attempt_id,
            idempotency_key=f"attempt:{attempt.attempt_id}:started",
        )

    def record_attempt_succeeded(
        self,
        session_id: str,
        attempt: FrozenAttemptSpec,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append an attempt-success event referring to its output evidence."""
        workflow_step_id = _workflow_step_id_from_attempt(attempt)
        self.record_event(
            session_id,
            "attempt_succeeded",
            {"method_name": attempt.method_name},
            stage=attempt.stage,
            workflow_step_id=workflow_step_id,
            subject_id=attempt.attempt_id,
            refs=self._artifact_refs(artifacts or []),
            idempotency_key=f"attempt:{attempt.attempt_id}:succeeded",
        )

    def record_attempt_failed(
        self,
        session_id: str,
        attempt: FrozenAttemptSpec,
        *,
        return_code: int | None = None,
        error: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append an attempt-failure event with a bounded diagnostic summary."""
        workflow_step_id = _workflow_step_id_from_attempt(attempt)
        self.record_event(
            session_id,
            "attempt_failed",
            {
                "method_name": attempt.method_name,
                "return_code": return_code,
                "error": error,
            },
            stage=attempt.stage,
            workflow_step_id=workflow_step_id,
            subject_id=attempt.attempt_id,
            refs=self._artifact_refs(artifacts or []),
            idempotency_key=f"attempt:{attempt.attempt_id}:failed",
        )

    def record_method_disposition(
        self,
        session_id: str,
        stage: str,
        workflow_step_id: str,
        method_name: str,
        disposition: Literal["not_started", "skipped", "completed", "failed"],
        *,
        reason: str | None = None,
        refs: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        """Record the one immutable disposition of a method occurrence.

        A disposition is deliberately not an attempt result or an occurrence
        terminal.  The append boundary keys it by the frozen occurrence and
        method name, so an exact semantic replay returns the original row and
        any changed disposition, reason, or evidence ref fails closed.
        """

        if not isinstance(method_name, str) or not method_name:
            raise ValueError("method disposition requires a non-empty method_name")
        if disposition not in _METHOD_DISPOSITIONS:
            raise ValueError(
                "method disposition must be one of: "
                "not_started, skipped, completed, failed"
            )
        if disposition in {"skipped", "failed"} and (
            not isinstance(reason, str) or not reason.strip()
        ):
            raise ValueError(f"method disposition {disposition!r} requires a reason")

        step_id = _require_workflow_step_id(workflow_step_id)
        subject_id = _method_disposition_subject_id(step_id, method_name)
        return self._record_event(
            session_id,
            "method_disposition_recorded",
            {
                "method_name": method_name,
                "disposition": disposition,
                "reason": reason,
            },
            stage=stage,
            workflow_step_id=step_id,
            subject_id=subject_id,
            refs=refs,
            idempotency_key=f"method-disposition:{subject_id}",
        )

    def record_method_disposition_recorded(
        self,
        session_id: str,
        stage: str,
        workflow_step_id: str,
        method_name: str,
        disposition: Literal["not_started", "skipped", "completed", "failed"],
        *,
        reason: str | None = None,
        refs: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        """Compatibility spelling for :meth:`record_method_disposition`."""

        return self.record_method_disposition(
            session_id,
            stage,
            workflow_step_id,
            method_name,
            disposition,
            reason=reason,
            refs=refs,
        )

    def has_attempt_success(self, session_id: str, attempt_id: str) -> bool:
        """Return True when the attempt already owns a terminal success event.

        Collect adapters own ``attempt_succeeded``; the shared failure
        boundary records ``attempt_failed`` only while the attempt has not
        already succeeded, so one attempt never carries both terminal facts.
        """
        return any(
            row["event_type"] == "attempt_succeeded" and row["subject_id"] == attempt_id
            for row in self.read_event_rows(session_id)
        )

    def record_artifact_registered(
        self,
        session_id: str,
        artifact: Any,
        *,
        attempt_id: str | None = None,
    ) -> None:
        """Record the causal registration without copying index metadata."""
        artifact_record = _as_mapping(artifact)
        ref = self._artifact_ref(artifact_record)
        indexed_record = self._indexed_artifact_record(ref)
        workflow_step_id = _require_workflow_step_id(
            indexed_record.get("workflow_step_id")
        )
        indexed_stage = indexed_record.get("stage")
        if not isinstance(indexed_stage, str) or not indexed_stage:
            raise DataLayoutError("artifact index record has no stage")
        supplied_stage = _optional_text(artifact_record.get("stage"))
        if supplied_stage is not None and supplied_stage != indexed_stage:
            raise DataLayoutError("artifact stage does not match its artifact index")
        artifact_id = str(ref.get("artifact_id") or ref.get("path_ref") or "unknown")
        self.record_event(
            session_id,
            "artifact_registered",
            {"attempt_id": attempt_id},
            stage=indexed_stage,
            workflow_step_id=workflow_step_id,
            subject_id=artifact_id,
            refs=[ref],
            idempotency_key=f"artifact:{artifact_id}:registered",
        )

    def record_assets_registered(
        self,
        session_id: str,
        assets: list[dict[str, Any]],
        *,
        registry_ref: Any,
        ambiguous: bool = False,
        workflow_step_id: str,
    ) -> None:
        """Record inventory cardinality and the authoritative registry ref."""
        registry = self._path_ref("registry_ref", registry_ref)
        subject_id = str(registry.get("path_ref") or registry_ref)
        self.record_event(
            session_id,
            "assets_registered",
            {"asset_count": len(assets), "ambiguous": ambiguous},
            stage="inventory",
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=subject_id,
            refs=[registry],
            idempotency_key=f"asset-registry:{subject_id}:registered",
        )

    def record_review_requested(
        self,
        session_id: str,
        stage: str,
        *,
        reason: str,
        signals: list[dict[str, Any]] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        review_request_artifact_ref: Any,
        auto_decidable: bool | None = None,
        review_request_id: str | None = None,
    ) -> None:
        """Record a request by ID; full signals/candidates remain in its artifact."""
        request_ref = self._indexed_named_artifact_ref(
            "review_request_artifact_ref", review_request_artifact_ref
        )
        request_record = self._indexed_artifact_record(request_ref)
        workflow_step_id = _require_workflow_step_id(
            request_record.get("workflow_step_id")
        )
        if request_record.get("stage") != stage:
            raise DataLayoutError("review request stage does not match its artifact")
        indexed_request_id = _require_review_request_id(request_record)
        if review_request_id is not None and review_request_id != indexed_request_id:
            raise DataLayoutError("review request ID does not match its artifact index")
        request_id = indexed_request_id
        payload: dict[str, Any] = {
            "reason": reason,
            "signal_count": len(signals or []),
            "candidate_count": len(candidates or []),
            "review_request_id": request_id,
        }
        if auto_decidable is not None:
            payload["auto_decidable"] = auto_decidable
        self._record_event(
            session_id,
            "review_requested",
            payload,
            stage=stage,
            workflow_step_id=workflow_step_id,
            subject_id=request_id,
            refs=[request_ref],
            idempotency_key=f"review:{request_id}:requested",
        )

    def record_review_decision(
        self,
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

        self.paths.require_session_id(session_id)
        if expected_identity.session_id != session_id:
            raise ValueError("review application identity does not match session_id")
        verified = load_verified_review_application(
            self.paths.run_dir,
            application_ref,
            expected_identity=expected_identity,
        )
        application = verified.application
        intent = load_verified_review_intent(
            self.paths.run_dir,
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
                self._path_ref(
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
        return self._record_event(
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
        self,
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

        self.paths.require_session_id(session_id)
        if expected_identity.session_id != session_id:
            raise ValueError("review application identity does not match session_id")
        verified = load_verified_review_application(
            self.paths.run_dir,
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
        if checkpoint_paths.run_dir != self.paths.run_dir:
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

        return self._record_event(
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

    def record_stage_entered(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        route_node: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self.record_event(
            session_id,
            "stage_entered",
            {"route_node": route_node},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            idempotency_key=idempotency_key or f"stage-entered:{workflow_step_id}",
        )

    def record_resource_delivered(
        self,
        session_id: str,
        *,
        stage: str,
        workflow_step_id: str,
        resource_type: str,
        delivery_mode: Literal["materialized", "reused"],
        candidate_bundle_ref: Any,
        primary_output_ref: Any,
    ) -> dict[str, Any]:
        """Persist an occurrence-owned immutable resource/candidate relation."""

        if not isinstance(resource_type, str) or not resource_type:
            raise ValueError("resource delivery requires a resource_type")
        if delivery_mode not in {"materialized", "reused"}:
            raise ValueError("resource delivery mode is invalid")
        candidate_ref = self._indexed_named_artifact_ref(
            "candidate_bundle_ref", candidate_bundle_ref
        )
        primary_ref = self._indexed_named_artifact_ref(
            "primary_output_ref", primary_output_ref
        )
        return self._record_event(
            session_id,
            "resource_delivered",
            {
                "resource_type": resource_type,
                "delivery_mode": delivery_mode,
            },
            stage=stage,
            workflow_step_id=workflow_step_id,
            subject_id=f"{workflow_step_id}:{resource_type}",
            refs=[candidate_ref, primary_ref],
            idempotency_key=f"resource-delivered:{workflow_step_id}:{resource_type}",
        )

    def record_stage_skipped(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        reason: str,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
        selected_method_names: Iterable[str] | None = None,
    ) -> None:
        self._record_event(
            session_id,
            "stage_skipped",
            {"reason": reason},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            refs=refs,
            idempotency_key=idempotency_key or f"stage-skipped:{workflow_step_id}",
            selected_method_names=selected_method_names,
        )

    def record_stage_completed(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
        selected_method_names: Iterable[str] | None = None,
    ) -> None:
        self._record_event(
            session_id,
            "stage_completed",
            {},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            refs=refs,
            idempotency_key=idempotency_key or f"stage-completed:{workflow_step_id}",
            selected_method_names=selected_method_names,
        )

    def record_stage_failed(
        self,
        session_id: str,
        stage: str,
        *,
        workflow_step_id: str,
        error: str | None = None,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
        selected_method_names: Iterable[str] | None = None,
    ) -> None:
        self._record_event(
            session_id,
            "stage_failed",
            {"error": error},
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=workflow_step_id,
            refs=refs,
            idempotency_key=idempotency_key or f"stage-failed:{workflow_step_id}",
            selected_method_names=selected_method_names,
        )

    def record_session_terminal(
        self,
        session_id: str,
        *,
        outcome: Literal["completed", "failed"],
        stage: str | None = None,
        error: str | None = None,
        refs: Iterable[Any] | None = None,
        failure_scope: Literal["occurrence", "controller_boundary"] | None = None,
    ) -> dict[str, Any]:
        """Append the single irreversible terminal fact for this session.

        A terminal's first durable outcome is canonical. Replays with that
        outcome return its exact row, while a conflicting outcome or legacy
        duplicate terminal history fails closed.
        """
        self.paths.require_session_id(session_id)
        if outcome not in {"completed", "failed"}:
            raise ValueError("terminal outcome must be one of: completed, failed")
        if outcome == "completed":
            if failure_scope is not None:
                raise ValueError("completed terminal must not include failure_scope")
            terminal_payload: dict[str, Any] = {"outcome": outcome, "error": error}
        else:
            # Direct Ledger callers historically had no scope argument.  Keep
            # that call shape while ensuring every newly written failed row is
            # explicit; recovery passes controller_boundary when its checkpoint
            # proof establishes an inter-occurrence crash.
            scope = failure_scope or "occurrence"
            if scope not in {"occurrence", "controller_boundary"}:
                raise ValueError(
                    "failed terminal failure_scope must be one of: occurrence, "
                    "controller_boundary"
                )
            terminal_payload = {
                "outcome": outcome,
                "error": error,
                "failure_scope": scope,
            }

        semantic = self._event_semantic(
            session_id,
            "session_terminal",
            terminal_payload,
            stage=stage,
            subject_id=session_id,
            refs=refs,
        )
        key = f"session:{session_id}:terminal"
        ensure_run_layout(self.paths)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                rows = self._rows_from_locked_handle(handle)
                terminals = [
                    row for row in rows if row["event_type"] == "session_terminal"
                ]
                if len(terminals) > 1:
                    raise LedgerConflictError(
                        "ledger integrity conflict: multiple session_terminal events"
                    )
                if terminals:
                    canonical = terminals[0]
                    if _semantic_body(canonical) != semantic:
                        if canonical["summary"].get("outcome") != outcome:
                            detail = "outcome conflicts with the canonical terminal"
                        else:
                            detail = "same outcome has different semantic body"
                        raise LedgerConflictError(f"session terminal {detail}")
                    natural_replay = canonical
                else:
                    natural_replay = None
                idempotency_replay = self._idempotency_replay_locked(
                    rows, key, semantic
                )
                if idempotency_replay is not None:
                    return idempotency_replay
                if natural_replay is not None:
                    return natural_replay
                return self._append_new_row_locked(handle, rows, semantic, key)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_canonical_terminal(self, session_id: str) -> dict[str, Any] | None:
        """Return the first terminal row plus any immutable-history diagnostic.

        ``None`` means no terminal exists. Duplicate same-outcome rows are
        ``duplicate_session_terminal``; different outcomes are
        ``conflicting_session_terminal``. Neither replaces the first row.
        """
        terminals = [
            row
            for row in self.read_event_rows(session_id)
            if row["event_type"] == "session_terminal"
        ]
        if not terminals:
            return None

        terminal_integrity: dict[str, Any] | None = None
        if len(terminals) > 1:
            outcomes = [row["summary"].get("outcome") for row in terminals]
            terminal_integrity = {
                "kind": (
                    "duplicate_session_terminal"
                    if all(outcome == outcomes[0] for outcome in outcomes[1:])
                    else "conflicting_session_terminal"
                ),
                "terminal_count": len(terminals),
                "duplicate_rows": terminals[1:],
            }
        return {"canonical": terminals[0], "terminal_integrity": terminal_integrity}

    def read_session(self, session_id: str) -> dict[str, Any] | None:
        """Project session metadata from ``session_created``."""
        self.paths.require_session_id(session_id)
        created = next(
            (
                row
                for row in self.read_events(session_id)
                if row["event_type"] == "session_created"
            ),
            None,
        )
        if created is None:
            return None
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "session_id": session_id,
            "input_manifest_ref": created.get("input_manifest_ref"),
            "created_at": created.get("timestamp"),
            "created_event_id": created.get("event_id"),
        }

    def read_event_rows(self, session_id: str) -> list[dict[str, Any]]:
        """Read and validate the exact v2 rows stored on disk."""
        self.paths.require_session_id(session_id)
        if not self.events_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerFormatError(
                        f"invalid JSON in {self.events_path}:{line_number}"
                    ) from exc
                self._validate_row(row, line_number)
                rows.append(row)
        return rows

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        """Return bounded query projections while preserving the v2 envelope."""
        return [self._project_row(row) for row in self.read_event_rows(session_id)]

    def last_sequence(self, session_id: str) -> int:
        rows = self.read_event_rows(session_id)
        return int(rows[-1]["sequence"]) if rows else 0

    def record_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        stage: str | None = None,
        workflow_step_id: str | None = None,
        subject_id: str | None = None,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append one idempotent, bounded v2 event and return its exact row."""
        writer = _TYPED_REVIEW_EVENT_WRITERS.get(event_type)
        if writer is not None:
            raise ValueError(f"{event_type} must be recorded with {writer}")
        return self._record_event(
            session_id,
            event_type,
            payload,
            stage=stage,
            workflow_step_id=workflow_step_id,
            subject_id=subject_id,
            refs=refs,
            idempotency_key=idempotency_key,
        )

    def _record_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        stage: str | None = None,
        workflow_step_id: str | None = None,
        subject_id: str | None = None,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
        selected_method_names: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Internal append primitive used after a typed writer has verified facts."""
        self.paths.require_session_id(session_id)
        if not event_type or not isinstance(event_type, str):
            raise ValueError("event_type must be a non-empty string")
        if event_type == "session_terminal":
            raise ValueError(
                "session_terminal must be recorded with record_session_terminal"
            )
        semantic = self._event_semantic(
            session_id,
            event_type,
            payload,
            stage=stage,
            workflow_step_id=workflow_step_id,
            subject_id=subject_id,
            refs=refs,
        )
        key = idempotency_key or _default_idempotency_key(semantic)
        if len(key) > 256:
            key = f"idem_{_sha256_text(key)}"

        ensure_run_layout(self.paths)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                rows = self._rows_from_locked_handle(handle)
                existing_attempt_terminal = self._existing_attempt_terminal_locked(
                    rows, semantic
                )
                existing_method_disposition = self._existing_method_disposition_locked(
                    rows, semantic
                )
                existing_terminal = self._existing_stage_terminal_locked(rows, semantic)
                self._assert_stage_terminal_method_coverage_locked(
                    rows,
                    semantic,
                    selected_method_names=selected_method_names,
                )
                # Natural-key checks must execute before idempotency lookup, but
                # must not return early: a key can be occupied by another domain
                # fact and is still a conflict even when this write is an exact
                # stage/attempt/method replay under a different key.
                natural_replay = next(
                    (
                        existing
                        for existing in (
                            existing_attempt_terminal,
                            existing_method_disposition,
                            existing_terminal,
                        )
                        if existing is not None
                    ),
                    None,
                )
                idempotency_replay = self._idempotency_replay_locked(
                    rows, key, semantic
                )
                if idempotency_replay is not None:
                    return idempotency_replay
                if natural_replay is not None:
                    return natural_replay
                return self._append_new_row_locked(handle, rows, semantic, key)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def record_occurrence_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        *,
        stage: str,
        workflow_step_id: str,
        subject_id: str | None = None,
        refs: Iterable[Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append a stage-runtime fact bound to one frozen occurrence."""

        if not isinstance(stage, str) or not stage:
            raise ValueError("occurrence event stage must be a non-empty string")
        if subject_id is None and idempotency_key is None:
            raise ValueError(
                "occurrence event requires a subject_id or idempotency_key"
            )
        return self.record_event(
            session_id,
            event_type,
            payload,
            stage=stage,
            workflow_step_id=_require_workflow_step_id(workflow_step_id),
            subject_id=subject_id,
            refs=refs,
            idempotency_key=idempotency_key,
        )

    def references_from(
        self,
        value: Any,
        *,
        role_prefix: str,
    ) -> list[dict[str, Any]]:
        """Extract normalized run-local refs from a compact nested value."""

        return self._normalize_refs(
            self._refs_from_nested(value, role_prefix=role_prefix)
        )

    def _rows_from_locked_handle(self, handle: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerFormatError(
                    f"invalid JSON in {self.events_path}:{line_number}"
                ) from exc
            self._validate_row(row, line_number)
            rows.append(row)
        return rows

    def _event_semantic(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None,
        *,
        stage: str | None = None,
        workflow_step_id: str | None = None,
        subject_id: str | None = None,
        refs: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        """Normalize the immutable body shared by generic and terminal writes."""
        raw = dict(payload or {})
        reserved = sorted(set(raw) & _RESERVED_PAYLOAD_KEYS)
        if reserved:
            raise ValueError(
                "ledger payload cannot override envelope fields: " + ", ".join(reserved)
            )
        stage = stage or _optional_text(raw.pop("stage", None))
        if workflow_step_id is None:
            workflow_step_id = _optional_text(raw.pop("workflow_step_id", None))
        else:
            raw.pop("workflow_step_id", None)
        if event_type in _OCCURRENCE_EVENT_TYPES or (
            stage is not None and event_type != "session_terminal"
        ):
            workflow_step_id = _require_workflow_step_id(workflow_step_id)
        elif workflow_step_id is not None:
            workflow_step_id = _require_workflow_step_id(workflow_step_id)
        subject_id = subject_id or _subject_from_payload(raw)
        payload_refs, summary_input = self._split_payload(raw)
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "session_id": session_id,
            "event_type": event_type,
            "stage": stage,
            "workflow_step_id": workflow_step_id,
            "subject_id": subject_id,
            "refs": self._normalize_refs([*(refs or []), *payload_refs]),
            "summary": _bounded_summary(summary_input),
        }

    def _append_new_row_locked(
        self,
        handle: Any,
        rows: list[dict[str, Any]],
        semantic: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Append one prepared body while the caller holds ``LOCK_EX``."""
        sequence = int(rows[-1]["sequence"]) + 1 if rows else 1
        row = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": f"evt_{sequence:08d}_{uuid4().hex[:12]}",
            "idempotency_key": idempotency_key,
            "session_id": semantic["session_id"],
            "timestamp": _now(),
            "event_type": semantic["event_type"],
            "stage": semantic["stage"],
            "workflow_step_id": semantic["workflow_step_id"],
            "subject_id": semantic["subject_id"],
            "refs": semantic["refs"],
            "summary": semantic["summary"],
        }
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return row

    @staticmethod
    def _idempotency_replay_locked(
        rows: Iterable[Mapping[str, Any]],
        key: str,
        semantic: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Validate one key owns exactly one immutable semantic fact."""

        matches = [dict(row) for row in rows if row.get("idempotency_key") == key]
        if not matches:
            return None
        if len(matches) != 1:
            raise LedgerConflictError(
                f"ledger integrity conflict: idempotency key {key!r} owns multiple rows"
            )
        existing = matches[0]
        if _semantic_body(existing) != semantic:
            raise LedgerConflictError(
                f"idempotency key {key!r} already owns a different ledger fact"
            )
        return existing

    @staticmethod
    def _existing_attempt_terminal_locked(
        rows: Iterable[Mapping[str, Any]],
        semantic: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically enforce one succeeded-or-failed terminal per attempt."""

        event_type = semantic.get("event_type")
        if event_type not in _ATTEMPT_TERMINAL_PRECEDENCE:
            return None
        attempt_id = semantic.get("subject_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise DataLayoutError("attempt terminal requires an attempt subject_id")
        terminals = [
            dict(row)
            for row in rows
            if row.get("event_type") in _ATTEMPT_TERMINAL_PRECEDENCE
            and row.get("subject_id") == attempt_id
        ]
        if not terminals:
            return None
        if len(terminals) > 1:
            raise LedgerConflictError(
                "ledger integrity conflict: multiple attempt terminal events for "
                f"attempt_id={attempt_id!r}"
            )
        existing = terminals[0]
        if _semantic_body(existing) == semantic:
            return existing
        existing_type = str(existing.get("event_type"))
        if existing_type == event_type:
            detail = "same terminal outcome has different semantic body"
        else:
            detail = f"existing={existing_type}; requested={event_type}"
        raise LedgerConflictError(
            "attempt terminal conflicts with existing terminal outcome for "
            f"attempt_id={attempt_id!r}: {detail}"
        )

    @staticmethod
    def _assert_stage_terminal_method_coverage_locked(
        rows: Iterable[Mapping[str, Any]],
        semantic: Mapping[str, Any],
        *,
        selected_method_names: Iterable[str] | None,
    ) -> None:
        """Require exact selected-method coverage before a stage terminal write.

        Direct typed Ledger callers remain compatible when they omit the roster.
        The authoritative occurrence settlement always supplies its frozen roster,
        so production terminal writes are checked while the append lock is held.
        """

        if selected_method_names is None:
            return
        if semantic.get("event_type") not in _STAGE_TERMINAL_PRECEDENCE:
            raise ValueError(
                "selected_method_names is allowed only for stage terminal events"
            )
        workflow_step_id = semantic.get("workflow_step_id")
        stage = semantic.get("stage")
        if not isinstance(workflow_step_id, str) or not workflow_step_id:
            raise DataLayoutError("stage terminal requires a workflow_step_id")
        if not isinstance(stage, str) or not stage:
            raise DataLayoutError("stage terminal requires a stage")
        roster = tuple(selected_method_names)
        if any(not isinstance(name, str) or not name.strip() for name in roster):
            raise ValueError("selected_method_names must contain non-empty strings")
        if len(roster) != len(set(roster)):
            raise ValueError("selected_method_names must be unique")
        disposition_rows = [
            row
            for row in rows
            if row.get("event_type") == "method_disposition_recorded"
            and row.get("workflow_step_id") == workflow_step_id
        ]
        disposition_names: list[str] = []
        for row in disposition_rows:
            if row.get("stage") != stage:
                raise LedgerConflictError(
                    "ledger integrity conflict: method disposition stage conflicts "
                    f"with workflow_step_id={workflow_step_id!r}"
                )
            method_name = (row.get("summary") or {}).get("method_name")
            if not isinstance(method_name, str) or not method_name:
                raise LedgerConflictError(
                    "ledger integrity conflict: method disposition has no valid "
                    f"method_name for workflow_step_id={workflow_step_id!r}"
                )
            disposition_names.append(method_name)
        if len(disposition_names) != len(set(disposition_names)):
            raise LedgerConflictError(
                "ledger integrity conflict: multiple method dispositions for "
                f"workflow_step_id={workflow_step_id!r}"
            )
        if set(disposition_names) != set(roster):
            missing = sorted(set(roster).difference(disposition_names))
            extra = sorted(set(disposition_names).difference(roster))
            detail: list[str] = []
            if missing:
                detail.append("missing=" + ", ".join(missing))
            if extra:
                detail.append("extra=" + ", ".join(extra))
            raise LedgerConflictError(
                "method disposition coverage does not match selected roster for "
                f"workflow_step_id={workflow_step_id!r}: {'; '.join(detail)}"
            )

    @staticmethod
    def _existing_method_disposition_locked(
        rows: Iterable[Mapping[str, Any]],
        semantic: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Enforce one exact method disposition under the append lock."""

        if semantic.get("event_type") != "method_disposition_recorded":
            return None
        workflow_step_id = semantic.get("workflow_step_id")
        stage = semantic.get("stage")
        method_name = (semantic.get("summary") or {}).get("method_name")
        if not isinstance(workflow_step_id, str) or not workflow_step_id:
            raise DataLayoutError("method disposition requires a workflow_step_id")
        if not isinstance(stage, str) or not stage:
            raise DataLayoutError("method disposition requires a stage")
        if not isinstance(method_name, str) or not method_name:
            raise DataLayoutError("method disposition requires a method_name")

        dispositions = [
            dict(row)
            for row in rows
            if row.get("event_type") == "method_disposition_recorded"
            and row.get("workflow_step_id") == workflow_step_id
            and (row.get("summary") or {}).get("method_name") == method_name
        ]
        if not dispositions:
            return None
        if len(dispositions) > 1:
            raise LedgerConflictError(
                "ledger integrity conflict: multiple method dispositions for "
                f"workflow_step_id={workflow_step_id!r}, method_name={method_name!r}"
            )
        existing = dispositions[0]
        if _semantic_body(existing) == semantic:
            return existing
        raise LedgerConflictError(
            "method disposition conflicts with existing disposition for "
            f"workflow_step_id={workflow_step_id!r}, method_name={method_name!r}"
        )

    @staticmethod
    def _existing_stage_terminal_locked(
        rows: Iterable[Mapping[str, Any]],
        semantic: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Return an exact terminal replay or reject another occurrence terminal.

        This is deliberately an append-boundary check rather than a projection
        convention.  The caller holds the exclusive file lock used to allocate
        sequences, so concurrent writers cannot both observe an unterminated
        occurrence and append a second terminal fact.  Existing immutable
        legacy conflicts remain readable and are diagnosed by
        :func:`project_stage_terminal_events`.
        """

        event_type = semantic.get("event_type")
        if event_type not in _STAGE_TERMINAL_PRECEDENCE:
            return None
        workflow_step_id = semantic.get("workflow_step_id")
        stage = semantic.get("stage")
        if not isinstance(workflow_step_id, str) or not workflow_step_id:
            raise DataLayoutError("stage terminal requires a workflow_step_id")
        if not isinstance(stage, str) or not stage:
            raise DataLayoutError("stage terminal requires a stage")

        terminals = [
            dict(row)
            for row in rows
            if row.get("event_type") in _STAGE_TERMINAL_PRECEDENCE
            and row.get("workflow_step_id") == workflow_step_id
        ]
        if not terminals:
            return None
        if len(terminals) > 1:
            raise LedgerConflictError(
                "ledger integrity conflict: multiple stage terminal events for "
                f"workflow_step_id={workflow_step_id!r}, stage={stage!r}"
            )

        existing = terminals[0]
        if _semantic_body(existing) == semantic:
            return existing
        existing_type = str(existing.get("event_type"))
        existing_stage = str(existing.get("stage"))
        if existing_stage != stage:
            detail = f"existing_stage={existing_stage}; requested_stage={stage}"
        elif existing_type == event_type:
            detail = "same terminal outcome has different semantic body"
        else:
            detail = f"existing={existing_type}; requested={event_type}"
        raise LedgerConflictError(
            "stage terminal conflicts with existing terminal outcome for "
            f"workflow_step_id={workflow_step_id!r}, stage={stage!r}: "
            f"{detail}"
        )

    def _validate_row(self, row: Any, line_number: int) -> None:
        required = {
            "schema_version",
            "sequence",
            "event_id",
            "idempotency_key",
            "session_id",
            "timestamp",
            "event_type",
            "stage",
            "workflow_step_id",
            "subject_id",
            "refs",
            "summary",
        }
        if not isinstance(row, dict) or not required.issubset(row):
            raise LedgerFormatError(f"malformed v2 ledger row at line {line_number}")
        if row["schema_version"] != LEDGER_SCHEMA_VERSION:
            raise LedgerFormatError(
                f"unsupported ledger schema at line {line_number}: "
                f"{row['schema_version']!r}"
            )
        if row["session_id"] != self.paths.session_id:
            raise LedgerFormatError(
                f"session mismatch in ledger row at line {line_number}"
            )
        if not isinstance(row["sequence"], int) or row["sequence"] < 1:
            raise LedgerFormatError(
                f"invalid sequence in ledger row at line {line_number}"
            )
        if not isinstance(row["refs"], list) or not isinstance(row["summary"], dict):
            raise LedgerFormatError(
                f"invalid refs/summary in ledger row at line {line_number}"
            )
        workflow_step_id = row["workflow_step_id"]
        if workflow_step_id is not None and (
            not isinstance(workflow_step_id, str) or not workflow_step_id
        ):
            raise LedgerFormatError(
                f"invalid workflow_step_id in ledger row at line {line_number}"
            )
        if (
            row["event_type"] in _OCCURRENCE_EVENT_TYPES
            or (row["stage"] is not None and row["event_type"] != "session_terminal")
        ) and not isinstance(workflow_step_id, str):
            raise LedgerFormatError(
                f"occurrence event lacks workflow_step_id at line {line_number}"
            )
        if _json_size(row["summary"]) > SUMMARY_MAX_BYTES:
            raise LedgerFormatError(
                f"oversized summary in ledger row at line {line_number}"
            )

    def _split_payload(
        self, payload: dict[str, Any]
    ) -> tuple[list[Any], dict[str, Any]]:
        refs: list[Any] = []
        summary: dict[str, Any] = {}
        supplied_refs = payload.pop("refs", None)
        if isinstance(supplied_refs, list):
            refs.extend(supplied_refs)
        supplied_summary = payload.pop("summary", None)
        if isinstance(supplied_summary, Mapping):
            summary.update(dict(supplied_summary))

        for key, value in payload.items():
            if key == "artifact" and isinstance(value, Mapping):
                refs.append({**dict(value), "role": "artifact"})
                continue
            if key in _COLLECTION_FACTS:
                if key == "artifacts" and isinstance(value, list):
                    for item in value:
                        if hasattr(item, "model_dump"):
                            item = item.model_dump(mode="json")
                        if isinstance(item, Mapping):
                            refs.append({**dict(item), "role": "artifact"})
                summary[f"{key[:-1]}_count"] = (
                    len(value) if isinstance(value, list) else 0
                )
                continue
            if _is_asset_ref_key(key) and value:
                refs.extend(self._asset_value_refs(key, value))
                continue
            if _is_ref_key(key) and value:
                refs.append(self._path_ref(key, value))
                continue
            if key == "relevant_refs":
                extracted = self._refs_from_nested(value, role_prefix="relevant")
                refs.extend(extracted)
                summary["relevant_ref_count"] = len(extracted)
                continue
            summary[key] = value
        return refs, summary

    def _refs_from_nested(
        self, value: Any, *, role_prefix: str
    ) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        if isinstance(value, Mapping):
            for key, nested in value.items():
                role = f"{role_prefix}.{key}"
                key_text = str(key)
                if _is_asset_ref_key(key_text):
                    refs.extend(self._asset_value_refs(role, nested))
                elif isinstance(nested, (str, Path)) and _is_ref_key(key_text):
                    try:
                        refs.append(self._path_ref(role, nested))
                    except DataLayoutError:
                        if Path(nested).is_absolute():
                            continue
                        raise
                else:
                    refs.extend(self._refs_from_nested(nested, role_prefix=role))
        elif isinstance(value, list):
            for index, nested in enumerate(value[:MAX_REFS]):
                refs.extend(
                    self._refs_from_nested(nested, role_prefix=f"{role_prefix}.{index}")
                )
        return refs

    def _asset_value_refs(self, role: str, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, (str, Path)):
            return [{"role": role, "value": _bounded_scalar(value)}]
        if isinstance(value, (list, tuple)):
            return [
                {
                    "role": f"{role}.{index}",
                    "value": _bounded_scalar(nested),
                }
                for index, nested in enumerate(value[:MAX_REFS])
                if isinstance(nested, (str, Path)) and nested
            ]
        return []

    def _normalize_refs(self, refs: Iterable[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in refs:
            if len(normalized) >= MAX_REFS:
                break
            ref = self._normalize_ref(value)
            if ref is None:
                continue
            identity = json.dumps(
                ref, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if identity in seen:
                continue
            normalized.append(ref)
            seen.add(identity)
        return normalized

    def _normalize_ref(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, (str, Path)):
            return self._path_ref("artifact_ref", value)
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if not isinstance(value, Mapping):
            return None
        ref = dict(value)
        role = _optional_text(ref.get("role"))
        path_value = ref.get("path_ref") or ref.get("file_path")
        normalized: dict[str, Any] = {}
        if role:
            normalized["role"] = role
        for key in ("artifact_id", "sha256", "artifact_type"):
            if ref.get(key) is not None:
                normalized[key] = _bounded_scalar(ref[key])
        if "sha256" not in normalized and ref.get("file_hash") is not None:
            normalized["sha256"] = _bounded_scalar(ref["file_hash"])
        size = ref.get("size_bytes")
        if size is None:
            size = ref.get("file_size_bytes")
        if size is not None:
            normalized["size_bytes"] = int(size)
        if path_value:
            normalized["path_ref"] = self._normalize_path_ref(path_value)
        elif ref.get("value") is not None:
            normalized["value"] = _bounded_scalar(ref["value"])
        if normalized.get("role") == "artifact" and normalized.get("path_ref"):
            normalized = self._indexed_artifact_ref(normalized)
        return normalized or None

    def _indexed_artifact_ref(self, ref: dict[str, Any]) -> dict[str, Any]:
        path_ref = str(ref["path_ref"])
        record = next(
            (
                candidate
                for candidate in read_artifact_records(
                    self.paths.run_dir,
                    self.paths.session_id,
                )
                if candidate.get("path_ref") == path_ref
            ),
            None,
        )
        if record is None:
            return ref

        supplied_id = ref.get("artifact_id")
        if (
            isinstance(supplied_id, str)
            and supplied_id.startswith("art_")
            and supplied_id != record["artifact_id"]
        ):
            raise DataLayoutError(
                "canonical artifact_id conflicts with the artifact index: "
                f"artifact_id={supplied_id}; path_ref={path_ref}; "
                f"indexed_artifact_id={record['artifact_id']}"
            )
        for key in ("sha256", "size_bytes", "artifact_type"):
            if key in ref and ref[key] != record[key]:
                raise DataLayoutError(
                    f"artifact {key} conflicts with the artifact index: "
                    f"artifact_id={supplied_id}; path_ref={path_ref}; "
                    f"expected={record[key]}; actual={ref[key]}"
                )
        return {
            "role": "artifact",
            "artifact_id": record["artifact_id"],
            "sha256": record["sha256"],
            "artifact_type": record["artifact_type"],
            "size_bytes": record["size_bytes"],
            "path_ref": record["path_ref"],
        }

    def _path_ref(self, role: str, value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, Mapping):
            normalized = self._normalize_ref({**dict(value), "role": role})
            if normalized is None or not normalized.get("path_ref"):
                raise ValueError(f"{role} must contain a path_ref")
            return normalized
        return {"role": role, "path_ref": self._normalize_path_ref(value)}

    def _indexed_named_artifact_ref(self, role: str, value: Any) -> dict[str, Any]:
        """Return one canonical index-backed ref with its semantic role."""

        named_ref = self._path_ref(role, value)
        artifact_ref = self._indexed_artifact_ref({**named_ref, "role": "artifact"})
        return {**artifact_ref, "role": role}

    def _normalize_path_ref(self, value: str | Path) -> str:
        path = Path(value)
        if path.is_absolute():
            return self.paths.to_ref(path)
        resolved = self.paths.resolve_ref(path)
        return self.paths.to_ref(resolved)

    def _artifact_ref(self, artifact: Any) -> dict[str, Any]:
        return self._normalize_ref({**_as_mapping(artifact), "role": "artifact"}) or {}

    def _artifact_refs(self, artifacts: Iterable[Any]) -> list[dict[str, Any]]:
        return [self._artifact_ref(artifact) for artifact in artifacts]

    def _indexed_artifact_record(self, ref: Mapping[str, Any]) -> dict[str, Any]:
        path_ref = ref.get("path_ref")
        if not isinstance(path_ref, str) or not path_ref:
            raise DataLayoutError("artifact ref has no path_ref")
        record = next(
            (
                candidate
                for candidate in read_artifact_records(
                    self.paths.run_dir, self.paths.session_id
                )
                if candidate.get("path_ref") == path_ref
            ),
            None,
        )
        if record is None:
            raise DataLayoutError(
                f"artifact ref is not present in the index: {path_ref}"
            )
        return record

    @staticmethod
    def _project_row(row: dict[str, Any]) -> dict[str, Any]:
        projected = dict(row)
        projected.update(row.get("summary") or {})
        if row.get("subject_id"):
            event_type = row.get("event_type")
            if str(event_type).startswith("attempt_"):
                projected.setdefault("attempt_id", row["subject_id"])
            if event_type in {"review_requested", "review_decision", "review_applied"}:
                projected.setdefault("review_request_id", row["subject_id"])
        for ref in row.get("refs") or []:
            if not isinstance(ref, dict):
                continue
            role = ref.get("role")
            value = ref.get("path_ref") or ref.get("value")
            if role == "artifact":
                projected.setdefault("artifact", ref)
                projected.setdefault("artifacts", []).append(ref)
            elif isinstance(role, str) and value is not None and "." not in role:
                projected.setdefault(role, value)
        return projected


def _semantic_body(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "schema_version",
            "session_id",
            "event_type",
            "stage",
            "workflow_step_id",
            "subject_id",
            "refs",
            "summary",
        )
    }


def _default_idempotency_key(semantic: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return f"event:{semantic['event_type']}:{digest[:32]}"


def _method_disposition_subject_id(workflow_step_id: str, method_name: str) -> str:
    """Return a stable bounded identity for one occurrence/method pair."""

    digest = _sha256_text(
        json.dumps(
            [workflow_step_id, method_name],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return f"method-disposition:{digest[:32]}"


def _subject_from_payload(payload: Mapping[str, Any]) -> str | None:
    for key in (
        "attempt_id",
        "review_request_id",
        "candidate_id",
        "gate_id",
        "artifact_id",
    ):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _bounded_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        bounded = _bounded_value(str(key), value)
        if bounded is None:
            continue
        candidate[str(key)] = bounded
    if _json_size(candidate) <= SUMMARY_MAX_BYTES:
        return candidate

    compact: dict[str, Any] = {
        "summary_truncated": True,
        "summary_sha256": _sha256_text(
            json.dumps(
                candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        ),
    }
    omitted: list[str] = []
    priority = (
        "outcome",
        "status",
        "reason",
        "decision",
        "gate_id",
        "method_name",
        "return_code",
        "error",
        "current_stage",
        "supervision_state",
    )
    ordered = [key for key in priority if key in candidate]
    ordered.extend(key for key in candidate if key not in ordered)
    for key in ordered:
        trial = {**compact, key: candidate[key]}
        if _json_size(trial) <= SUMMARY_MAX_BYTES - 512:
            compact[key] = candidate[key]
        else:
            omitted.append(key)
    compact["omitted_fields"] = omitted[:MAX_LIST_ITEMS]
    return compact


def _bounded_value(key: str, value: Any) -> Any:
    if isinstance(value, (str, Path)):
        limit = (
            MAX_TEXT_CHARS
            if key in {"error", "detail", "reason", "last_error"}
            else 1024
        )
        text = str(value)
        return text if len(text) <= limit else text[: limit - 1] + "…"
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        if key in {"notes", "signals", "candidates", "assets", "artifacts"}:
            return len(value)
        limit = ROUTE_TRACE_LIMIT if key == "route_trace" else MAX_LIST_ITEMS
        items = value[-limit:] if key == "route_trace" else value[:limit]
        if all(
            isinstance(item, (str, bool, int, float, Path)) or item is None
            for item in value
        ):
            return [_bounded_scalar(item) for item in items if item is not None]
        ids = [
            _bounded_scalar(
                item.get("candidate_id") or item.get("artifact_id") or item.get("id")
            )
            for item in items
            if isinstance(item, Mapping)
            and (item.get("candidate_id") or item.get("artifact_id") or item.get("id"))
        ]
        return {"item_count": len(value), "ids": ids}
    if isinstance(value, Mapping):
        if len(value) <= 8 and all(
            isinstance(nested, (str, bool, int, float, Path)) or nested is None
            for nested in value.values()
        ):
            return {
                str(nested_key): _bounded_scalar(nested)
                for nested_key, nested in value.items()
                if nested is not None
            }
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return {
            "item_count": len(value),
            "keys": [str(nested_key) for nested_key in list(value)[:MAX_LIST_ITEMS]],
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
    return _bounded_scalar(value)


def _bounded_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text if len(text) <= 1024 else text[:1023] + "…"


def _is_ref_key(key: str) -> bool:
    return key in _REF_KEYS or key.endswith("_ref") or key.endswith("_artifact_ref")


def _is_asset_ref_key(key: str) -> bool:
    return key.endswith("_asset_ref") or key.endswith("_asset_refs")


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _require_workflow_step_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise DataLayoutError("workflow_step_id must be a non-empty string")
    return value


def _workflow_step_id_from_attempt(attempt: FrozenAttemptSpec) -> str:
    return _require_workflow_step_id((attempt.metadata or {}).get("workflow_step_id"))


def _require_review_request_id(record: Mapping[str, Any]) -> str:
    value = record.get("review_request_id")
    if not isinstance(value, str) or not value:
        raise DataLayoutError("review artifact has no indexed review_request_id")
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise TypeError(f"expected an artifact/ref mapping, got {type(value).__name__}")
    return dict(value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
