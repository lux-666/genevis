"""Lifecycle helpers for inspecting and resuming human review gates."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import read_artifact_records
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ArtifactMeta
from app.schemas.state import TopLevelState


class LifecycleError(ValueError):
    """Base error for lifecycle operations."""


class ResumeValidationError(LifecycleError):
    """Raised when a resume request cannot be applied."""


class DurableResumeUnavailableError(LifecycleError):
    """Raised when a durable cross-process resume is requested."""


class UnsupportedLifecycleOperationError(LifecycleError):
    """Raised for lifecycle operations outside the current task scope."""


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
    summary: dict[str, Any] = {
        "session_id": snapshot.get("session_id"),
        "current_stage": snapshot.get("current_stage"),
        "supervision_state": snapshot.get("supervision_state"),
        "active_interrupt": active_interrupt,
        "stage_statuses": _stage_statuses(snapshot),
        "latest_artifact_refs": _latest_artifact_refs(snapshot),
        "decision_requirements": _decision_requirements(active_interrupt),
    }
    timeout = _blocked_timeout_status(active_interrupt, blocked_timeout_seconds, now=now)
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
    """Inspect durable ledger/artifact records and durable checkpoint state."""

    from app.graph.checkpointer import checkpoint_db_path

    ledger = Ledger(data_dir)
    session = ledger.read_session(session_id)
    events = ledger.read_events(session_id)
    artifacts = read_artifact_records(data_dir, session_id)
    latest_review_request = _latest_event(events, "review_requested")
    latest_review_decision = _latest_event(events, "review_decision")
    latest_review_lifecycle_event = _latest_review_lifecycle_event(events)
    latest_unresolved_review_request = latest_review_lifecycle_event if latest_review_lifecycle_event and latest_review_lifecycle_event.get("event_type") == "review_requested" else None
    latest_request_artifact = _latest_artifact_by_suffix(artifacts, ".request")
    latest_decision_artifact = _latest_artifact_by_suffix(artifacts, ".decision")

    supervision_state = "unknown"
    if latest_unresolved_review_request:
        supervision_state = "blocked_human"
    elif latest_review_lifecycle_event and latest_review_lifecycle_event.get("event_type") == "review_decision":
        supervision_state = "review_decision_recorded"
    elif session:
        supervision_state = "running_or_completed_unknown"

    db_path = checkpoint_db_path(data_dir)
    durable_available = db_path.exists()
    checkpoint_persistence = "sqlite" if durable_available else "memory_only"

    decision_requirements = _decision_requirements_from_artifact(latest_request_artifact) if latest_unresolved_review_request else None

    summary: dict[str, Any] = {
        "session_id": session_id,
        "session": session,
        "supervision_state": supervision_state,
        "checkpoint_persistence": checkpoint_persistence,
        "durable_resume_available": durable_available,
        "ledger_event_count": len(events),
        "latest_events": events[-5:],
        "latest_review_request": latest_review_request,
        "latest_review_decision": latest_review_decision,
        "latest_review_lifecycle_event": latest_review_lifecycle_event,
        "latest_unresolved_review_request": latest_unresolved_review_request,
        "latest_review_request_artifact": latest_request_artifact,
        "latest_review_decision_artifact": latest_decision_artifact,
        "decision_requirements": decision_requirements,
        "artifact_count": len(artifacts),
    }
    if latest_unresolved_review_request and blocked_timeout_seconds is not None:
        summary["blocked_timeout"] = _durable_blocked_timeout_status(
            latest_unresolved_review_request,
            blocked_timeout_seconds,
            now=now,
        )
    if not session:
        summary["error"] = "session_not_found"
    return summary


def resume_review_state(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    blocked_timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a validated human review decision to an in-memory blocked state."""

    _validate_common_resume_state(state, request)
    active_interrupt = state.get("active_interrupt") or {}
    timeout = _blocked_timeout_status(active_interrupt, blocked_timeout_seconds, now=now)
    if timeout and timeout.get("expired"):
        return _timeout_failed_update(state, request, timeout)

    if request.stage == "assembly_review":
        from app.stages.assembly_review.nodes import apply_assembly_review_resume_decision

        return apply_assembly_review_resume_decision(state, request)
    if request.stage == "hic_scaffolding":
        from app.stages.hic_scaffolding.nodes import apply_hic_scaffolding_resume_decision

        return apply_hic_scaffolding_resume_decision(state, request)
    raise ResumeValidationError(f"Unsupported resume stage: {request.stage}")


def reject_unsupported_lifecycle_operation(operation: str) -> None:
    if operation in {"retry", "rerun", "rebuild", "rebuild-from"}:
        raise UnsupportedLifecycleOperationError(
            f"{operation} is not a resume operation and is not implemented in Phase 3 Task 02."
        )


def _validate_common_resume_state(state: TopLevelState, request: ResumeReviewRequest) -> None:
    if state.get("session_id") != request.session_id:
        raise ResumeValidationError("resume session_id does not match blocked state")
    if state.get("supervision_state") != "blocked_human":
        raise ResumeValidationError("resume requires supervision_state blocked_human")
    active_interrupt = state.get("active_interrupt")
    if not active_interrupt:
        raise ResumeValidationError("resume requires an active_interrupt")
    if active_interrupt.get("stage") != request.stage:
        raise ResumeValidationError("resume stage does not match active_interrupt.stage")
    _ensure_decision_allowed_by_interrupt(active_interrupt, request)


def _ensure_decision_allowed_by_interrupt(active_interrupt: dict[str, Any], request: ResumeReviewRequest) -> None:
    allowed = active_interrupt.get("decisions_allowed")
    if allowed is None and request.stage == "hic_scaffolding":
        allowed = ["select_method", "skip", "stop_human_review"]
    if allowed is not None and request.decision not in allowed:
        raise ResumeValidationError(f"decision {request.decision!r} is not allowed by active_interrupt")


def _timeout_failed_update(state: TopLevelState, request: ResumeReviewRequest, timeout: dict[str, Any]) -> dict[str, Any]:
    timeout_request = request.model_copy(
        update={
            "decision": "stop_human_review",
            "notes": [*request.notes, "blocked timeout expired"],
            "metadata": {**request.metadata, "blocked_timeout": timeout},
        }
    )
    if request.stage == "assembly_review":
        from app.stages.assembly_review.nodes import apply_assembly_review_resume_decision

        update = apply_assembly_review_resume_decision(state, timeout_request)
    elif request.stage == "hic_scaffolding":
        from app.stages.hic_scaffolding.nodes import apply_hic_scaffolding_resume_decision

        update = apply_hic_scaffolding_resume_decision(state, timeout_request)
    else:
        raise ResumeValidationError(f"Unsupported timeout stage: {request.stage}")
    return {**update, "last_error": f"{request.stage} blocked timeout expired"}


def _stage_statuses(state: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for stage, output in (state.get("stage_outputs") or {}).items():
        if isinstance(output, dict):
            statuses[stage] = str(output.get("status", "unknown"))
    return statuses


def _latest_artifact_refs(state: dict[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("asset_registry_ref", "stage_plan_ref"):
        if state.get(key):
            refs[key] = state[key]
    for stage, output in (state.get("stage_outputs") or {}).items():
        if not isinstance(output, dict):
            continue
        stage_refs = {key: value for key, value in output.items() if key.endswith("_ref") or key.endswith("_artifact_ref")}
        if stage_refs:
            refs[stage] = stage_refs
    return refs


def _decision_requirements(active_interrupt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not active_interrupt:
        return None
    stage = active_interrupt.get("stage")
    if stage == "assembly_review":
        return {
            "stage": stage,
            "decisions_allowed": active_interrupt.get("decisions_allowed", []),
            "candidate_ids": [candidate.get("candidate_id") for candidate in active_interrupt.get("candidates", [])],
            "requires_structured_resume": True,
        }
    if stage == "hic_scaffolding":
        return {
            "stage": stage,
            "decisions_allowed": active_interrupt.get("decisions_allowed", ["select_method", "skip", "stop_human_review"]),
            "candidate_methods": active_interrupt.get("candidate_methods", []),
            "requires_structured_resume": True,
        }
    return {"stage": stage, "requires_structured_resume": True}


def _blocked_timeout_status(
    active_interrupt: dict[str, Any] | None,
    blocked_timeout_seconds: float | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if not active_interrupt or blocked_timeout_seconds is None:
        return None
    blocked_at_raw = active_interrupt.get("blocked_at")
    if not blocked_at_raw:
        return {"configured_seconds": blocked_timeout_seconds, "expired": False, "reason": "blocked_at_missing"}
    try:
        blocked_at = datetime.fromisoformat(str(blocked_at_raw))
    except ValueError:
        return {"configured_seconds": blocked_timeout_seconds, "expired": False, "reason": "blocked_at_invalid"}
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
    timeout = _blocked_timeout_status(active_interrupt, blocked_timeout_seconds, now=now)
    return timeout or {"configured_seconds": blocked_timeout_seconds, "expired": False, "reason": "timestamp_missing"}


def _latest_review_lifecycle_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") in {"review_requested", "review_decision"}:
            return event
    return None


def _latest_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") == event_type:
            return event
    return None


def _latest_artifact_by_suffix(artifacts: list[dict[str, Any]], suffix: str) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        artifact_type = str(artifact.get("artifact_type", ""))
        if artifact_type.endswith(suffix):
            return artifact
    return None


def _decision_requirements_from_artifact(
    artifact: ArtifactMeta | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reconstruct decision requirements from a review request artifact.

    Reads ``decisions_allowed`` / ``candidates`` / ``candidate_methods``
    out of the JSON file pointed to by the artifact metadata so the
    durable inspect path can tell an operator which decisions the gate
    accepts without their having to open the artifact themselves.
    Falls back to ``None`` if the file is missing or unreadable; the
    inspect summary already exposes the artifact ref so the operator
    can recover manually.

    The artifact record is validated through ``ArtifactMeta`` before
    reading ``file_path``. That keeps the durable inspect path tied to
    the same metadata schema used by artifact registration instead of
    silently drifting if the serialized key changes.
    """
    if not artifact:
        return None
    meta = artifact if isinstance(artifact, ArtifactMeta) else ArtifactMeta.model_validate(artifact)
    path = Path(meta.file_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    stage = payload.get("stage")
    requirements: dict[str, Any] = {
        "stage": stage,
        "decisions_allowed": list(payload.get("decisions_allowed") or []),
        "requires_structured_resume": True,
    }
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        requirements["candidate_ids"] = [
            c.get("candidate_id")
            for c in candidates
            if isinstance(c, dict) and c.get("candidate_id") is not None
        ]
    candidate_methods = payload.get("candidate_methods")
    if isinstance(candidate_methods, list):
        requirements["candidate_methods"] = list(candidate_methods)
    return requirements
