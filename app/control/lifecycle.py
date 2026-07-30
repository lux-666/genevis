"""Lifecycle helpers for inspecting and resuming human review gates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from app.control.errors import (
    ResumeValidationError,
    UnsupportedLifecycleOperationError,
)
from app.review.approval import ensure_decision_allowed, load_verified_interrupt_request
from app.review.base import ResumeReviewRequest
from app.review.gates import (
    ReviewGateDeclaration,
    apply_review_gate_decision,
    apply_review_gate_timeout,
    find_review_gate,
    get_review_gate,
    registered_review_stage_label,
)
from app.review.interrupt_context import generic_interrupt_context
from app.review.persistence import ReviewPersistenceError, load_review_request
from app.review.records import counts_by_origin, decision_records
from app.schemas.artifacts import ArtifactRef
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.schemas.state import ActiveInterrupt, StateUpdate, TopLevelState
from app.storage.artifacts import read_artifact_records
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths


def finalize_terminal_state(
    graph: Any,
    config: RunnableConfig,
    state: dict[str, Any],
    session_id: str,
    data_dir: str | Path,
    checkpointer: str,
    *,
    report_kind: Literal["operator", "final"],
) -> str | None:
    """No-op active states; report terminals and soft-fail SQLite compaction."""
    if state.get("supervision_state") not in {"completed", "failed_terminal"}:
        return None

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    terminal_state = {**state, "session_id": session_id, "data_dir": str(paths.run_dir)}

    from app.reports import review_report

    if report_kind == "operator":
        refs = ledger.references_from(terminal_state, role_prefix="terminal.failure")
        ledger.record_session_terminal(
            session_id,
            outcome="failed",
            stage=terminal_state.get("current_stage"),
            error=terminal_state.get("last_error"),
            refs=refs,
            idempotency_key="session-terminal:failed",
        )
        review_report.write_operator_report(paths.run_dir, session_id, terminal_state)
    else:
        review_report.write_final_review_report(paths.run_dir, session_id)

    saver = getattr(graph, "checkpointer", None)
    if checkpointer != "sqlite" or saver is None:
        return None

    try:
        checkpoint_state = terminal_state
        if report_kind == "operator":
            graph.update_state(
                config,
                {
                    "supervision_state": "failed_terminal",
                    "last_error": terminal_state.get("last_error"),
                    "active_interrupt": None,
                },
            )
            checkpoint_values = getattr(graph.get_state(config), "values", None)
            if not isinstance(checkpoint_values, dict):
                raise RuntimeError("persisted terminal checkpoint has no state values")
            checkpoint_state = {**terminal_state, **checkpoint_values}
        from app.graph.checkpoint_compaction import compact_terminal_checkpoint

        compacted = compact_terminal_checkpoint(saver, config, checkpoint_state)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        ledger.record_event(
            session_id,
            "terminal_compaction_skipped",
            {"error": error},
            idempotency_key="terminal-compaction:skipped",
        )
        return error

    if compacted:
        ledger.record_event(
            session_id,
            "terminal_compaction_completed",
            {"checkpoint_count": 1},
            idempotency_key="terminal-compaction:completed",
        )
    return None


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
    summary: dict[str, Any] = {
        "session_id": snapshot.get("session_id"),
        "current_stage": snapshot.get("current_stage"),
        "supervision_state": snapshot.get("supervision_state"),
        "active_interrupt": active_interrupt,
        "stage_statuses": _stage_statuses(snapshot),
        "latest_artifact_refs": _latest_artifact_refs(snapshot),
        "decision_requirements": decision_requirements,
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
    latest_review_request = _latest_event(events, "review_requested")
    latest_review_decision = _latest_event(events, "review_decision")
    latest_env_preflight_block = _latest_event(events, "env_preflight_blocked")
    latest_terminal = _latest_event(events, "session_terminal")
    review_decisions = decision_records(events)
    decisions_by_origin = counts_by_origin(review_decisions)
    latest_review_lifecycle_event = _latest_review_lifecycle_event(events)
    latest_unresolved_review_request = _latest_unresolved_review_request(events)
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
    latest_function_bundle = _latest_artifact(
        artifacts, lambda a: a.get("artifact_type") == "function_annotation.bundle"
    )
    current_status_path = paths.report_path("current_status.json")
    session_summary_path = paths.report_path("session_summary.json")
    latest_operator_report = (
        str(current_status_path) if current_status_path.exists() else None
    )
    latest_final_report = (
        str(session_summary_path) if session_summary_path.exists() else None
    )

    supervision_state = "unknown"
    if latest_unresolved_review_request and (
        latest_terminal is None
        or int(latest_unresolved_review_request.get("sequence") or 0)
        > int(latest_terminal.get("sequence") or 0)
    ):
        supervision_state = "blocked_human"
    elif latest_terminal:
        supervision_state = _terminal_supervision_state(latest_terminal.get("outcome"))
    elif (
        latest_review_lifecycle_event
        and latest_review_lifecycle_event.get("event_type") == "review_decision"
    ):
        supervision_state = "review_decision_recorded"
    elif session:
        supervision_state = "running"

    db_path = paths.checkpoint_db_path
    durable_available = db_path.exists()
    checkpoint_persistence = "sqlite" if durable_available else "memory_only"

    decision_requirements_error: str | None = None
    if latest_unresolved_review_request:
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
            )
        except ResumeValidationError as exc:
            decision_requirements = None
            decision_requirements_error = str(exc)
    else:
        decision_requirements = None
    active_interrupt = _durable_interrupt_projection(
        latest_unresolved_review_request,
        decision_requirements,
    )
    current_stage = _latest_stage(events)
    route_trace = [
        str(event.get("route_node") or event.get("stage"))
        for event in events
        if event.get("event_type") == "stage_entered" and event.get("stage")
    ][-ROUTE_TRACE_LIMIT:]
    last_error = _latest_error(events)
    latest_failed_report = (
        latest_operator_report if supervision_state == "failed_terminal" else None
    )
    latest_blocked_report = (
        latest_operator_report if supervision_state == "blocked_human" else None
    )

    summary: dict[str, Any] = {
        "session_id": session_id,
        "session": session,
        "current_stage": current_stage,
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
        "latest_review_request_artifact": latest_request_artifact,
        "latest_review_decision_artifact": latest_decision_artifact,
        "latest_operator_report": latest_operator_report,
        "latest_failed_operator_report": latest_failed_report,
        "latest_blocked_operator_report": latest_blocked_report,
        # Reports are optional, derived views. Their contents are never lifecycle truth.
        "latest_operator_report_payload": None,
        "latest_final_report": latest_final_report,
        "latest_function_annotation_bundle": latest_function_bundle,
        "latest_relevant_artifacts": _latest_relevant_artifacts(artifacts),
        "decision_requirements": decision_requirements,
        "artifact_count": len(artifacts),
    }
    if decision_requirements_error:
        summary["review_request_verification_error"] = decision_requirements_error
    if latest_unresolved_review_request and blocked_timeout_seconds is not None:
        summary["blocked_timeout"] = _durable_blocked_timeout_status(
            latest_unresolved_review_request,
            blocked_timeout_seconds,
            now=now,
        )
    if not session:
        summary["error"] = "session_not_found"
    summary["next_action"] = build_next_action(summary, data_dir=data_dir)
    return summary


def resume_review_state(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    blocked_timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> StateUpdate:
    """Apply a validated human review decision to an in-memory blocked state."""

    active_interrupt, request_record = _validated_resume_context(state, request)
    ensure_decision_allowed(request_record, request)
    blocked_timeout_seconds, timeout_source = _resolve_blocked_timeout_seconds(
        request_record,
        blocked_timeout_seconds,
    )

    timeout = _blocked_timeout_status(
        active_interrupt, blocked_timeout_seconds, now=now
    )
    if timeout and timeout.get("expired"):
        timeout = _with_timeout_source(timeout, timeout_source)
        return _timeout_failed_update(state, request, timeout)

    try:
        return apply_review_gate_decision(state, request)
    except KeyError as exc:
        raise ResumeValidationError(
            f"Unsupported resume stage: {request.stage}"
        ) from exc


def validate_resume_review_state(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> None:
    """Validate blocked-state identity and allowed decision without side effects."""

    _active_interrupt, request_record = _validated_resume_context(state, request)
    ensure_decision_allowed(request_record, request)


def reject_unsupported_lifecycle_operation(operation: str) -> None:
    if operation in {"retry", "rerun", "rebuild", "rebuild-from"}:
        raise UnsupportedLifecycleOperationError(
            f"{operation} is not a resume operation and is not implemented in "
            "the current lifecycle contract."
        )


def _validate_common_resume_state(
    state: TopLevelState, request: ResumeReviewRequest
) -> None:
    _active_interrupt, request_record = _validated_resume_context(state, request)
    ensure_decision_allowed(request_record, request)


def _validate_resume_identity(
    state: TopLevelState, request: ResumeReviewRequest
) -> ActiveInterrupt:
    active_interrupt, _request_record = _validated_resume_context(state, request)
    return active_interrupt


def _validated_resume_context(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> tuple[ActiveInterrupt, dict[str, Any]]:
    if state.get("session_id") != request.session_id:
        raise ResumeValidationError("resume session_id does not match blocked state")
    if state.get("supervision_state") != "blocked_human":
        raise ResumeValidationError("resume requires supervision_state blocked_human")
    active_interrupt = state.get("active_interrupt")
    if not active_interrupt:
        raise ResumeValidationError("resume requires an active_interrupt")
    if active_interrupt.get("stage") != request.stage:
        raise ResumeValidationError(
            "resume stage does not match active_interrupt.stage"
        )
    active_gate_id = active_interrupt.get("gate_id")
    if (
        active_gate_id == "parameter_approval"
        and request.gate_id != "parameter_approval"
    ):
        raise ResumeValidationError(
            "parameter approval resume requires explicit gate_id=parameter_approval"
        )
    if request.gate_id and active_gate_id and request.gate_id != active_gate_id:
        raise ResumeValidationError(
            "resume gate_id does not match active_interrupt.gate_id"
        )
    interrupt_request_id = active_interrupt.get("review_request_id")
    interrupt_request_hash = active_interrupt.get("request_hash")
    interrupt_request_ref = active_interrupt.get("request_ref")
    missing = [
        key
        for key, value in (
            ("review_request_id", interrupt_request_id),
            ("request_hash", interrupt_request_hash),
            ("request_ref", interrupt_request_ref),
        )
        if not value
    ]
    if missing:
        raise ResumeValidationError(
            "v2 resume requires compact request identity fields: " + ", ".join(missing)
        )
    if request.review_request_id is None or request.review_request_hash is None:
        raise ResumeValidationError(
            "v2 resume decision requires review_request_id and review_request_hash"
        )
    if request.review_request_id != interrupt_request_id:
        raise ResumeValidationError(
            "resume review_request_id does not match active_interrupt.review_request_id"
        )
    if request.review_request_hash != interrupt_request_hash:
        raise ResumeValidationError(
            "resume review_request_hash does not match active_interrupt.request_hash"
        )
    _request_ref, request_record = load_verified_interrupt_request(
        state,
        active_interrupt,
    )
    return active_interrupt, request_record


def _resolve_blocked_timeout_seconds(
    request_record: dict[str, Any],
    blocked_timeout_seconds: float | None,
) -> tuple[float | None, dict[str, Any]]:
    policy_timeout = _review_policy_outcome_timeout_seconds(request_record)
    if blocked_timeout_seconds is None:
        return (
            policy_timeout,
            {"source": "review_request.review_policy_outcome.max_blocked_seconds"}
            if policy_timeout is not None
            else {},
        )

    source: dict[str, Any] = {"source": "blocked_timeout_seconds"}
    if policy_timeout is not None and policy_timeout != blocked_timeout_seconds:
        source.update(
            {
                "warning": (
                    "blocked_timeout_seconds overrides "
                    "review_request.review_policy_outcome.max_blocked_seconds"
                ),
                "review_policy_outcome_max_blocked_seconds": policy_timeout,
            }
        )
    return blocked_timeout_seconds, source


def _review_policy_outcome_timeout_seconds(
    request_record: dict[str, Any],
) -> float | None:
    payload = request_record.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    review_policy_outcome = payload.get("review_policy_outcome") or {}
    if not isinstance(review_policy_outcome, dict):
        return None
    timeout = review_policy_outcome.get("max_blocked_seconds")
    if timeout is None:
        return None
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ResumeValidationError(
            "review_policy_outcome.max_blocked_seconds must be numeric"
        )
    return float(timeout)


def _with_timeout_source(
    timeout: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    if not source:
        return timeout
    return {**timeout, **source}


def _timeout_failed_update(
    state: TopLevelState, request: ResumeReviewRequest, timeout: dict[str, Any]
) -> StateUpdate:
    """Handle timeout through the review-gate registry, then fall back to stop."""
    try:
        timeout_update = apply_review_gate_timeout(state, request, timeout)
    except KeyError as exc:
        raise ResumeValidationError(
            f"Unsupported timeout stage: {request.stage}"
        ) from exc
    if timeout_update is not None:
        return timeout_update

    # Fall back to stop_human_review for other gates or when no recommendation
    timeout_request = request.model_copy(
        update={
            "decision": "stop_human_review",
            "notes": [*request.notes, "blocked timeout expired"],
            "metadata": {**request.metadata, "blocked_timeout": timeout},
        }
    )
    try:
        update = apply_review_gate_decision(state, timeout_request)
    except KeyError as exc:
        raise ResumeValidationError(
            f"Unsupported timeout stage: {request.stage}"
        ) from exc
    update["last_error"] = f"{request.stage} blocked timeout expired"
    return update


def terminal_failure_update(
    state: TopLevelState,
    stage: str,
    error: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    update = {
        "current_stage": stage,
        "supervision_state": "failed_terminal",
        "active_interrupt": None,
        "last_error": error,
        "stage_outputs": state.get("stage_outputs") or {},
    }
    if metadata:
        update.update(metadata)
    return update


def timeout_terminal_update(
    state: TopLevelState, request: ResumeReviewRequest, timeout: dict[str, Any]
) -> dict[str, Any]:
    return terminal_failure_update(
        state,
        request.stage,
        f"{request.stage} blocked timeout expired",
        metadata={"blocked_timeout": timeout},
    )


def _stage_statuses(state: Mapping[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for stage, output in (state.get("stage_outputs") or {}).items():
        if isinstance(output, dict):
            statuses[stage] = str(output.get("status", "unknown"))
    return statuses


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


def _decision_requirements_from_state(
    state: Mapping[str, Any],
    active_interrupt: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not active_interrupt:
        return None
    _ref, record = load_verified_interrupt_request(state, active_interrupt)
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("review request artifact payload is invalid")
    context = {
        **payload,
        **dict(record.get("bindings") or {}),
        "stage": record.get("stage"),
        "gate_id": record.get("gate_id"),
        "review_request_id": record.get("review_request_id"),
        "request_ref": active_interrupt.get("request_ref"),
        "request_hash": active_interrupt.get("request_hash"),
        "review_request_artifact_ref": (
            (active_interrupt.get("request_ref") or {}).get("path_ref")
            if isinstance(active_interrupt.get("request_ref"), dict)
            else None
        ),
    }
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

    return _with_review_policy_requirements(
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


def _with_review_policy_requirements(
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
        "auto_decidable",
        "pending_human_confirmation",
        "review_request_artifact_ref",
        "review_request_id",
        "request_ref",
        "request_hash",
    ):
        if key in active_interrupt:
            enriched[key] = active_interrupt.get(key)
    policy = active_interrupt.get("review_policy")
    if isinstance(policy, dict) and policy.get("mode"):
        enriched["review_policy.mode"] = policy.get("mode")
    return enriched


def _blocked_timeout_status(
    active_interrupt: Mapping[str, Any] | None,
    blocked_timeout_seconds: float | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not active_interrupt or blocked_timeout_seconds is None:
        return None
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
        if event.get("event_type") in {"review_requested", "review_decision"}:
            return event
    return None


def _latest_unresolved_review_request(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    decided_ids = {
        str(event.get("review_request_id") or event.get("subject_id"))
        for event in events
        if event.get("event_type") == "review_decision"
    }
    for event in reversed(events):
        if event.get("event_type") != "review_requested":
            continue
        request_id = str(event.get("review_request_id") or event.get("subject_id"))
        if request_id not in decided_ids:
            return event
    return None


def _latest_event(
    events: list[dict[str, Any]], event_type: str
) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _latest_artifact(
    artifacts: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        if predicate(artifact):
            return artifact
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


def _latest_relevant_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relevant_prefixes = (
        "annotation.",
        "assembly_review.",
        "hic_scaffolding.",
        "repeat_annotation.",
        "gene_annotation.",
        "function_annotation.",
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


def _latest_stage(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("stage"):
            return str(event["stage"])
    return None


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
) -> dict[str, Any] | None:
    if request is None:
        return None
    projected = {
        "stage": request.get("stage"),
        "review_request_id": request.get("review_request_id")
        or request.get("subject_id"),
        "reason": request.get("reason"),
        "review_request_artifact_ref": request.get("review_request_artifact_ref"),
        "auto_decidable": request.get("auto_decidable"),
        "pending_human_confirmation": request.get("pending_human_confirmation"),
    }
    if requirements:
        projected["gate_id"] = requirements.get("gate_id")
        projected["decisions_allowed"] = list(
            requirements.get("decisions_allowed") or []
        )
    return {key: value for key, value in projected.items() if value is not None}


def build_next_action(
    summary: dict[str, Any], *, data_dir: str | Path = "data"
) -> dict[str, Any]:
    if summary.get("error"):
        return {
            "kind": "inspect_unavailable",
            "summary": (
                f"No durable ledger session found for {summary.get('session_id')!r}."
            ),
            "required_fields": [],
            "relevant_refs": [],
        }

    requirements = summary.get("decision_requirements")
    if requirements:
        return _review_gate_next_action(summary, data_dir=data_dir)

    if summary.get("supervision_state") == "completed":
        refs = _artifact_refs(
            summary.get("latest_final_report"),
            summary.get("latest_function_annotation_bundle"),
        )
        return {
            "kind": "completed",
            "summary": (
                "Workflow completed; inspect the listed final report and function "
                "annotation artifacts."
            ),
            "required_fields": [],
            "relevant_refs": refs,
        }

    env_block = summary.get("latest_env_preflight_block")
    if env_block:
        return _env_block_next_action(env_block)

    if summary.get("supervision_state") == "failed_terminal":
        return _failed_terminal_next_action(summary)

    return {
        "kind": "unknown",
        "summary": (
            "No active operator action was inferred from durable ledger and artifact "
            "records."
        ),
        "required_fields": [],
        "relevant_refs": _artifact_refs(*summary.get("latest_relevant_artifacts", [])),
    }


def build_result_next_action(result: dict[str, Any]) -> dict[str, Any]:
    supervision = result.get("supervision_state")
    active_interrupt = result.get("active_interrupt")
    if supervision == "blocked_human" and active_interrupt:
        try:
            requirements = _decision_requirements_from_state(result, active_interrupt)
        except ResumeValidationError as exc:
            return {
                "kind": "review_request_invalid",
                "summary": str(exc),
                "stage": active_interrupt.get("stage"),
                "required_fields": [
                    "review_request_id",
                    "review_request_hash",
                    "request_ref",
                ],
                "relevant_refs": _artifact_refs(active_interrupt.get("request_ref")),
            }
        checkpoint = result.get("checkpoint_persistence")
        summary = {
            "session_id": result.get("session_id"),
            "decision_requirements": requirements,
            "durable_resume_available": checkpoint != "memory",
        }
        return _review_gate_next_action(
            summary, data_dir=result.get("data_dir", "data")
        )
    if supervision == "failed_terminal":
        missing_input = _result_missing_input(result)
        if missing_input:
            return _missing_input_next_action(missing_input)
        env_block = _result_env_block(result)
        if env_block:
            return _env_block_next_action(env_block)
        return _failed_terminal_next_action(
            {
                "supervision_state": supervision,
                "latest_operator_report_payload": {
                    "current_stage": result.get("current_stage"),
                    "last_error": result.get("last_error"),
                    "relevant_refs": _result_relevant_refs(result),
                },
            }
        )
    return {
        "kind": "unknown",
        "summary": "No active operator action was inferred from the workflow result.",
        "required_fields": [],
        "relevant_refs": [],
    }


def build_resume_decision_template(summary: dict[str, Any]) -> dict[str, Any]:
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    gate_id = requirements.get("gate_id")
    if not isinstance(stage, str):
        raise ResumeValidationError(
            "no active registered review decision requirements; registered stages: "
            f"{registered_review_stage_label()}"
        )
    try:
        gate = get_review_gate(stage, gate_id)
    except KeyError as exc:
        raise ResumeValidationError(
            "no active registered review decision requirements; registered stages: "
            f"{registered_review_stage_label()}"
        ) from exc

    decisions_allowed = list(requirements.get("decisions_allowed") or [])
    decision = _preferred_template_decision(gate, decisions_allowed)
    guidance = {
        "decisions_allowed": decisions_allowed,
        "review_request_artifact_ref": requirements.get("review_request_artifact_ref"),
    }
    if requirements.get("runtime_proposal"):
        guidance["runtime_proposal"] = dict(requirements["runtime_proposal"])
    if requirements.get("report_refs"):
        guidance["report_refs"] = dict(requirements.get("report_refs") or {})
    if requirements.get("candidate_comparison_context"):
        guidance["candidate_comparison_context"] = dict(
            requirements.get("candidate_comparison_context") or {}
        )
    template: dict[str, Any] = {
        "session_id": summary.get("session_id"),
        "stage": stage,
        "gate_id": gate.gate_id,
        "review_request_id": requirements.get("review_request_id"),
        "review_request_hash": requirements.get("request_hash"),
        "decision": decision,
        "notes": ["replace this note with the operator rationale"],
        "metadata": {
            **dict(requirements.get("resume_metadata") or {}),
            "template_guidance": guidance,
        },
        "decided_by": "human",
        "rationale_artifact_ref": None,
    }
    if requirements.get("candidate_ids"):
        candidate_ids = list(requirements.get("candidate_ids") or [])
        candidate_refs = dict(
            requirements.get("candidate_primary_output_refs") or {}
        )
        guidance["candidate_ids"] = candidate_ids
        guidance["candidate_primary_output_refs"] = candidate_refs
        if "candidate_ids" in gate.required_resume_fields:
            template["candidate_ids"] = candidate_ids
        else:
            selected_candidate = candidate_ids[0] if candidate_ids else "<candidate_id>"
            template["candidate_id"] = selected_candidate
            template["rejected_candidates"] = []
    if requirements.get("candidate_methods"):
        candidate_methods = list(requirements.get("candidate_methods") or [])
        guidance["candidate_methods"] = candidate_methods
        template["method_name"] = (
            candidate_methods[0] if candidate_methods else "<method_name>"
        )
    return template


def _preferred_template_decision(
    gate: ReviewGateDeclaration,
    decisions_allowed: list[str],
) -> str:
    if gate.template_preferred_decision in decisions_allowed:
        return str(gate.template_preferred_decision)
    return (
        decisions_allowed[0]
        if decisions_allowed
        else (gate.template_preferred_decision or gate.decisions_allowed[0])
    )


def _review_gate_next_action(
    summary: dict[str, Any], *, data_dir: str | Path
) -> dict[str, Any]:
    session_id = summary.get("session_id")
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage", "review")
    abs_data_dir = str(Path(data_dir).expanduser().resolve(strict=False))
    template_hint = str(
        Path(abs_data_dir) / "decisions" / f"{session_id}_{stage}_decision.json"
    )
    command = (
        "gene-agent inspect "
        f"--session-id {session_id} --data-dir {abs_data_dir} "
        f"--write-decision-template {template_hint}"
    )
    resume_command = (
        "gene-agent resume "
        f"--session-id {session_id} --data-dir {abs_data_dir} "
        f"--decision-file {template_hint} --checkpointer sqlite"
    )
    if not summary.get("durable_resume_available"):
        resume_summary = (
            "Durable resume is unavailable until the session has a sqlite "
            "checkpoint database."
        )
    else:
        resume_summary = (
            f"Human review is required for {stage}; write and edit a decision file, "
            "then resume."
        )
    fields = [
        "session_id",
        "stage",
        "review_request_id",
        "review_request_hash",
        "decision",
        "decided_by",
    ]
    gate = find_review_gate(stage, requirements.get("gate_id"))
    if gate is not None:
        fields.extend(
            field for field in gate.required_resume_fields if field not in fields
        )
    return {
        "kind": "review_decision_required",
        "summary": resume_summary,
        "stage": stage,
        "decisions_allowed": list(requirements.get("decisions_allowed") or []),
        "candidate_ids": list(requirements.get("candidate_ids") or []),
        "candidate_methods": list(requirements.get("candidate_methods") or []),
        "required_fields": fields,
        "decision_template_path_hint": template_hint,
        "command": command,
        "resume_command": resume_command,
        "relevant_refs": _artifact_refs(
            summary.get("latest_review_request_artifact"),
            requirements.get("request_ref"),
            requirements.get("report_refs"),
            _candidate_comparison_relevant_refs(
                requirements.get("candidate_comparison_context")
            ),
        ),
    }


def _env_block_next_action(event: dict[str, Any]) -> dict[str, Any]:
    status = event.get("status")
    kind = "missing_tool" if status == "missing_tool" else "blocked_env"
    missing_tools = list(event.get("missing_tools") or [])
    env_target = event.get("env_target")
    detail = event.get("detail") or "environment preflight failed"
    if kind == "missing_tool":
        summary = (
            f"Install or expose required tool(s) in {env_target}: "
            f"{', '.join(missing_tools)}."
        )
    else:
        summary = f"Fix the configured execution environment {env_target}: {detail}."
    return {
        "kind": kind,
        "summary": summary,
        "stage": event.get("stage"),
        "method_name": event.get("method_name"),
        "env_target": env_target,
        "missing_tools": missing_tools,
        "detail": detail,
        "required_fields": [],
        "relevant_refs": [],
    }


def _result_stage_output(result: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    current_stage = result.get("current_stage")
    stage_outputs = result.get("stage_outputs") or {}
    if not isinstance(current_stage, str) or not isinstance(stage_outputs, dict):
        return None
    output = stage_outputs.get(current_stage)
    if not isinstance(output, dict):
        return None
    return current_stage, output


def _result_missing_input(result: dict[str, Any]) -> dict[str, Any] | None:
    stage_output = _result_stage_output(result)
    if not stage_output:
        return None
    current_stage, output = stage_output
    if output.get("status") != "blocked_input":
        return None
    return {
        "stage": current_stage,
        "blocked_input_ref": output.get("blocked_input_ref"),
        "detail": output.get("reason") or result.get("last_error"),
    }


def _result_env_block(result: dict[str, Any]) -> dict[str, Any] | None:
    stage_output = _result_stage_output(result)
    if not stage_output:
        return None
    current_stage, output = stage_output
    if output.get("status") not in {"missing_tool", "blocked_env"}:
        return None
    return {
        "stage": current_stage,
        "method_name": output.get("method_name"),
        "status": output.get("status"),
        "env_target": output.get("env_target"),
        "missing_tools": output.get("missing_tools") or [],
        "detail": output.get("reason"),
    }


def _missing_input_next_action(block: dict[str, Any]) -> dict[str, Any]:
    blocked_ref = block.get("blocked_input_ref")
    detail = block.get("detail") or "required input is missing"
    if blocked_ref:
        summary = (
            "Provide or correct the missing input path, then rerun with the original "
            f"manifest: {blocked_ref}."
        )
    else:
        summary = (
            "Provide or correct the missing input referenced by the manifest: "
            f"{detail}."
        )
    return {
        "kind": "missing_input",
        "summary": summary,
        "stage": block.get("stage"),
        "blocked_input_ref": blocked_ref,
        "detail": detail,
        "required_fields": ["manifest asset path"],
        "relevant_refs": [str(blocked_ref)] if blocked_ref else [],
    }


def _result_relevant_refs(result: dict[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("asset_registry_ref", "stage_plan_ref"):
        if result.get(key):
            refs[key] = result[key]
    current_stage = result.get("current_stage")
    stage_outputs = result.get("stage_outputs") or {}
    if isinstance(current_stage, str) and isinstance(stage_outputs, dict):
        output = stage_outputs.get(current_stage)
        if isinstance(output, dict):
            refs[current_stage] = {
                key: value
                for key, value in output.items()
                if key.endswith("_ref") or key.endswith("_artifact_ref")
            }
    return refs


def _report_missing_input(payload: dict[str, Any]) -> dict[str, Any] | None:
    refs = payload.get("relevant_refs")
    if not isinstance(refs, dict):
        return None
    stage = payload.get("current_stage")
    stage_refs = refs.get(stage) if stage else None
    if not isinstance(stage_refs, dict):
        return None
    blocked_ref = stage_refs.get("blocked_input_ref")
    if not blocked_ref:
        return None
    return {
        "stage": stage,
        "blocked_input_ref": blocked_ref,
        "detail": payload.get("last_error"),
    }


def _failed_terminal_next_action(summary: dict[str, Any]) -> dict[str, Any]:
    payload = (
        summary.get("latest_operator_report_payload")
        or summary.get("latest_terminal_event")
        or {}
    )
    last_error = str(
        payload.get("last_error") or summary.get("last_error") or "unknown failure"
    )
    stage = (
        payload.get("current_stage")
        or payload.get("stage")
        or summary.get("current_stage")
    )
    missing_input = _report_missing_input(payload)
    if missing_input:
        return _missing_input_next_action(missing_input)
    if _is_function_protein_artifact_error(last_error):
        return {
            "kind": "materialized_proteins_required",
            "summary": (
                "Function annotation requires a materialized local predicted-proteins "
                "FASTA matching database_provenance input_proteins_ref."
            ),
            "stage": stage or "function_annotation",
            "last_error": last_error,
            "required_fields": [
                "manifest.assets[predicted_proteins].paths[0]",
                "stage_outputs.gene_annotation.proteins_fasta_ref",
                "function_annotation.database_provenance[*].input_proteins_ref",
            ],
            "manifest_asset_type": "predicted_proteins",
            "run_config_path": (
                "function_annotation.database_provenance[*].input_proteins_ref"
            ),
            "relevant_refs": _artifact_refs(
                payload.get("relevant_refs"),
                summary.get("latest_relevant_artifacts"),
            ),
        }
    if "database_provenance" in last_error:
        required_fields = [
            "method_name",
            "database_name",
            "database_version",
            "snapshot_date",
            "database_source_ref",
            "input_proteins_ref",
            "command or descriptor_version",
        ]
        return {
            "kind": "provenance_required",
            "summary": (
                "Function annotation requires database_provenance records matching a "
                "materialized proteins_fasta_ref."
            ),
            "stage": stage or "function_annotation",
            "last_error": last_error,
            "required_fields": required_fields,
            "run_config_path": "function_annotation.database_provenance",
            "relevant_refs": _artifact_refs(
                payload.get("relevant_refs"),
                summary.get("latest_relevant_artifacts"),
            ),
        }
    return {
        "kind": "failed_terminal",
        "summary": last_error,
        "stage": stage,
        "last_error": last_error,
        "required_fields": [],
        "relevant_refs": _artifact_refs(
            payload.get("relevant_refs"),
            summary.get("latest_relevant_artifacts"),
        ),
    }


def _is_function_protein_artifact_error(last_error: str) -> bool:
    return any(
        marker in last_error
        for marker in (
            "protein artifact not found for function_annotation",
            "protein artifact is not a file for function_annotation",
            "proteins_fasta_ref must be a local file path for function_annotation",
        )
    )


def _candidate_comparison_relevant_refs(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    return {
        key: context[key] for key in ("candidate_comparison_ref",) if context.get(key)
    }


def _artifact_refs(*values: Any) -> list[str]:
    refs: list[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, list):
            refs.extend(_artifact_refs(*value))
        elif isinstance(value, dict):
            path_ref = value.get("path_ref") or value.get("file_path")
            if path_ref:
                refs.append(str(path_ref))
            elif value.get("artifact"):
                refs.extend(_artifact_refs(value.get("artifact")))
            elif value.get("relevant_refs"):
                refs.extend(_artifact_refs(value.get("relevant_refs")))
            else:
                refs.extend(_artifact_refs(*value.values()))
        else:
            refs.append(str(value))
    return list(dict.fromkeys(refs))


def _decision_requirements_from_artifact(
    artifact: dict[str, Any] | None,
    *,
    paths: SessionPaths,
    expected_request_id: str,
    expected_request_hash: str | None = None,
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
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise ResumeValidationError("review request artifact payload is invalid")
    context = {
        **payload,
        **dict(record.get("bindings") or {}),
        "stage": record.get("stage"),
        "gate_id": record.get("gate_id"),
        "review_request_id": expected_request_id,
        "request_ref": ref.model_dump(mode="json"),
        "request_hash": ref.sha256,
        "review_request_artifact_ref": ref.path_ref,
    }
    return _decision_requirements(context)
