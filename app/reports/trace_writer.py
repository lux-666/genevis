"""Unified session-summary projection from ledger v2 and the artifact index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.control.lifecycle import inspect_session
from app.control.workflow_plan import workflow_steps_from_run_config
from app.reports._projection import atomic_write_text, source_watermark
from app.reports.review_report import _pending_review_requests
from app.review.records import counts_by_origin
from app.storage.artifacts import read_artifact_records
from app.storage.frozen_inputs import validate_frozen_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths

# Stable stage-status vocabulary. Keep this small and durable; the trace
# JSON and any downstream tooling depend on these exact strings.
STAGE_STATUS_EXECUTED = "executed"
STAGE_STATUS_PLANNED_ONLY = "planned_only"
STAGE_STATUS_BLOCKED_HUMAN = "blocked_human"
STAGE_STATUS_FAILED = "failed"
STAGE_STATUS_COMPLETED = "completed"
STAGE_STATUS_SKIPPED = "skipped"
STAGE_STATUS_NO_EVIDENCE = "no_durable_evidence"

# Durable-record gap vocabulary. Each gap names a fact the trace could not
# reconstruct and why, so operators do not mistake "unavailable" for "absent".
GAP_UNAVAILABLE = "unavailable"  # no durable record carries this fact
GAP_CHECKPOINT_ONLY = "checkpoint_only"
GAP_UNSUPPORTED = "unsupported"

SESSION_SUMMARY_JSON_FILENAME = "session_summary.json"
SESSION_SUMMARY_MARKDOWN_FILENAME = "session_summary.md"

# Artifact types that are workflow plumbing, not operator-facing evidence.
_NOISE_ARTIFACT_TYPES = frozenset({"command.stdout", "command.stderr"})
# Artifact-type prefixes the operator should open first, in priority order.
_OPEN_FIRST_PREFIXES = ("assembly_review.",)
# Terminal traces accept only references emitted by an explicit successful
# publication, registration, review, or completed-stage semantic boundary.
# Generic and failure diagnostics can carry refs for recovery, but never prove
# that an indexed artifact belongs in the terminal evidence projection.
_ARTIFACT_PROVING_EVENT_TYPES = frozenset(
    {
        "artifact_registered",
        "assets_registered",
        "attempt_succeeded",
        "review_requested",
        "review_decision",
        "review_applied",
        "registered_assembly_candidate_bound",
        "stage_completed",
    }
)
ArtifactIdentity = tuple[str, str, str, int, str]


def build_session_trace(data_dir: str | Path, session_id: str) -> dict[str, Any]:
    """Reconstruct the structured session-summary model from durable facts."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    session = ledger.read_session(session_id)
    events = ledger.read_events(session_id)
    authoritative_events, post_terminal_events, canonical_terminal = (
        _authoritative_event_boundary(ledger, session_id, events)
    )
    artifacts = _authoritative_artifacts(
        read_artifact_records(paths.run_dir, session_id),
        authoritative_events,
        canonical_terminal,
    )
    summary = inspect_session(session_id, paths.run_dir)
    review_occurrences = _authoritative_review_occurrences(
        summary.get("review_occurrences") or [],
        canonical_terminal,
    )

    decisions = [
        event
        for event in authoritative_events
        if event.get("event_type") == "review_decision"
    ]
    stages = _build_stage_records(
        paths,
        authoritative_events,
        artifacts,
        summary,
        review_occurrences,
    )
    gaps = _build_gaps(
        session,
        authoritative_events,
        decisions,
        review_occurrences,
        summary,
    )
    route = _route_records(authoritative_events)
    observed_path = [
        record["stage"] for record in route if record["event_type"] == "stage_entered"
    ]
    if not observed_path:
        observed_path = [
            stage["stage"]
            for stage in stages
            if stage["status"] != STAGE_STATUS_NO_EVIDENCE
        ]

    trace: dict[str, Any] = {
        "schema_version": 4,
        "report_type": "session_summary",
        "session_id": session_id,
        "session_found": session is not None,
        "source_watermark": source_watermark(paths, ledger),
        "input_manifest_ref": (session or {}).get("input_manifest_ref"),
        "created_at": (session or {}).get("created_at"),
        "supervision_state": summary.get("supervision_state"),
        "checkpoint_persistence": summary.get("checkpoint_persistence"),
        "durable_resume_available": summary.get("durable_resume_available"),
        "final_or_current_stage": _final_or_current_stage(stages, summary),
        "route": route,
        "stage_path_observed": observed_path,
        "stages": stages,
        "decisions": [_decision_record(decision) for decision in decisions],
        "decisions_by_origin": counts_by_origin(decisions),
        "pending_review_requests": _pending_review_requests(review_occurrences),
        "open_first": _open_first_artifacts(artifacts),
        "failures": _failures(authoritative_events),
        "blocked": _blocked_summary(summary),
        "next_action": summary.get("next_action"),
        "ledger_event_count": len(events),
        "authoritative_ledger_event_count": len(authoritative_events),
        "terminal_integrity": _terminal_integrity_diagnostic(
            canonical_terminal,
            post_terminal_events,
            summary.get("terminal_integrity"),
        ),
        "artifact_count": len(artifacts),
        "gaps": gaps,
    }
    if session is None:
        trace["error"] = "session_not_found"
    return trace


def _authoritative_event_boundary(
    ledger: Ledger,
    session_id: str,
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Split business facts from immutable-history terminal diagnostics."""

    terminal_read = ledger.read_canonical_terminal(session_id)
    canonical_terminal = (
        terminal_read.get("canonical") if terminal_read is not None else None
    )
    terminal_sequence = _event_sequence(canonical_terminal)
    if terminal_sequence is None:
        return list(events), [], canonical_terminal
    return (
        [
            event
            for event in events
            if (_event_sequence(event) or 0) <= terminal_sequence
        ],
        [
            event
            for event in events
            if (_event_sequence(event) or 0) > terminal_sequence
            and not _is_post_terminal_maintenance_event(event)
        ],
        canonical_terminal,
    )


def _authoritative_artifacts(
    artifacts: list[dict[str, Any]],
    authoritative_events: list[dict[str, Any]],
    canonical_terminal: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Keep terminal traces to index artifacts proven before the terminal.

    A v2 artifact index has no Ledger sequence, so its own order cannot prove
    whether an item predates a terminal. A complete artifact ref in a
    successful semantic event (including ``attempt_succeeded`` or
    ``artifact_registered``) is the causal link. Failed-attempt and failed-stage
    refs deliberately cannot prove an artifact: collection may leave those as
    index-only orphans. Report projections use their fixed report paths rather
    than this index, so there is deliberately no broad report-artifact exception.
    """

    if _event_sequence(canonical_terminal) is None:
        return list(artifacts)

    terminal_sequence = _event_sequence(canonical_terminal)
    assert terminal_sequence is not None

    proven_identities: set[ArtifactIdentity] = set()
    for event in authoritative_events:
        # The terminal row is a boundary, never publication evidence. A ref on
        # that row cannot establish when its indexed artifact was produced.
        event_sequence = _event_sequence(event)
        if event_sequence is None or event_sequence >= terminal_sequence:
            continue
        if event.get("event_type") not in _ARTIFACT_PROVING_EVENT_TYPES:
            continue
        for ref in event.get("refs") or []:
            identity = _complete_artifact_identity(ref)
            if identity is None:
                continue
            proven_identities.add(identity)

    return [
        artifact
        for artifact in artifacts
        if _complete_artifact_identity(artifact) in proven_identities
    ]


def _complete_artifact_identity(value: Any) -> ArtifactIdentity | None:
    """Return an index-joinable identity only for a complete ArtifactRef."""

    if not isinstance(value, dict):
        return None
    artifact_id = value.get("artifact_id")
    path_ref = value.get("path_ref")
    sha256 = value.get("sha256")
    size_bytes = value.get("size_bytes")
    artifact_type = value.get("artifact_type")
    if not (
        isinstance(artifact_id, str)
        and artifact_id
        and isinstance(path_ref, str)
        and path_ref
        and isinstance(sha256, str)
        and sha256
        and type(size_bytes) is int
        and size_bytes >= 0
        and isinstance(artifact_type, str)
        and artifact_type
    ):
        return None
    return artifact_id, path_ref, sha256, size_bytes, artifact_type


def _is_post_terminal_maintenance_event(event: dict[str, Any]) -> bool:
    """Accept only finalizer-shaped compaction facts after a terminal row."""

    event_type = event.get("event_type")
    if not isinstance(event_type, str):
        return False
    expected_key = {
        "terminal_compaction_completed": "terminal-compaction:completed",
        "terminal_compaction_skipped": "terminal-compaction:skipped",
    }.get(event_type)
    if expected_key is None or event.get("idempotency_key") != expected_key:
        return False
    if any(
        event.get(field) is not None
        for field in ("stage", "workflow_step_id", "subject_id")
    ):
        return False
    if event.get("refs") not in (None, []):
        return False
    summary = event.get("summary")
    if not isinstance(summary, dict):
        return False
    if event_type == "terminal_compaction_completed":
        return summary == {"checkpoint_count": 1}
    return (
        set(summary) == {"error"}
        and isinstance(summary["error"], str)
        and bool(summary["error"])
    )


def _authoritative_review_occurrences(
    occurrences: list[dict[str, Any]],
    canonical_terminal: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Exclude requests appended after the irreversible session outcome."""

    terminal_sequence = _event_sequence(canonical_terminal)
    if terminal_sequence is None:
        return list(occurrences)
    return [
        occurrence
        for occurrence in occurrences
        if (_event_sequence(occurrence, key="request_sequence") or 0)
        <= terminal_sequence
    ]


def _terminal_integrity_diagnostic(
    canonical_terminal: dict[str, Any] | None,
    post_terminal_events: list[dict[str, Any]],
    terminal_integrity: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Expose terminal-after rows without granting them projection authority."""

    if not post_terminal_events and terminal_integrity is None:
        return None
    diagnostic: dict[str, Any] = {
        "canonical_terminal_sequence": _event_sequence(canonical_terminal),
        "post_terminal_event_count": len(post_terminal_events),
    }
    if terminal_integrity is not None:
        diagnostic["terminal_history"] = terminal_integrity
    if post_terminal_events:
        diagnostic["post_terminal_events"] = [
            {
                "sequence": event.get("sequence"),
                "event_type": event.get("event_type"),
                "stage": event.get("stage"),
                "workflow_step_id": event.get("workflow_step_id"),
            }
            for event in post_terminal_events
        ]
    return diagnostic


def _build_stage_records(
    paths: SessionPaths,
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    summary: dict[str, Any],
    review_occurrences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    steps = _workflow_steps_for_trace(paths, events, artifacts)
    methods_by_step = _methods_by_workflow_step(events)
    artifacts_by_step = _artifact_refs_by_workflow_step(artifacts)
    candidate_methods_by_step = _candidate_methods_by_workflow_step(artifacts)
    reviews_by_step = _review_events_by_workflow_step(events, review_occurrences)
    lifecycle_by_step = _stage_lifecycle_by_workflow_step(events)
    blocked_step_id = _blocked_workflow_step_id(summary, events)
    failed_step_id = _failed_workflow_step_id(events, summary)

    records: list[dict[str, Any]] = []
    for step in steps:
        step_id = step["step_id"]
        stage = step["stage"]
        methods = methods_by_step.get(step_id, [])
        stage_artifacts = artifacts_by_step.get(step_id, [])
        reviews = reviews_by_step.get(step_id, [])
        candidate_methods = candidate_methods_by_step.get(step_id, [])
        lifecycle = lifecycle_by_step.get(step_id)
        status = _stage_status(
            step_id,
            methods=methods,
            stage_artifacts=stage_artifacts,
            candidate_methods=candidate_methods,
            reviews=reviews,
            blocked_step_id=blocked_step_id,
            failed_step_id=failed_step_id,
            lifecycle=lifecycle,
        )
        record: dict[str, Any] = {
            "workflow_step_id": step_id,
            "stage": stage,
            "status": status,
            "methods": methods,
            "artifact_refs": _stage_artifacts_for_trace(stage_artifacts),
            "timing": _stage_timing(methods, reviews, lifecycle),
        }
        if candidate_methods:
            record["candidate_methods"] = candidate_methods
        if reviews:
            record["reviews"] = reviews
        if lifecycle:
            record["lifecycle"] = lifecycle
        records.append(record)
    return records


def _workflow_steps_for_trace(
    paths: SessionPaths,
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Load the frozen plan, falling back to occurrence-bearing durable facts.

    The frozen RunConfig is authoritative whenever it is available.  The
    fallback keeps trace inspection useful for partially migrated or damaged
    sessions without inventing a stage order from a global registry.
    """

    try:
        bundle = validate_frozen_inputs(paths)
        return [
            dict(step) for step in workflow_steps_from_run_config(bundle.run_config)
        ]
    except (OSError, ValueError, TypeError):
        pass

    steps: dict[str, dict[str, Any]] = {}
    conflicted_step_ids: set[str] = set()
    for record in [*events, *artifacts]:
        step_id = record.get("workflow_step_id")
        stage = record.get("stage")
        if not isinstance(step_id, str) or not step_id:
            continue
        if not isinstance(stage, str) or not stage:
            continue
        if step_id in conflicted_step_ids:
            continue
        existing = steps.get(step_id)
        if existing is None:
            steps[step_id] = {
                "step_id": step_id,
                "stage": stage,
                "allow_local_execution": False,
            }
        elif existing["stage"] != stage:
            # A conflicting durable occurrence cannot be safely projected.
            del steps[step_id]
            conflicted_step_ids.add(step_id)
    return [steps[step_id] for step_id in sorted(steps)]


def _methods_by_workflow_step(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    attempts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        event_type = event.get("event_type")
        if event_type not in {"attempt_started", "attempt_succeeded", "attempt_failed"}:
            continue
        attempt_id = (
            event.get("attempt_id")
            or f"{event.get('stage')}:{event.get('method_name')}"
        )
        record = attempts.get(attempt_id)
        if record is None:
            record = {
                "attempt_id": attempt_id,
                "workflow_step_id": event.get("workflow_step_id"),
                "stage": event.get("stage"),
                "method_name": event.get("method_name"),
                "outcome": "started",
                "started_at": None,
                "finished_at": None,
                "error": None,
                "return_code": None,
            }
            attempts[attempt_id] = record
            order.append(attempt_id)
        if event_type == "attempt_started":
            record["started_at"] = event.get("timestamp")
        elif event_type == "attempt_succeeded":
            record["outcome"] = "succeeded"
            record["finished_at"] = event.get("timestamp")
        elif event_type == "attempt_failed":
            record["outcome"] = "failed"
            record["finished_at"] = event.get("timestamp")
            record["error"] = _first_error_line(event.get("error"))
            record["return_code"] = event.get("return_code")
    by_step: dict[str, list[dict[str, Any]]] = {}
    for attempt_id in order:
        record = attempts[attempt_id]
        step_id = record.get("workflow_step_id")
        if isinstance(step_id, str) and step_id:
            by_step.setdefault(step_id, []).append(record)
    return by_step


def _artifact_refs_by_workflow_step(
    artifacts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_step: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type", ""))
        if artifact_type in _NOISE_ARTIFACT_TYPES:
            continue
        step_id = artifact.get("workflow_step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        by_step.setdefault(step_id, []).append(
            {
                "artifact_type": artifact_type,
                "method_name": artifact.get("method_id") or artifact.get("method_name"),
                "path_ref": artifact.get("path_ref") or artifact.get("file_path"),
            }
        )
    return by_step


def _stage_artifacts_for_trace(
    stage_artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return list(stage_artifacts[:5])


def _candidate_methods_by_workflow_step(
    artifacts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_step: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        if artifact.get("artifact_type") != "draft_assembly.candidate":
            continue
        step_id = artifact.get("workflow_step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        by_step.setdefault(step_id, []).append(
            {
                "method_name": artifact.get("method_id") or artifact.get("method_name"),
                "candidate_id": artifact.get("candidate_id"),
                "execution_enabled": (artifact.get("provenance") or {}).get(
                    "execution_enabled"
                ),
            }
        )
    return by_step


def _review_events_by_workflow_step(
    events: list[dict[str, Any]],
    review_occurrences: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group full occurrence identities without stage/request-ID merging."""

    requests: dict[tuple[str, str, str], dict[str, Any]] = {}
    for occurrence in review_occurrences:
        step_id = occurrence.get("workflow_step_id")
        request_id = occurrence.get("review_request_id")
        request_hash = occurrence.get("request_hash")
        if not all(
            isinstance(value, str) and value
            for value in (step_id, request_id, request_hash)
        ):
            continue
        key = (str(step_id), str(request_id), str(request_hash))
        requests[key] = {
            key_name: occurrence.get(key_name)
            for key_name in (
                "workflow_step_id",
                "stage",
                "gate_id",
                "review_request_id",
                "request_hash",
                "reason",
                "review_request_artifact_ref",
                "status",
                "resolved",
                "resolution_basis",
                "integrity_error",
                "request_sequence",
            )
            if occurrence.get(key_name) is not None
        }
        requests[key]["requested_at"] = occurrence.get("timestamp")

    for event in events:
        if event.get("event_type") not in {"review_decision", "review_applied"}:
            continue
        step_id = str(event.get("workflow_step_id") or "")
        request_id = str(
            event.get("review_request_id") or event.get("subject_id") or ""
        )
        request_hash = str(event.get("request_hash") or "")
        if not step_id or not request_id or not request_hash:
            continue
        entry = requests.get((step_id, request_id, request_hash))
        if entry is None:
            continue
        if event.get("event_type") == "review_applied":
            entry["applied_at"] = event.get("timestamp")
            entry["application_id"] = event.get("application_id")
            continue
        entry["decided_at"] = event.get("timestamp")
        entry["decision"] = event.get("decision")
        entry["decided_by"] = event.get("decided_by") or "human"
        entry["candidate_id"] = event.get("candidate_id")
        entry["method_name"] = event.get("method_name")
        entry["rationale_artifact_ref"] = event.get("rationale_artifact_ref")
        entry["decision_artifact_ref"] = event.get("artifact_ref")

    by_step: dict[str, list[dict[str, Any]]] = {}
    for (step_id, _request_id, _request_hash), entry in requests.items():
        by_step.setdefault(step_id, []).append(entry)
    for entries in by_step.values():
        entries.sort(key=lambda entry: int(entry.get("request_sequence") or 0))
    return by_step


def _stage_lifecycle_by_workflow_step(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_step: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") not in {
            "stage_entered",
            "stage_skipped",
            "stage_completed",
        }:
            continue
        step_id = event.get("workflow_step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        entry = by_step.setdefault(step_id, {})
        if event["event_type"] == "stage_entered":
            entry["entered_at"] = event.get("timestamp")
        elif event["event_type"] == "stage_skipped":
            entry["skipped_at"] = event.get("timestamp")
            entry["skip_reason"] = event.get("reason")
        else:
            entry["completed_at"] = event.get("timestamp")
    return by_step


def _stage_status(
    workflow_step_id: str,
    *,
    methods: list[dict[str, Any]],
    stage_artifacts: list[dict[str, Any]],
    candidate_methods: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    blocked_step_id: str | None,
    failed_step_id: str | None,
    lifecycle: dict[str, Any] | None,
) -> str:
    if workflow_step_id == failed_step_id:
        return STAGE_STATUS_FAILED
    if workflow_step_id == blocked_step_id:
        return STAGE_STATUS_BLOCKED_HUMAN
    if any(method["outcome"] == "succeeded" for method in methods):
        return STAGE_STATUS_EXECUTED
    if any(method["outcome"] == "failed" for method in methods):
        return STAGE_STATUS_FAILED
    if lifecycle and lifecycle.get("skipped_at"):
        return STAGE_STATUS_SKIPPED
    if lifecycle and lifecycle.get("completed_at"):
        return STAGE_STATUS_COMPLETED
    if any(review.get("status") == "applied" for review in reviews):
        return STAGE_STATUS_COMPLETED
    if candidate_methods or stage_artifacts:
        return STAGE_STATUS_PLANNED_ONLY
    return STAGE_STATUS_NO_EVIDENCE


def _stage_timing(
    methods: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    lifecycle: dict[str, Any] | None,
) -> dict[str, Any]:
    timestamps = [
        value
        for method in methods
        if isinstance(value := method.get("started_at"), str) and value
    ]
    finished = [
        value
        for method in methods
        if isinstance(value := method.get("finished_at"), str) and value
    ]
    timing: dict[str, Any] = {}
    if timestamps:
        timing["started_at"] = min(timestamps)
    if finished:
        timing["finished_at"] = max(finished)
    requested = [
        review["requested_at"]
        for review in reviews
        if isinstance(review.get("requested_at"), str)
    ]
    decided = [
        review["decided_at"]
        for review in reviews
        if isinstance(review.get("decided_at"), str)
    ]
    applied = [
        review["applied_at"]
        for review in reviews
        if isinstance(review.get("applied_at"), str)
    ]
    if requested:
        timing["review_requested_at"] = min(requested)
    if decided:
        timing["review_decided_at"] = max(decided)
    if applied:
        timing["review_applied_at"] = max(applied)
    if lifecycle:
        for key in ("entered_at", "skipped_at", "completed_at"):
            if lifecycle.get(key):
                timing[key] = lifecycle[key]
    return timing


def _decision_record(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_request_id": decision.get("review_request_id")
        or decision.get("subject_id"),
        "request_hash": decision.get("request_hash"),
        "workflow_step_id": decision.get("workflow_step_id"),
        "stage": decision.get("stage"),
        "gate_id": decision.get("gate_id"),
        "decision": decision.get("decision"),
        "decided_by": decision.get("decided_by"),
        "candidate_id": decision.get("candidate_id"),
        "method_name": decision.get("method_name"),
        "rationale_artifact_ref": decision.get("rationale_artifact_ref"),
        "decision_artifact_ref": decision.get("artifact_ref"),
        "timestamp": decision.get("timestamp"),
    }


def _open_first_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for prefix in _OPEN_FIRST_PREFIXES:
        for artifact in reversed(artifacts):
            artifact_type = str(artifact.get("artifact_type", ""))
            if not artifact_type.startswith(prefix) or artifact_type in seen:
                continue
            selected.append(
                {
                    "artifact_type": artifact_type,
                    "stage": artifact.get("stage"),
                    "path_ref": artifact.get("path_ref") or artifact.get("file_path"),
                }
            )
            seen.add(artifact_type)
            break
        if len(selected) >= 6:
            break
    return selected


def _failures(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") == "attempt_failed":
            failures.append(
                {
                    "workflow_step_id": event.get("workflow_step_id"),
                    "stage": event.get("stage"),
                    "method_name": event.get("method_name"),
                    "return_code": event.get("return_code"),
                    "error": _first_error_line(event.get("error")),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event.get("event_type") == "env_preflight_blocked":
            failures.append(
                {
                    "workflow_step_id": event.get("workflow_step_id"),
                    "stage": event.get("stage"),
                    "method_name": event.get("method_name"),
                    "env_target": event.get("env_target"),
                    "missing_tools": event.get("missing_tools"),
                    "detail": event.get("detail"),
                    "timestamp": event.get("timestamp"),
                }
            )
    return failures


def _blocked_summary(summary: dict[str, Any]) -> dict[str, Any] | None:
    if summary.get("supervision_state") != "blocked_human":
        return None
    requirements = summary.get("decision_requirements") or {}
    next_action = summary.get("next_action") or {}
    return {
        "workflow_step_id": (summary.get("active_interrupt") or {}).get(
            "workflow_step_id"
        ),
        "stage": requirements.get("stage") or next_action.get("stage"),
        "decisions_allowed": requirements.get("decisions_allowed") or [],
        "pending_human_action": next_action.get("summary"),
        "resume_command": next_action.get("resume_command")
        or next_action.get("command"),
        "review_status": (summary.get("latest_review_status") or {}).get("status"),
        "integrity_error": (summary.get("latest_review_status") or {}).get(
            "integrity_error"
        ),
        "review_request_artifact_ref": (
            summary.get("latest_review_request_artifact") or {}
        ).get("path_ref")
        or (summary.get("latest_review_request_artifact") or {}).get("file_path"),
    }


def _blocked_workflow_step_id(
    summary: dict[str, Any], events: list[dict[str, Any]]
) -> str | None:
    if summary.get("supervision_state") != "blocked_human":
        return None
    active_interrupt = summary.get("active_interrupt") or {}
    step_id = active_interrupt.get("workflow_step_id")
    if isinstance(step_id, str) and step_id:
        return step_id
    for event in reversed(events):
        if event.get("event_type") == "review_requested":
            step_id = event.get("workflow_step_id")
            if isinstance(step_id, str) and step_id:
                return step_id
    return None


def _blocked_stage(summary: dict[str, Any]) -> str | None:
    """Return the display stage for the pending-human-action summary."""

    if summary.get("supervision_state") != "blocked_human":
        return None
    requirements = summary.get("decision_requirements") or {}
    if requirements.get("stage"):
        return str(requirements["stage"])
    next_action = summary.get("next_action") or {}
    return str(next_action["stage"]) if next_action.get("stage") else None


def _failed_workflow_step_id(
    events: list[dict[str, Any]], summary: dict[str, Any]
) -> str | None:
    if summary.get("supervision_state") != "failed_terminal":
        return None
    for event in reversed(events):
        if event.get("event_type") in {"stage_failed", "attempt_failed"}:
            step_id = event.get("workflow_step_id")
            if isinstance(step_id, str) and step_id:
                return step_id
    return None


def _final_or_current_stage(
    stages: list[dict[str, Any]], summary: dict[str, Any]
) -> dict[str, Any]:
    if summary.get("current_stage"):
        return {"stage": summary["current_stage"], "source": "ledger"}
    last_active = None
    for stage in stages:
        if stage["status"] != STAGE_STATUS_NO_EVIDENCE:
            last_active = stage["stage"]
    if last_active:
        return {"stage": last_active, "source": "durable_evidence"}
    return {"stage": None, "source": GAP_UNAVAILABLE}


def _build_gaps(
    session: dict[str, Any] | None,
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    review_occurrences: list[dict[str, Any]],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    if session is None:
        gaps.append(
            {
                "field": "session",
                "kind": GAP_UNAVAILABLE,
                "detail": "No session_created event was found for this run.",
            }
        )
    route_events = [
        event
        for event in events
        if event.get("event_type")
        in {"stage_entered", "stage_skipped", "stage_completed"}
    ]
    if session is not None and len(events) > 1 and not route_events:
        gaps.append(
            {
                "field": "route",
                "kind": GAP_UNAVAILABLE,
                "detail": (
                    "No explicit stage_entered/stage_skipped/stage_completed events "
                    "were recorded."
                ),
            }
        )
    request_occurrences = {
        (
            str(occurrence.get("workflow_step_id") or ""),
            str(occurrence.get("review_request_id") or ""),
            str(occurrence.get("request_hash") or ""),
        )
        for occurrence in review_occurrences
    }
    orphan_decisions = [
        decision
        for decision in decisions
        if (
            str(decision.get("workflow_step_id") or ""),
            str(decision.get("review_request_id") or decision.get("subject_id") or ""),
            str(decision.get("request_hash") or ""),
        )
        not in request_occurrences
    ]
    if orphan_decisions:
        gaps.append(
            {
                "field": "review_bindings",
                "kind": GAP_UNAVAILABLE,
                "detail": (
                    f"{len(orphan_decisions)} review decision event(s) have no "
                    "matching workflow occurrence, request ID, and request hash."
                ),
            }
        )
    review_errors = [
        str(error)
        for error in summary.get("review_status_integrity_errors") or []
        if error
    ]
    review_errors.extend(
        str(occurrence["integrity_error"])
        for occurrence in review_occurrences
        if occurrence.get("integrity_error")
    )
    if review_errors:
        gaps.append(
            {
                "field": "review_status",
                "kind": GAP_UNAVAILABLE,
                "detail": "; ".join(dict.fromkeys(review_errors))[:4096],
            }
        )
    return gaps


def _route_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    route: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") not in {
            "stage_entered",
            "stage_skipped",
            "stage_completed",
        }:
            continue
        record = {
            "sequence": event.get("sequence"),
            "event_type": event.get("event_type"),
            "stage": event.get("stage"),
            "workflow_step_id": event.get("workflow_step_id"),
            "timestamp": event.get("timestamp"),
        }
        if event.get("reason"):
            record["reason"] = event["reason"]
        if event.get("route_node"):
            record["route_node"] = event["route_node"]
        route.append(record)
    return route


def _event_sequence(
    record: dict[str, Any] | None,
    *,
    key: str = "sequence",
) -> int | None:
    if not isinstance(record, dict):
        return None
    sequence = record.get(key)
    if isinstance(sequence, int):
        return sequence
    if isinstance(sequence, str) and sequence.isdigit():
        return int(sequence)
    return None


def _first_error_line(error: Any) -> str | None:
    if not error:
        return None
    text = str(error).strip()
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text
    # The LocalRunner conda wrapper appends a generic "conda run ... failed"
    # line that buries the real tool error; prefer the last meaningful line.
    meaningful = [line for line in lines if "conda.cli.main_run" not in line]
    return (meaningful or lines)[-1]


def render_trace_markdown(trace: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Session Summary: {trace.get('session_id')}")
    lines.append("")
    if trace.get("error"):
        lines.append(f"> {trace['error']}: no durable ledger session was found.")
        lines.append("")
    lines.append(f"- supervision_state: {trace.get('supervision_state')}")
    final_stage = trace.get("final_or_current_stage") or {}
    lines.append(
        f"- final_or_current_stage: {final_stage.get('stage')} "
        f"(source: {final_stage.get('source')})"
    )
    lines.append(f"- manifest: {trace.get('input_manifest_ref')}")
    lines.append(f"- checkpoint_persistence: {trace.get('checkpoint_persistence')}")
    lines.append(f"- created_at: {trace.get('created_at')}")
    lines.append(
        f"- ledger_events: {trace.get('ledger_event_count')}, "
        f"artifacts: {trace.get('artifact_count')}"
    )
    by_origin = trace.get("decisions_by_origin") or {}
    lines.append(f"- decisions_by_origin: {by_origin}")
    watermark = trace.get("source_watermark") or {}
    lines.append(
        "- source_watermark: "
        f"ledger_sequence={watermark.get('last_ledger_sequence')}, "
        f"artifact_index_sha256={watermark.get('artifact_index_sha256')}"
    )
    lines.append("")

    lines.append("## Stages")
    lines.append("")
    for stage in trace.get("stages", []):
        lines.append(
            f"### {stage['workflow_step_id']} / {stage['stage']} — {stage['status']}"
        )
        timing = stage.get("timing") or {}
        if timing:
            span = " ".join(f"{key}={value}" for key, value in timing.items())
            lines.append(f"- timing: {span}")
        for method in stage.get("methods", []):
            suffix = (
                f" (rc={method['return_code']})"
                if method.get("return_code") is not None
                else ""
            )
            line = (
                f"- method {method.get('method_name')}: {method.get('outcome')}{suffix}"
            )
            if method.get("error"):
                line += f" — {method['error']}"
            lines.append(line)
        for candidate in stage.get("candidate_methods", []):
            enabled = candidate.get("execution_enabled")
            lines.append(
                f"- candidate {candidate.get('method_name')}: "
                f"execution_enabled={enabled}"
            )
        for review in stage.get("reviews") or []:
            if review.get("decision"):
                lines.append(
                    f"- review {review.get('review_request_id')}: "
                    f"{review.get('decision')} by {review.get('decided_by')} "
                    f"[{review.get('status')}]"
                    + (
                        f" (candidate {review['candidate_id']})"
                        if review.get("candidate_id")
                        else ""
                    )
                )
            elif review.get("reason"):
                lines.append(
                    f"- review {review.get('review_request_id')}: requested "
                    f"({review.get('reason')}) [{review.get('status')}]"
                )
            for ref_key in (
                "review_request_artifact_ref",
                "rationale_artifact_ref",
                "decision_artifact_ref",
            ):
                if review.get(ref_key):
                    lines.append(f"  - {ref_key}: {review[ref_key]}")
        for ref in stage.get("artifact_refs", []):
            lines.append(
                f"- artifact {ref.get('artifact_type')}: {ref.get('path_ref')}"
            )
        lines.append("")

    blocked = trace.get("blocked")
    if blocked:
        lines.append("## Pending Human Action")
        lines.append("")
        lines.append(f"- stage: {blocked.get('stage')}")
        lines.append(f"- decisions_allowed: {blocked.get('decisions_allowed')}")
        if blocked.get("pending_human_action"):
            lines.append(f"- action: {blocked['pending_human_action']}")
        if blocked.get("resume_command"):
            lines.append(f"- resume_command: {blocked['resume_command']}")
        if blocked.get("review_request_artifact_ref"):
            lines.append(f"- review_request: {blocked['review_request_artifact_ref']}")
        lines.append("")

    failures = trace.get("failures") or []
    if failures:
        lines.append("## Failures")
        lines.append("")
        for failure in failures:
            detail = failure.get("error") or failure.get("detail") or ""
            lines.append(
                f"- {failure.get('stage')} / {failure.get('method_name')}: {detail}"
            )
        lines.append("")

    open_first = trace.get("open_first") or []
    if open_first:
        lines.append("## Open First")
        lines.append("")
        for artifact in open_first:
            lines.append(
                f"- {artifact.get('artifact_type')}: {artifact.get('path_ref')}"
            )
        lines.append("")

    gaps = trace.get("gaps") or []
    if gaps:
        lines.append("## Durable-Record Gaps")
        lines.append("")
        for gap in gaps:
            lines.append(
                f"- {gap.get('field')} [{gap.get('kind')}]: {gap.get('detail')}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_session_trace(data_dir: str | Path, session_id: str) -> dict[str, Any]:
    """Regenerate JSON and Markdown views under ``reports/`` only."""
    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    trace = build_session_trace(data_dir, session_id)
    markdown_text = render_trace_markdown(trace)

    if not trace.get("session_found"):
        return {
            "trace": trace,
            "json_ref": None,
            "markdown_ref": None,
            "markdown": markdown_text,
        }

    json_ref = paths.report_path(SESSION_SUMMARY_JSON_FILENAME)
    markdown_ref = paths.report_path(SESSION_SUMMARY_MARKDOWN_FILENAME)
    json_text = json.dumps(trace, ensure_ascii=False, indent=2, default=str) + "\n"
    atomic_write_text(json_ref, json_text)
    atomic_write_text(markdown_ref, markdown_text)

    return {
        "trace": trace,
        "json_ref": str(json_ref),
        "markdown_ref": str(markdown_ref),
        "markdown": markdown_text,
    }
