"""Unified session-summary projection from ledger v2 and the artifact index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.storage.ledger import Ledger
from app.control.lifecycle import inspect_session
from app.storage.artifacts import read_artifact_records
from app.reports._projection import atomic_write_text, source_watermark
from app.reports.review_report import _pending_confirmations
from app.review.records import counts_by_origin
from app.workflow import STAGE_ORDER
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
_ANNOTATION_INTAKE_ARTIFACT_TYPE = "annotation.intake_plan"
# Artifact-type prefixes the operator should open first, in priority order.
_OPEN_FIRST_PREFIXES = (
    "annotation.",
    "function_annotation.",
    "assembly_review.",
    "repeat_annotation.",
    "gene_annotation.",
)


def build_session_trace(data_dir: str | Path, session_id: str) -> dict[str, Any]:
    """Reconstruct the structured session-summary model from durable facts."""

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    session = ledger.read_session(session_id)
    events = ledger.read_events(session_id)
    artifacts = read_artifact_records(paths.run_dir, session_id)
    summary = inspect_session(session_id, paths.run_dir)

    decisions = [event for event in events if event.get("event_type") == "review_decision"]
    stages = _build_stage_records(events, artifacts, summary)
    gaps = _build_gaps(session, events, decisions)
    route = _route_records(events)
    observed_path = [record["stage"] for record in route if record["event_type"] == "stage_entered"]
    if not observed_path:
        observed_path = [
            stage["stage"]
            for stage in stages
            if stage["status"] != STAGE_STATUS_NO_EVIDENCE
        ]

    trace: dict[str, Any] = {
        "schema_version": 2,
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
        "pending_human_confirmations": _pending_confirmations(events),
        "open_first": _open_first_artifacts(artifacts),
        "failures": _failures(events),
        "blocked": _blocked_summary(summary),
        "next_action": summary.get("next_action"),
        "ledger_event_count": len(events),
        "artifact_count": len(artifacts),
        "gaps": gaps,
    }
    if session is None:
        trace["error"] = "session_not_found"
    return trace


def _build_stage_records(
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    methods_by_stage = _methods_by_stage(events)
    artifacts_by_stage = _artifact_refs_by_stage(artifacts)
    candidate_methods_by_stage = _candidate_methods_by_stage(artifacts)
    review_by_stage = _review_events_by_stage(events)
    lifecycle_by_stage = _stage_lifecycle_by_stage(events)
    blocked_stage = _blocked_stage(summary)
    failed_stage = _failed_stage(events, summary)

    records: list[dict[str, Any]] = []
    for stage in STAGE_ORDER:
        methods = methods_by_stage.get(stage, [])
        stage_artifacts = artifacts_by_stage.get(stage, [])
        review = review_by_stage.get(stage)
        candidate_methods = candidate_methods_by_stage.get(stage, [])
        lifecycle = lifecycle_by_stage.get(stage)
        status = _stage_status(
            stage,
            methods=methods,
            stage_artifacts=stage_artifacts,
            candidate_methods=candidate_methods,
            review=review,
            blocked_stage=blocked_stage,
            failed_stage=failed_stage,
            lifecycle=lifecycle,
        )
        record: dict[str, Any] = {
            "stage": stage,
            "status": status,
            "methods": methods,
            "artifact_refs": _stage_artifacts_for_trace(stage_artifacts),
            "timing": _stage_timing(methods, review, lifecycle),
        }
        if candidate_methods:
            record["candidate_methods"] = candidate_methods
        if review:
            record["review"] = review
        if lifecycle:
            record["lifecycle"] = lifecycle
        records.append(record)
    return records


def _methods_by_stage(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    attempts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        event_type = event.get("event_type")
        if event_type not in {"attempt_started", "attempt_succeeded", "attempt_failed"}:
            continue
        attempt_id = event.get("attempt_id") or f"{event.get('stage')}:{event.get('method_name')}"
        record = attempts.get(attempt_id)
        if record is None:
            record = {
                "attempt_id": attempt_id,
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
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for attempt_id in order:
        record = attempts[attempt_id]
        by_stage.setdefault(str(record["stage"]), []).append(record)
    return by_stage


def _artifact_refs_by_stage(artifacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        artifact_type = str(artifact.get("artifact_type", ""))
        if artifact_type in _NOISE_ARTIFACT_TYPES:
            continue
        stage = str(artifact.get("stage", ""))
        by_stage.setdefault(stage, []).append(
            {
                "artifact_type": artifact_type,
                "method_name": artifact.get("method_id") or artifact.get("method_name"),
                "path_ref": artifact.get("path_ref") or artifact.get("file_path"),
            }
        )
    return by_stage


def _stage_artifacts_for_trace(stage_artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = list(stage_artifacts[:5])
    if any(artifact.get("artifact_type") == _ANNOTATION_INTAKE_ARTIFACT_TYPE for artifact in selected):
        return selected
    intake = next(
        (
            artifact
            for artifact in reversed(stage_artifacts)
            if artifact.get("artifact_type") == _ANNOTATION_INTAKE_ARTIFACT_TYPE
        ),
        None,
    )
    if intake is not None:
        selected.append(intake)
    return selected


def _candidate_methods_by_stage(artifacts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifacts:
        if artifact.get("artifact_type") != "draft_assembly.candidate":
            continue
        by_stage.setdefault(str(artifact.get("stage", "")), []).append(
            {
                "method_name": artifact.get("method_id") or artifact.get("method_name"),
                "candidate_id": artifact.get("candidate_id"),
                "execution_enabled": (artifact.get("provenance") or {}).get(
                    "execution_enabled"
                ),
            }
        )
    return by_stage


def _review_events_by_stage(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    requests: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        event_type = event.get("event_type")
        stage = str(event.get("stage", ""))
        if not stage:
            continue
        request_id = str(event.get("review_request_id") or event.get("subject_id") or "")
        if event_type == "review_requested":
            entry = requests.setdefault(request_id, {"stage": stage})
            if request_id not in order:
                order.append(request_id)
            entry["review_request_id"] = request_id
            entry["requested_at"] = event.get("timestamp")
            entry["reason"] = event.get("reason")
            entry["review_request_artifact_ref"] = event.get("review_request_artifact_ref")
            entry["auto_decidable"] = event.get("auto_decidable")
            entry["policy_mode"] = event.get("policy_mode")
            entry["pending_human_confirmation"] = event.get("pending_human_confirmation")
        elif event_type == "review_decision":
            entry = requests.setdefault(request_id, {"stage": stage, "review_request_id": request_id})
            if request_id not in order:
                order.append(request_id)
            entry["decided_at"] = event.get("timestamp")
            entry["decision"] = event.get("decision")
            entry["decided_by"] = event.get("decided_by") or "human"
            entry["candidate_id"] = event.get("candidate_id")
            entry["method_name"] = event.get("method_name")
            entry["rationale_artifact_ref"] = event.get("rationale_artifact_ref")
            entry["decision_artifact_ref"] = event.get("artifact_ref")
    by_stage: dict[str, dict[str, Any]] = {}
    for request_id in order:
        entry = requests[request_id]
        by_stage[str(entry["stage"])] = entry
    return by_stage


def _stage_lifecycle_by_stage(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_stage: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") not in {"stage_entered", "stage_skipped", "stage_completed"}:
            continue
        stage = str(event.get("stage") or "")
        if not stage:
            continue
        entry = by_stage.setdefault(stage, {})
        if event["event_type"] == "stage_entered":
            entry["entered_at"] = event.get("timestamp")
        elif event["event_type"] == "stage_skipped":
            entry["skipped_at"] = event.get("timestamp")
            entry["skip_reason"] = event.get("reason")
        else:
            entry["completed_at"] = event.get("timestamp")
    return by_stage


def _stage_status(
    stage: str,
    *,
    methods: list[dict[str, Any]],
    stage_artifacts: list[dict[str, Any]],
    candidate_methods: list[dict[str, Any]],
    review: dict[str, Any] | None,
    blocked_stage: str | None,
    failed_stage: str | None,
    lifecycle: dict[str, Any] | None,
) -> str:
    if stage == failed_stage:
        return STAGE_STATUS_FAILED
    if stage == blocked_stage:
        return STAGE_STATUS_BLOCKED_HUMAN
    if any(method["outcome"] == "succeeded" for method in methods):
        return STAGE_STATUS_EXECUTED
    if any(method["outcome"] == "failed" for method in methods):
        return STAGE_STATUS_FAILED
    if lifecycle and lifecycle.get("skipped_at"):
        return STAGE_STATUS_SKIPPED
    if lifecycle and lifecycle.get("completed_at"):
        return STAGE_STATUS_COMPLETED
    if review and review.get("decision"):
        return STAGE_STATUS_COMPLETED
    if candidate_methods or stage_artifacts:
        return STAGE_STATUS_PLANNED_ONLY
    return STAGE_STATUS_NO_EVIDENCE


def _stage_timing(
    methods: list[dict[str, Any]],
    review: dict[str, Any] | None,
    lifecycle: dict[str, Any] | None,
) -> dict[str, Any]:
    timestamps = [method.get("started_at") for method in methods if method.get("started_at")]
    finished = [method.get("finished_at") for method in methods if method.get("finished_at")]
    timing: dict[str, Any] = {}
    if timestamps:
        timing["started_at"] = min(timestamps)
    if finished:
        timing["finished_at"] = max(finished)
    if review and review.get("requested_at"):
        timing["review_requested_at"] = review["requested_at"]
    if review and review.get("decided_at"):
        timing["review_decided_at"] = review["decided_at"]
    if lifecycle:
        for key in ("entered_at", "skipped_at", "completed_at"):
            if lifecycle.get(key):
                timing[key] = lifecycle[key]
    return timing


def _decision_record(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_request_id": decision.get("review_request_id") or decision.get("subject_id"),
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
        "stage": requirements.get("stage") or next_action.get("stage"),
        "decisions_allowed": requirements.get("decisions_allowed") or [],
        "pending_human_action": next_action.get("summary"),
        "resume_command": next_action.get("resume_command"),
        "review_request_artifact_ref": (
            summary.get("latest_review_request_artifact") or {}
        ).get("path_ref")
        or (summary.get("latest_review_request_artifact") or {}).get("file_path"),
    }


def _blocked_stage(summary: dict[str, Any]) -> str | None:
    if summary.get("supervision_state") != "blocked_human":
        return None
    requirements = summary.get("decision_requirements") or {}
    if requirements.get("stage"):
        return str(requirements["stage"])
    next_action = summary.get("next_action") or {}
    return str(next_action["stage"]) if next_action.get("stage") else None


def _failed_stage(events: list[dict[str, Any]], summary: dict[str, Any]) -> str | None:
    if summary.get("supervision_state") != "failed_terminal":
        return None
    if summary.get("current_stage"):
        return str(summary["current_stage"])
    next_action = summary.get("next_action") or {}
    if next_action.get("stage"):
        return str(next_action["stage"])
    for event in reversed(events):
        if event.get("event_type") == "attempt_failed":
            return str(event.get("stage"))
    return None


def _final_or_current_stage(stages: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
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
        if event.get("event_type") in {"stage_entered", "stage_skipped", "stage_completed"}
    ]
    if session is not None and len(events) > 1 and not route_events:
        gaps.append(
            {
                "field": "route",
                "kind": GAP_UNAVAILABLE,
                "detail": "No explicit stage_entered/stage_skipped/stage_completed events were recorded.",
            }
        )
    request_ids = {
        str(event.get("review_request_id") or event.get("subject_id"))
        for event in events
        if event.get("event_type") == "review_requested"
    }
    orphan_decisions = [
        decision
        for decision in decisions
        if str(decision.get("review_request_id") or decision.get("subject_id")) not in request_ids
    ]
    if orphan_decisions:
        gaps.append(
            {
                "field": "review_bindings",
                "kind": GAP_UNAVAILABLE,
                "detail": f"{len(orphan_decisions)} review decision event(s) have no matching request ID.",
            }
        )
    return gaps


def _route_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    route: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") not in {"stage_entered", "stage_skipped", "stage_completed"}:
            continue
        record = {
            "sequence": event.get("sequence"),
            "event_type": event.get("event_type"),
            "stage": event.get("stage"),
            "timestamp": event.get("timestamp"),
        }
        if event.get("reason"):
            record["reason"] = event["reason"]
        if event.get("route_node"):
            record["route_node"] = event["route_node"]
        route.append(record)
    return route


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
    lines.append(f"- final_or_current_stage: {final_stage.get('stage')} (source: {final_stage.get('source')})")
    lines.append(f"- manifest: {trace.get('input_manifest_ref')}")
    lines.append(f"- checkpoint_persistence: {trace.get('checkpoint_persistence')}")
    lines.append(f"- created_at: {trace.get('created_at')}")
    lines.append(f"- ledger_events: {trace.get('ledger_event_count')}, artifacts: {trace.get('artifact_count')}")
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
        lines.append(f"### {stage['stage']} — {stage['status']}")
        timing = stage.get("timing") or {}
        if timing:
            span = " ".join(f"{key}={value}" for key, value in timing.items())
            lines.append(f"- timing: {span}")
        for method in stage.get("methods", []):
            suffix = f" (rc={method['return_code']})" if method.get("return_code") is not None else ""
            line = f"- method {method.get('method_name')}: {method.get('outcome')}{suffix}"
            if method.get("error"):
                line += f" — {method['error']}"
            lines.append(line)
        for candidate in stage.get("candidate_methods", []):
            enabled = candidate.get("execution_enabled")
            lines.append(f"- candidate {candidate.get('method_name')}: execution_enabled={enabled}")
        review = stage.get("review")
        if review:
            if review.get("decision"):
                lines.append(
                    f"- review: {review.get('decision')} by {review.get('decided_by')}"
                    + (f" (candidate {review['candidate_id']})" if review.get("candidate_id") else "")
                )
            elif review.get("reason"):
                lines.append(f"- review: requested ({review.get('reason')})")
            for ref_key in ("review_request_artifact_ref", "rationale_artifact_ref", "decision_artifact_ref"):
                if review.get(ref_key):
                    lines.append(f"  - {ref_key}: {review[ref_key]}")
        for ref in stage.get("artifact_refs", []):
            lines.append(f"- artifact {ref.get('artifact_type')}: {ref.get('path_ref')}")
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
            lines.append(f"- {failure.get('stage')} / {failure.get('method_name')}: {detail}")
        lines.append("")

    open_first = trace.get("open_first") or []
    if open_first:
        lines.append("## Open First")
        lines.append("")
        for artifact in open_first:
            lines.append(f"- {artifact.get('artifact_type')}: {artifact.get('path_ref')}")
        lines.append("")

    gaps = trace.get("gaps") or []
    if gaps:
        lines.append("## Durable-Record Gaps")
        lines.append("")
        for gap in gaps:
            lines.append(f"- {gap.get('field')} [{gap.get('kind')}]: {gap.get('detail')}")
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
