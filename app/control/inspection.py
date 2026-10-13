"""Lifecycle helpers for inspecting and resuming human review gates."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.control.errors import ResumeValidationError
from app.control.projections import reduce_workflow_progress
from app.control.workflow_plan import (
    current_workflow_step_id,
    workflow_steps_from_run_config,
)
from app.review.approval import load_verified_interrupt_request
from app.review.gates import (
    find_review_gate,
)
from app.review.interrupt_context import generic_interrupt_context
from app.review.persistence import ReviewPersistenceError, load_review_request
from app.review.records import counts_by_origin, decision_records
from app.review.status import ReviewStatusProjection, project_review_history
from app.schemas.artifacts import ArtifactRef
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import (
    TopLevelState,
)
from app.storage.artifacts import read_artifact_records
from app.storage.assembly_candidates import load_executed_candidate_bundle
from app.storage.ledger import (
    Ledger,
    project_stage_terminal_events,
)
from app.storage.paths import SessionPaths


def inspect_state(
    state: TopLevelState,
    *,
    blocked_timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a compact non-mutating summary of an in-memory workflow state.

    Persistence facts (``checkpoint_persistence`` /
    ``durable_resume_available``) are intentionally absent here — they
    are properties of the LangGraph checkpoint backend, not of the
    in-memory state dict. Use :func:`inspect_session` for those.
    """

    snapshot = deepcopy(state)
    active_interrupt = snapshot.get("active_interrupt")
    request_error: str | None = None
    try:
        decision_requirements = _decision_requirements_from_state(
            snapshot,
            active_interrupt,
        )
    except ResumeValidationError as exc:
        decision_requirements = None
        request_error = str(exc)
    workflow_progress = _checkpoint_workflow_progress(snapshot, active_interrupt)
    summary: dict[str, Any] = {
        "session_id": snapshot.get("session_id"),
        # These public current fields are reduced from explicit occurrence
        # facts.  A checkpoint cursor is not evidence that earlier occurrences
        # settled, so it cannot independently establish workflow progress.
        "current_stage": workflow_progress["current_stage"],
        "current_workflow_step_id": workflow_progress["current_workflow_step_id"],
        "supervision_state": snapshot.get("supervision_state"),
        "active_interrupt": active_interrupt,
        "stage_statuses": _stage_statuses(snapshot),
        "latest_artifact_refs": _latest_artifact_refs(snapshot),
        "decision_requirements": decision_requirements,
        "workflow_progress": workflow_progress,
        # Kept only as an explicitly non-authoritative checkpoint observation
        # for callers that need to display the paused graph node.
        "checkpoint_current_stage": snapshot.get("current_stage"),
    }
    if request_error:
        summary["review_request_verification_error"] = request_error
    timeout = _blocked_timeout_status(
        active_interrupt, blocked_timeout_seconds, now=now
    )
    if timeout:
        summary["blocked_timeout"] = timeout
    return summary


def inspect_session(
    session_id: str,
    data_dir: str | Path = "data",
    *,
    blocked_timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect durable ledger/artifact records and checkpoint availability."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    session = ledger.read_session(session_id)
    events = ledger.read_events(session_id)
    artifacts = read_artifact_records(paths.run_dir, session_id)
    review_history = project_review_history(paths.run_dir, session_id)
    review_occurrences = review_history.records()
    terminal_sequence = int(
        (review_history.canonical_terminal or {}).get("sequence") or 0
    )
    authoritative_events = [
        event
        for event in events
        if terminal_sequence == 0
        or int(event.get("sequence") or 0) <= terminal_sequence
    ]
    authoritative_occurrences = [
        occurrence
        for occurrence in review_history.occurrences
        if terminal_sequence == 0
        or int(occurrence.request_sequence or 0) <= terminal_sequence
    ]
    verified_decision_sequences = {
        occurrence.decision_sequence
        for occurrence in authoritative_occurrences
        if occurrence.integrity_error is None
        and occurrence.decision_sequence is not None
    }
    verified_applied_sequences = {
        occurrence.applied_sequence
        for occurrence in authoritative_occurrences
        if occurrence.integrity_error is None
        and occurrence.applied_sequence is not None
    }
    authoritative_review_decisions = [
        event
        for event in authoritative_events
        if event.get("event_type") == "review_decision"
        and int(event.get("sequence") or 0) in verified_decision_sequences
    ]
    trusted_review_events = [
        event
        for event in authoritative_events
        if event.get("event_type") not in {"review_decision", "review_applied"}
        or (
            event.get("event_type") == "review_decision"
            and int(event.get("sequence") or 0) in verified_decision_sequences
        )
        or (
            event.get("event_type") == "review_applied"
            and int(event.get("sequence") or 0) in verified_applied_sequences
        )
    ]
    latest_review_projection = (
        authoritative_occurrences[-1] if authoritative_occurrences else None
    )
    latest_active_review = review_history.latest_unapplied
    latest_review_request = (
        dict(latest_review_projection.request_event)
        if latest_review_projection is not None
        and latest_review_projection.request_event is not None
        else None
    )
    latest_review_decision = _latest_event(
        authoritative_review_decisions,
        "review_decision",
    )
    latest_env_preflight_block = _latest_event(
        authoritative_events, "env_preflight_blocked"
    )
    latest_terminal = review_history.canonical_terminal
    review_decisions = decision_records(authoritative_review_decisions)
    decisions_by_origin = counts_by_origin(review_decisions)
    latest_review_lifecycle_event = _latest_review_lifecycle_event(
        trusted_review_events
    )
    latest_unresolved_review_request = (
        dict(latest_active_review.request_event)
        if latest_active_review is not None
        and latest_active_review.request_event is not None
        else None
    )
    latest_request_ref = (
        latest_unresolved_review_request or latest_review_request or {}
    ).get("review_request_artifact_ref")
    latest_decision_ref = (latest_review_decision or {}).get("artifact_ref")
    latest_request_artifact = _artifact_for_ref(artifacts, latest_request_ref)
    if latest_request_artifact is None and latest_request_ref:
        latest_request_artifact = {"path_ref": latest_request_ref}
    latest_decision_artifact = _artifact_for_ref(artifacts, latest_decision_ref)
    if latest_decision_artifact is None and latest_decision_ref:
        latest_decision_artifact = {"path_ref": latest_decision_ref}
    current_status_path = paths.report_path("current_status.json")
    session_summary_path = paths.report_path("session_summary.json")
    latest_operator_report = (
        str(current_status_path) if current_status_path.exists() else None
    )
    latest_final_report = (
        str(session_summary_path) if session_summary_path.exists() else None
    )

    supervision_state = "unknown"
    if latest_terminal:
        supervision_state = _terminal_supervision_state(latest_terminal.get("outcome"))
    elif latest_active_review is not None:
        supervision_state = "blocked_human"
    elif review_history.integrity_errors:
        supervision_state = "review_integrity_error"
    elif session:
        supervision_state = "running"

    db_path = paths.checkpoint_db_path
    checkpoint_persistence = "sqlite" if db_path.exists() else "memory_only"

    decision_requirements_error: str | None = None
    if latest_unresolved_review_request and (
        latest_active_review is not None and latest_active_review.accepts_new_decision
    ):
        try:
            decision_requirements = _decision_requirements_from_artifact(
                latest_request_artifact,
                paths=paths,
                expected_request_id=str(
                    latest_unresolved_review_request.get("review_request_id")
                    or latest_unresolved_review_request.get("subject_id")
                    or ""
                ),
                expected_request_hash=(
                    _event_ref(
                        latest_unresolved_review_request, "review_request_artifact_ref"
                    )
                    or {}
                ).get("sha256"),
                expected_workflow_step_id=str(
                    latest_unresolved_review_request.get("workflow_step_id") or ""
                ),
            )
        except ResumeValidationError as exc:
            decision_requirements = None
            decision_requirements_error = str(exc)
    else:
        decision_requirements = None
    active_interrupt = _durable_interrupt_projection(
        latest_unresolved_review_request,
        decision_requirements,
        review_status=latest_active_review,
    )
    route_trace = _durable_route_trace(authoritative_events)
    last_error = _latest_error(authoritative_events)
    latest_failed_report = (
        latest_operator_report if supervision_state == "failed_terminal" else None
    )
    latest_blocked_report = (
        latest_operator_report if supervision_state == "blocked_human" else None
    )
    final_assembly: dict[str, Any] | None = None
    final_assembly_integrity_error: str | None = None
    if supervision_state == "completed":
        final_assembly, final_assembly_integrity_error = (
            _latest_verified_assembly_delivery(
                authoritative_events,
                paths=paths,
                session_id=session_id,
            )
        )
    stage_terminals, stage_terminal_integrity = project_stage_terminal_events(
        authoritative_events
    )
    workflow_progress, execution = _inspect_execution_read_model(
        paths,
        stage_terminals=stage_terminals,
        stage_terminal_integrity=stage_terminal_integrity,
        supervision_state=supervision_state,
        failure_scope=(
            latest_terminal.get("failure_scope")
            if isinstance(latest_terminal, Mapping)
            else None
        ),
        latest_unresolved_review_request=latest_unresolved_review_request,
    )
    durable_available = execution.get("durable_progress") == "checkpoint"
    workflow_progress_integrity = workflow_progress.pop(
        "integrity_diagnostics",
        [],
    )
    if workflow_progress["plan_source"] == "frozen_run_config":
        current_workflow_step_id = workflow_progress["current_workflow_step_id"]
        current_stage = workflow_progress["current_stage"]
        if (
            supervision_state == "blocked_human"
            and workflow_progress["blocked_steps"] != 1
        ):
            decision_requirements = None
            active_interrupt = None
            decision_requirements_error = decision_requirements_error or (
                "blocked review request does not match the verified frozen "
                "workflow occurrence"
            )
    else:
        current_workflow_step_id = None
        current_stage = None
        if latest_unresolved_review_request is not None:
            decision_requirements = None
            active_interrupt = None
            decision_requirements_error = decision_requirements_error or (
                "blocked review request cannot be authorized without verified "
                "frozen workflow inputs"
            )

    summary: dict[str, Any] = {
        "inspect_schema_version": 1,
        "session_id": session_id,
        "session": session,
        "current_stage": current_stage,
        "current_workflow_step_id": current_workflow_step_id,
        "supervision_state": supervision_state,
        "active_interrupt": active_interrupt,
        "route_trace": route_trace,
        "last_error": last_error,
        "checkpoint_persistence": checkpoint_persistence,
        "durable_resume_available": durable_available,
        "ledger_event_count": len(events),
        "latest_events": events[-5:],
        "latest_review_request": latest_review_request,
        "latest_review_decision": latest_review_decision,
        "latest_env_preflight_block": latest_env_preflight_block,
        "latest_terminal_event": latest_terminal,
        "review_decisions": review_decisions,
        "decisions_by_origin": decisions_by_origin,
        "latest_review_lifecycle_event": latest_review_lifecycle_event,
        "latest_unresolved_review_request": latest_unresolved_review_request,
        "latest_review_status": (
            latest_active_review.to_record()
            if latest_active_review is not None
            else (
                latest_review_projection.to_record()
                if latest_review_projection is not None
                else None
            )
        ),
        "review_occurrences": review_occurrences,
        "review_status_integrity_errors": list(review_history.integrity_errors),
        "terminal_integrity": review_history.terminal_integrity,
        "stage_terminal_integrity": stage_terminal_integrity,
        "latest_review_request_artifact": latest_request_artifact,
        "latest_review_decision_artifact": latest_decision_artifact,
        "latest_operator_report": latest_operator_report,
        "latest_failed_operator_report": latest_failed_report,
        "latest_blocked_operator_report": latest_blocked_report,
        # Reports are optional, derived views. Their contents are never lifecycle truth.
        "latest_operator_report_payload": None,
        "latest_final_report": latest_final_report,
        "final_assembly": final_assembly,
        "latest_relevant_artifacts": _latest_relevant_artifacts(artifacts),
        "decision_requirements": decision_requirements,
        "artifact_count": len(artifacts),
        "workflow_progress": workflow_progress,
        "workflow_progress_integrity": workflow_progress_integrity,
        "execution": execution,
    }
    if decision_requirements_error:
        summary["review_request_verification_error"] = decision_requirements_error
    if final_assembly_integrity_error:
        summary["final_assembly_integrity_error"] = final_assembly_integrity_error
    if (
        latest_unresolved_review_request
        and latest_active_review is not None
        and latest_active_review.accepts_new_decision
        and blocked_timeout_seconds is not None
    ):
        summary["blocked_timeout"] = _durable_blocked_timeout_status(
            latest_unresolved_review_request,
            blocked_timeout_seconds,
            now=now,
        )
    if not session:
        summary["error"] = "session_not_found"
    from app.control.next_action import build_next_action

    summary["next_action"] = build_next_action(summary, data_dir=data_dir)
    return summary


def _stage_statuses(state: Mapping[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for stage, output in (state.get("stage_outputs") or {}).items():
        if isinstance(output, dict):
            statuses[stage] = str(output.get("status", "unknown"))
    return statuses


def _checkpoint_workflow_progress(
    state: Mapping[str, Any],
    active_interrupt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reduce only explicit in-checkpoint occurrence facts.

    A detached checkpoint does not retain the durable prefix of stage terminal
    events, so it cannot prove the cursor's implied completed occurrences.  An
    in-flight ``workflow_step_outcome`` is still an explicit fact and is
    included as such, but the reducer is deliberately marked degraded and
    withholds a current occurrence.  Durable :func:`inspect_session` supplies
    the complete Ledger terminal map and is authoritative.
    """

    outcome = state.get("workflow_step_outcome")
    stage_terminals: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(outcome, Mapping):
        step_id = outcome.get("workflow_step_id")
        stage = outcome.get("stage")
        terminal_outcome = outcome.get("outcome")
        event_type_by_outcome = {
            "completed": "stage_completed",
            "skipped": "stage_skipped",
            "failed": "stage_failed",
        }
        event_type = (
            event_type_by_outcome.get(terminal_outcome)
            if isinstance(terminal_outcome, str)
            else None
        )
        if isinstance(step_id, str) and isinstance(stage, str) and event_type:
            stage_terminals[(step_id, stage)] = {"event_type": event_type}

    checkpoint_only = {
        "kind": "checkpoint_only_stage_terminal_prefix",
        "detail": (
            "checkpoint state does not contain the durable occurrence-terminal "
            "prefix required to determine current workflow progress"
        ),
    }
    try:
        reduction = reduce_workflow_progress(
            state.get("workflow_steps"),
            stage_terminals=stage_terminals,
            active_gate=(
                active_interrupt
                if state.get("supervision_state") == "blocked_human"
                and isinstance(active_interrupt, Mapping)
                else None
            ),
            session_outcome=(
                "completed"
                if state.get("supervision_state") == "completed"
                else "failed"
                if state.get("supervision_state") == "failed_terminal"
                else None
            ),
            terminal_integrity=(checkpoint_only,),
        )
    except (TypeError, ValueError):
        return {
            "plan_source": "unavailable",
            "total_steps": None,
            "completed_steps": None,
            "skipped_steps": None,
            "failed_steps": None,
            "blocked_steps": None,
            "unfinished_steps": None,
            "current_workflow_step_id": None,
            "current_stage": None,
            "projection_authority": "unavailable",
            "integrity_diagnostics": [checkpoint_only],
        }
    return {
        "plan_source": "checkpoint_only",
        **reduction,
    }


def _validate_active_interrupt_workflow_step(
    state: Mapping[str, Any],
    active_interrupt: Mapping[str, Any],
) -> str:
    """Require a blocked request to target exactly the current frozen occurrence."""

    interrupt_step_id = active_interrupt.get("workflow_step_id")
    if not isinstance(interrupt_step_id, str) or not interrupt_step_id:
        raise ResumeValidationError("resume requires active_interrupt.workflow_step_id")
    try:
        current_step_id = current_workflow_step_id(
            cast(TopLevelState, state),
            expected_stage=str(active_interrupt.get("stage") or ""),
        )
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "resume requires a valid frozen workflow step for active_interrupt"
        ) from exc
    if interrupt_step_id != current_step_id:
        raise ResumeValidationError(
            "active_interrupt.workflow_step_id does not match the current frozen "
            "workflow step"
        )
    return current_step_id


def _latest_artifact_refs(state: Mapping[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("asset_registry_ref", "stage_plan_ref"):
        if state.get(key):
            refs[key] = state[key]
    for stage, output in (state.get("stage_outputs") or {}).items():
        if not isinstance(output, dict):
            continue
        stage_refs = {
            key: value
            for key, value in output.items()
            if key.endswith("_ref") or key.endswith("_artifact_ref")
        }
        if stage_refs:
            refs[stage] = stage_refs
    return refs


def _review_request_decision_context(
    record: Mapping[str, Any],
    *,
    request_ref: Any,
    request_hash: Any,
    review_request_artifact_ref: Any,
) -> dict[str, Any]:
    """Project one verified request record into the decision-context contract."""

    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("review request artifact payload is invalid")
    return {
        **payload,
        **dict(record.get("bindings") or {}),
        "stage": record.get("stage"),
        "workflow_step_id": record.get("workflow_step_id"),
        "gate_id": record.get("gate_id"),
        "review_request_id": record.get("review_request_id"),
        "request_ref": request_ref,
        "request_hash": request_hash,
        "review_request_artifact_ref": review_request_artifact_ref,
    }


def _decision_requirements_from_state(
    state: Mapping[str, Any],
    active_interrupt: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not active_interrupt:
        return None
    _validate_active_interrupt_workflow_step(state, active_interrupt)
    _ref, record = load_verified_interrupt_request(state, active_interrupt)
    context = _review_request_decision_context(
        record,
        request_ref=active_interrupt.get("request_ref"),
        request_hash=active_interrupt.get("request_hash"),
        review_request_artifact_ref=(
            (active_interrupt.get("request_ref") or {}).get("path_ref")
            if isinstance(active_interrupt.get("request_ref"), dict)
            else None
        ),
    )
    return _decision_requirements(context)


def _decision_requirements(
    active_interrupt: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not active_interrupt:
        return None
    stage = active_interrupt.get("stage")
    gate = find_review_gate(stage, active_interrupt.get("gate_id"))
    decisions_allowed = active_interrupt.get("decisions_allowed")
    if decisions_allowed is None and gate is not None:
        decisions_allowed = list(gate.decisions_allowed)

    builder = gate.load_interrupt_context_builder() if gate is not None else None
    if builder is not None:
        extra = builder(active_interrupt)
    else:
        extra = generic_interrupt_context(active_interrupt)

    return _with_review_requirements(
        active_interrupt,
        {
            "stage": stage,
            "gate_id": active_interrupt.get("gate_id")
            or (gate.gate_id if gate else None),
            "decisions_allowed": decisions_allowed or [],
            **extra,
            "requires_structured_resume": True,
        },
    )


_PARAMETER_APPROVAL_BINDING_FIELDS = (
    "attempt_id",
    "task",
    "previous_attempt_id",
    "recommendation_hash",
)

_PARAMETER_APPROVAL_CONTEXT_FIELDS = (
    "recommendation_ref",
    "parameter_diff",
    "rationale",
    "failure_facts",
    "evidence_refs",
)


def _parameter_approval_resume_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("gate_id") != "parameter_approval":
        return {}
    return {
        key: payload.get(key)
        for key in _PARAMETER_APPROVAL_BINDING_FIELDS
        if key in payload
    }


def _runtime_proposal_requirements(gate: Any) -> dict[str, Any]:
    "Describe what this existing gate proposes without implying a recommender exists."

    proposal = {
        "kind": gate.proposal_kind,
        "label": gate.proposal_label or gate.gate_id.replace("_", " "),
        "parameter_recommendation_status": gate.parameter_recommendation_status,
        "human_confirmation_required": True,
    }
    if gate.related_parameter_gate is not None:
        proposal["related_parameter_gate"] = {
            "stage": gate.related_parameter_gate[0],
            "gate_id": gate.related_parameter_gate[1],
        }
    if gate.parameter_recommendation_notes:
        proposal["parameter_recommendation_notes"] = list(
            gate.parameter_recommendation_notes
        )
    return proposal


def _with_review_requirements(
    active_interrupt: dict[str, Any], requirements: dict[str, Any]
) -> dict[str, Any]:
    enriched = dict(requirements)
    gate = find_review_gate(
        active_interrupt.get("stage"), active_interrupt.get("gate_id")
    )
    if gate is not None:
        enriched["runtime_proposal"] = _runtime_proposal_requirements(gate)
    resume_metadata = _parameter_approval_resume_metadata(active_interrupt)
    if resume_metadata:
        enriched["resume_metadata"] = resume_metadata
        for key in _PARAMETER_APPROVAL_CONTEXT_FIELDS:
            if key in active_interrupt:
                enriched[key] = active_interrupt.get(key)
    for key in (
        "workflow_step_id",
        "reason",
        "notes",
        "auto_decidable",
        "max_blocked_seconds",
        "review_request_artifact_ref",
        "review_request_id",
        "request_ref",
        "request_hash",
    ):
        if key in active_interrupt:
            enriched[key] = active_interrupt.get(key)
    return enriched


def _blocked_timeout_status(
    active_interrupt: Mapping[str, Any] | None,
    blocked_timeout_seconds: float | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not active_interrupt or blocked_timeout_seconds is None:
        return None
    if blocked_timeout_seconds < 0:
        return {
            "configured_seconds": blocked_timeout_seconds,
            "expired": False,
            "reason": "wait_indefinitely",
        }
    blocked_at_raw = active_interrupt.get("blocked_at")
    if not blocked_at_raw:
        return {
            "configured_seconds": blocked_timeout_seconds,
            "expired": False,
            "reason": "blocked_at_missing",
        }
    try:
        blocked_at = datetime.fromisoformat(str(blocked_at_raw))
    except ValueError:
        return {
            "configured_seconds": blocked_timeout_seconds,
            "expired": False,
            "reason": "blocked_at_invalid",
        }
    if blocked_at.tzinfo is None:
        blocked_at = blocked_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed = (current - blocked_at).total_seconds()
    return {
        "configured_seconds": blocked_timeout_seconds,
        "elapsed_seconds": elapsed,
        "expired": elapsed >= blocked_timeout_seconds,
    }


def _durable_blocked_timeout_status(
    review_request_event: dict[str, Any],
    blocked_timeout_seconds: float,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    active_interrupt = {"blocked_at": review_request_event.get("timestamp")}
    timeout = _blocked_timeout_status(
        active_interrupt, blocked_timeout_seconds, now=now
    )
    return timeout or {
        "configured_seconds": blocked_timeout_seconds,
        "expired": False,
        "reason": "timestamp_missing",
    }


def _latest_review_lifecycle_event(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") in {
            "review_requested",
            "review_decision",
            "review_applied",
        }:
            return event
    return None


def _latest_event(
    events: list[dict[str, Any]], event_type: str
) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _artifact_for_ref(
    artifacts: list[dict[str, Any]],
    path_ref: Any,
) -> dict[str, Any] | None:
    if not path_ref:
        return None
    target = str(path_ref)
    for artifact in reversed(artifacts):
        candidate = artifact.get("path_ref") or artifact.get("file_path")
        if str(candidate) == target:
            return artifact
    return None


def _event_ref(event: dict[str, Any], role: str) -> dict[str, Any] | None:
    for ref in event.get("refs") or []:
        if isinstance(ref, dict) and ref.get("role") == role:
            return ref
    return None


def _latest_verified_assembly_delivery(
    events: list[dict[str, Any]],
    *,
    paths: SessionPaths,
    session_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Project the latest canonically completed Assembly delivery."""

    terminals, diagnostics = project_stage_terminal_events(events)
    for event in reversed(events):
        if event.get("event_type") != "resource_delivered":
            continue
        summary = event.get("summary")
        if (
            not isinstance(summary, Mapping)
            or summary.get("resource_type") != "assembly"
        ):
            continue
        stage = event.get("stage")
        workflow_step_id = event.get("workflow_step_id")
        if not isinstance(stage, str) or not isinstance(workflow_step_id, str):
            return None, "delivered assembly is missing its occurrence identity"
        if any(
            diagnostic.get("workflow_step_id") == workflow_step_id
            for diagnostic in diagnostics
        ):
            return None, "delivered assembly has non-authoritative terminal history"
        terminal = terminals.get((workflow_step_id, stage))
        if terminal is None or terminal.get("event_type") != "stage_completed":
            return None, "delivered assembly occurrence is not canonically completed"
        if events.index(event) >= events.index(terminal):
            return None, "delivered assembly was not settled before stage completion"
        matching_deliveries = [
            candidate
            for candidate in events
            if candidate.get("event_type") == "resource_delivered"
            and candidate.get("stage") == stage
            and candidate.get("workflow_step_id") == workflow_step_id
            and isinstance(candidate.get("summary"), Mapping)
            and candidate["summary"].get("resource_type") == "assembly"
        ]
        if len(matching_deliveries) != 1:
            return None, "completed assembly occurrence must have exactly one delivery"
        if summary.get("delivery_mode") not in {"materialized", "reused"}:
            return None, "delivered assembly has an invalid delivery mode"
        candidate_event_ref = _event_ref(event, "candidate_bundle_ref")
        primary_event_ref = _event_ref(event, "primary_output_ref")
        try:
            candidate_ref = _artifact_ref_from_event_ref(candidate_event_ref)
            primary_ref = _artifact_ref_from_event_ref(primary_event_ref)
            verified = load_executed_candidate_bundle(
                paths.run_dir,
                session_id,
                candidate_ref,
            )
            if verified.primary_output_ref != primary_ref:
                raise ValueError(
                    "delivery primary output differs from its executed candidate"
                )
        except (OSError, ValueError) as exc:
            return None, f"delivered assembly artifact verification failed: {exc}"
        return (
            {
                "stage": stage,
                "workflow_step_id": workflow_step_id,
                "delivery_mode": summary.get("delivery_mode"),
                "candidate_bundle_ref": candidate_ref.model_dump(mode="json"),
                "candidate_bundle_path": str(verified.path),
                "primary_output_ref": primary_ref.model_dump(mode="json"),
                "primary_output_path": str(verified.primary_output_path),
            },
            None,
        )
    return None, None


def _artifact_ref_from_event_ref(value: object) -> ArtifactRef:
    if not isinstance(value, Mapping):
        raise ValueError("resource delivery is missing an artifact ref")
    return ArtifactRef.model_validate(
        {key: value[key] for key in ArtifactRef.model_fields if key in value}
    )


def _latest_relevant_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relevant_prefixes = (
        "assembly_review.",
        "hic_scaffolding.",
    )
    relevant: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in reversed(artifacts):
        artifact_type = str(artifact.get("artifact_type", ""))
        if not artifact_type.startswith(relevant_prefixes):
            continue
        if artifact_type in seen:
            continue
        relevant.append(artifact)
        seen.add(artifact_type)
        if len(relevant) >= 8:
            break
    return list(reversed(relevant))


def _terminal_supervision_state(outcome: Any) -> str:
    normalized = str(outcome or "unknown")
    if normalized in {"completed", "succeeded", "success"}:
        return "completed"
    if normalized in {"failed", "failed_terminal", "error"}:
        return "failed_terminal"
    return normalized


def _inspect_execution_read_model(
    paths: SessionPaths,
    *,
    stage_terminals: Mapping[tuple[str, str], Mapping[str, Any]],
    stage_terminal_integrity: list[dict[str, Any]],
    supervision_state: str,
    failure_scope: object,
    latest_unresolved_review_request: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project verified-plan progress plus per-session execution evidence.

    The frozen RunConfig is the sole denominator authority.  If it cannot be
    verified, inspection reports all plan-derived values as unavailable rather
    than reconstructing a plan from Ledger events or the stage registry.
    """

    from app.storage.frozen_inputs import (
        frozen_execution_lock_held,
        frozen_execution_progress,
        validate_frozen_inputs,
    )

    try:
        lock_held: bool | None = frozen_execution_lock_held(paths)
    except OSError:
        lock_held = None

    unavailable_progress = {
        "plan_source": "unavailable",
        "total_steps": None,
        "completed_steps": None,
        "skipped_steps": None,
        "failed_steps": None,
        "blocked_steps": None,
        "unfinished_steps": None,
        "current_workflow_step_id": None,
        "current_stage": None,
    }
    try:
        bundle = validate_frozen_inputs(paths)
        steps = workflow_steps_from_run_config(bundle.run_config)
    except (OSError, ValueError):
        return unavailable_progress, {
            "frozen_inputs_status": "invalid_or_missing",
            "durable_progress": "unavailable",
            "execution_lock_held": lock_held,
        }

    try:
        durable_progress: str = frozen_execution_progress(paths)
    except (OSError, ValueError):
        durable_progress = "unavailable"

    active_gate = (
        latest_unresolved_review_request
        if supervision_state == "blocked_human"
        else None
    )
    reduction = reduce_workflow_progress(
        steps,
        stage_terminals=stage_terminals,
        active_gate=active_gate,
        session_outcome=supervision_state,
        failure_scope=(failure_scope if isinstance(failure_scope, str) else None),
        terminal_integrity=stage_terminal_integrity,
    )
    return (
        {
            "plan_source": "frozen_run_config",
            **reduction,
        },
        {
            "frozen_inputs_status": "verified",
            "durable_progress": durable_progress,
            "execution_lock_held": lock_held,
        },
    )


def _durable_route_trace(events: list[dict[str, Any]]) -> list[str]:
    """Project durable route entries as the same step-qualified trace as state."""

    trace: list[str] = []
    for event in events:
        if event.get("event_type") != "stage_entered" or not event.get("stage"):
            continue
        step_id = event.get("workflow_step_id")
        node = event.get("route_node") or event.get("stage")
        if isinstance(step_id, str) and step_id:
            trace.append(f"{step_id}:{node}")
        else:
            trace.append(str(node))
    return trace[-ROUTE_TRACE_LIMIT:]


def _latest_error(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("error"):
            return str(event["error"])
        if event.get("detail") and event.get("event_type") == "env_preflight_blocked":
            return str(event["detail"])
    return None


def _durable_interrupt_projection(
    request: dict[str, Any] | None,
    requirements: dict[str, Any] | None,
    *,
    review_status: ReviewStatusProjection | None = None,
) -> dict[str, Any] | None:
    if request is None:
        return None
    projected = {
        "stage": request.get("stage"),
        "workflow_step_id": request.get("workflow_step_id"),
        "review_request_id": request.get("review_request_id")
        or request.get("subject_id"),
        "reason": request.get("reason"),
        "review_request_artifact_ref": request.get("review_request_artifact_ref"),
    }
    if requirements:
        projected["gate_id"] = requirements.get("gate_id")
        projected["decisions_allowed"] = list(
            requirements.get("decisions_allowed") or []
        )
    if review_status is not None:
        projected["review_status"] = review_status.status
        if review_status.integrity_error is not None:
            projected["integrity_error"] = review_status.integrity_error
    return {key: value for key, value in projected.items() if value is not None}


def _decision_requirements_from_artifact(
    artifact: dict[str, Any] | None,
    *,
    paths: SessionPaths,
    expected_request_id: str,
    expected_request_hash: str | None = None,
    expected_workflow_step_id: str | None = None,
) -> dict[str, Any] | None:
    """Load requirements only after validating the indexed immutable ref."""
    if not artifact:
        raise ResumeValidationError("review request artifact is missing from the index")
    path_ref = artifact.get("path_ref")
    sha256 = artifact.get("sha256")
    size_bytes = artifact.get("size_bytes")
    artifact_id = artifact.get("artifact_id")
    artifact_type = artifact.get("artifact_type")
    try:
        ref = ArtifactRef.model_validate(
            {
                "schema_version": 2,
                "artifact_id": artifact_id,
                "path_ref": path_ref,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "artifact_type": artifact_type,
            }
        )
    except Exception as exc:
        raise ResumeValidationError(
            "review request artifact index ref is invalid"
        ) from exc
    if expected_request_hash and ref.sha256 != expected_request_hash:
        raise ResumeValidationError(
            "ledger review request hash does not match the artifact index"
        )
    try:
        record = load_review_request(
            paths.run_dir,
            ref,
            session_id=paths.session_id,
        )
    except ReviewPersistenceError as exc:
        raise ResumeValidationError(
            f"review request verification failed: {exc}"
        ) from exc
    if record.get("review_request_id") != expected_request_id:
        raise ResumeValidationError(
            "ledger review_request_id does not match the request artifact"
        )
    if (
        expected_workflow_step_id
        and record.get("workflow_step_id") != expected_workflow_step_id
    ):
        raise ResumeValidationError(
            "ledger workflow_step_id does not match the request artifact"
        )
    context = _review_request_decision_context(
        record,
        request_ref=ref.model_dump(mode="json"),
        request_hash=ref.sha256,
        review_request_artifact_ref=ref.path_ref,
    )
    return _decision_requirements(context)
