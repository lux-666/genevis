"""Session run trace built from durable ledger and artifact records.

The trace reconstructs a stage-by-stage explanation of what happened in a
session using only durable records (``ledger/<session>/session.json``,
``events.jsonl``, ``artifacts/<session>/artifacts.jsonl`` and the operator/
final reports those produce). It never requires a live Python process or a
checkpoint to reconstruct facts that already exist in ledger or artifacts.

Where a fact cannot be reconstructed from durable records it is surfaced as a
gap rather than fabricated. The most important known gap is the authoritative
ordered ``route_trace``: it lives only in checkpoint/in-memory state, so the
trace reconstructs an evidence-based stage path and marks the authoritative
ordering as ``checkpoint_only``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.control.lifecycle import inspect_session
from app.execution.artifacts import append_artifact_records, read_artifact_records
from app.review.records import counts_by_origin, decision_records
from app.schemas.artifacts import ArtifactMeta
from app.stages import STAGE_ORDER

# Stable stage-status vocabulary. Keep this small and durable; the trace
# JSON and any downstream tooling depend on these exact strings.
STAGE_STATUS_EXECUTED = "executed"
STAGE_STATUS_PLANNED_ONLY = "planned_only"
STAGE_STATUS_BLOCKED_HUMAN = "blocked_human"
STAGE_STATUS_FAILED = "failed"
STAGE_STATUS_COMPLETED = "completed"
STAGE_STATUS_NO_EVIDENCE = "no_durable_evidence"

# Durable-record gap vocabulary. Each gap names a fact the trace could not
# reconstruct and why, so operators do not mistake "unavailable" for "absent".
GAP_UNAVAILABLE = "unavailable"  # no durable record carries this fact
GAP_CHECKPOINT_ONLY = "checkpoint_only"  # only the checkpoint/in-memory state has it
GAP_UNSUPPORTED = "unsupported"  # current records are too coarse to represent it

TRACE_JSON_ARTIFACT_TYPE = "run_trace.session"
TRACE_MARKDOWN_ARTIFACT_TYPE = "run_trace.session_markdown"

# The trace registers its own artifacts; exclude them from operator-facing
# counts so re-running --trace does not inflate the workflow artifact total.
_TRACE_ARTIFACT_PREFIX = "run_trace."

# Artifact types that are workflow plumbing, not operator-facing evidence.
_NOISE_ARTIFACT_TYPES = frozenset({"command.stdout", "command.stderr"})
_ANNOTATION_INTAKE_ARTIFACT_TYPE = "annotation.intake_plan"
# Artifact-type prefixes the operator should open first, in priority order.
_OPEN_FIRST_PREFIXES = (
    "operator_report.",
    "final_report.",
    "annotation.",
    "function_annotation.",
    "assembly_review.",
    "repeat_annotation.",
    "gene_annotation.",
)


def build_session_trace(data_dir: str | Path, session_id: str) -> dict[str, Any]:
    """Reconstruct a structured run trace from durable session records."""

    ledger = Ledger(data_dir)
    session = ledger.read_session(session_id)
    events = ledger.read_events(session_id)
    artifacts = read_artifact_records(data_dir, session_id)
    summary = inspect_session(session_id, data_dir)

    decisions = decision_records(events)
    workflow_artifacts = [
        artifact
        for artifact in artifacts
        if not str(artifact.get("artifact_type", "")).startswith(_TRACE_ARTIFACT_PREFIX)
    ]
    stages = _build_stage_records(events, workflow_artifacts, summary)
    gaps = _build_gaps(session, events, decisions)

    trace: dict[str, Any] = {
        "session_id": session_id,
        "session_found": session is not None,
        "input_manifest_ref": (session or {}).get("input_manifest_ref"),
        "data_dir": str(data_dir),
        "created_at": (session or {}).get("created_at"),
        "supervision_state": summary.get("supervision_state"),
        "checkpoint_persistence": summary.get("checkpoint_persistence"),
        "durable_resume_available": summary.get("durable_resume_available"),
        "final_or_current_stage": _final_or_current_stage(stages, summary),
        "stage_path_observed": [stage["stage"] for stage in stages if stage["status"] != STAGE_STATUS_NO_EVIDENCE],
        "stages": stages,
        "decisions": [_decision_record(decision) for decision in decisions],
        "decisions_by_origin": counts_by_origin(decisions),
        "open_first": _open_first_artifacts(workflow_artifacts),
        "failures": _failures(events),
        "blocked": _blocked_summary(summary),
        "next_action": summary.get("next_action"),
        "ledger_event_count": len(events),
        "artifact_count": len(workflow_artifacts),
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
    blocked_stage = _blocked_stage(summary)
    failed_stage = _failed_stage(events, summary)

    records: list[dict[str, Any]] = []
    for stage in STAGE_ORDER:
        methods = methods_by_stage.get(stage, [])
        stage_artifacts = artifacts_by_stage.get(stage, [])
        review = review_by_stage.get(stage)
        candidate_methods = candidate_methods_by_stage.get(stage, [])
        status = _stage_status(
            stage,
            methods=methods,
            stage_artifacts=stage_artifacts,
            candidate_methods=candidate_methods,
            review=review,
            blocked_stage=blocked_stage,
            failed_stage=failed_stage,
        )
        record: dict[str, Any] = {
            "stage": stage,
            "status": status,
            "methods": methods,
            "artifact_refs": _stage_artifacts_for_trace(stage_artifacts),
            "timing": _stage_timing(methods, review),
        }
        if candidate_methods:
            record["candidate_methods"] = candidate_methods
        if review:
            record["review"] = review
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
                "method_name": artifact.get("method_name"),
                "file_path": artifact.get("file_path"),
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
        metadata = artifact.get("metadata") or {}
        by_stage.setdefault(str(artifact.get("stage", "")), []).append(
            {
                "method_name": artifact.get("method_name"),
                "candidate_id": metadata.get("candidate_id"),
                "execution_enabled": metadata.get("execution_enabled"),
            }
        )
    return by_stage


def _review_events_by_stage(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_stage: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = event.get("event_type")
        stage = str(event.get("stage", ""))
        if not stage:
            continue
        if event_type == "review_requested":
            entry = by_stage.setdefault(stage, {})
            entry["requested_at"] = event.get("timestamp")
            entry["reason"] = event.get("reason")
            entry["review_request_artifact_ref"] = event.get("review_request_artifact_ref")
            entry["auto_decidable"] = event.get("auto_decidable")
            entry["policy_mode"] = event.get("policy_mode")
            entry["pending_human_confirmation"] = event.get("pending_human_confirmation")
        elif event_type == "review_decision":
            entry = by_stage.setdefault(stage, {})
            entry["decided_at"] = event.get("timestamp")
            entry["decision"] = event.get("decision")
            entry["decided_by"] = event.get("decided_by") or "human"
            entry["candidate_id"] = event.get("candidate_id")
            entry["method_name"] = event.get("method_name")
            entry["rationale_artifact_ref"] = event.get("rationale_artifact_ref")
            entry["decision_artifact_ref"] = event.get("artifact_ref")
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
) -> str:
    if stage == failed_stage and any(method["outcome"] == "failed" for method in methods):
        return STAGE_STATUS_FAILED
    if stage == blocked_stage:
        return STAGE_STATUS_BLOCKED_HUMAN
    if any(method["outcome"] == "succeeded" for method in methods):
        return STAGE_STATUS_EXECUTED
    if any(method["outcome"] == "failed" for method in methods):
        return STAGE_STATUS_FAILED
    if review and review.get("decision"):
        return STAGE_STATUS_COMPLETED
    if candidate_methods or stage_artifacts:
        return STAGE_STATUS_PLANNED_ONLY
    return STAGE_STATUS_NO_EVIDENCE


def _stage_timing(methods: list[dict[str, Any]], review: dict[str, Any] | None) -> dict[str, Any]:
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
    return timing


def _decision_record(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": decision.get("stage"),
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
                    "file_path": artifact.get("file_path"),
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
        "review_request_artifact_ref": (summary.get("latest_review_request_artifact") or {}).get("file_path"),
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
    payload = summary.get("latest_operator_report_payload") or {}
    if payload.get("current_stage"):
        return str(payload["current_stage"])
    next_action = summary.get("next_action") or {}
    if next_action.get("stage"):
        return str(next_action["stage"])
    for event in reversed(events):
        if event.get("event_type") == "attempt_failed":
            return str(event.get("stage"))
    return None


def _final_or_current_stage(stages: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    payload = summary.get("latest_operator_report_payload") or {}
    if payload.get("current_stage"):
        return {"stage": payload["current_stage"], "source": "operator_report"}
    last_active = None
    for stage in stages:
        if stage["status"] != STAGE_STATUS_NO_EVIDENCE:
            last_active = stage["stage"]
    if last_active:
        return {"stage": last_active, "source": GAP_CHECKPOINT_ONLY}
    return {"stage": None, "source": GAP_UNAVAILABLE}


def _build_gaps(
    session: dict[str, Any] | None,
    events: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = [
        {
            "field": "route_trace",
            "kind": GAP_CHECKPOINT_ONLY,
            "detail": "The authoritative ordered route_trace lives only in checkpoint/in-memory state. The trace reconstructs an evidence-based stage path from ledger and artifact records instead.",
        },
        {
            "field": "skip_reasons",
            "kind": GAP_CHECKPOINT_ONLY,
            "detail": "Per-stage skip reasons are state fields not persisted to the ledger. Stages without durable evidence are reported as no_durable_evidence rather than skipped-with-reason.",
        },
    ]
    if session is None:
        gaps.append(
            {
                "field": "session",
                "kind": GAP_UNAVAILABLE,
                "detail": "No durable ledger session.json was found for this session id.",
            }
        )
    if _has_multiple_requests_per_stage(events):
        gaps.append(
            {
                "field": "review_requests",
                "kind": GAP_UNSUPPORTED,
                "detail": "A stage emitted more than one review_requested event. Durable records bind decisions at stage granularity, so multiple requests in one stage are not represented per-request.",
            }
        )
    return gaps


def _has_multiple_requests_per_stage(events: list[dict[str, Any]]) -> bool:
    counts: dict[str, int] = {}
    for event in events:
        if event.get("event_type") == "review_requested":
            stage = str(event.get("stage", ""))
            counts[stage] = counts.get(stage, 0) + 1
    return any(count > 1 for count in counts.values())


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
    lines.append(f"# Run Trace: {trace.get('session_id')}")
    lines.append("")
    if trace.get("error"):
        lines.append(f"> {trace['error']}: no durable ledger session was found.")
        lines.append("")
    lines.append(f"- supervision_state: {trace.get('supervision_state')}")
    final_stage = trace.get("final_or_current_stage") or {}
    lines.append(f"- final_or_current_stage: {final_stage.get('stage')} (source: {final_stage.get('source')})")
    lines.append(f"- manifest: {trace.get('input_manifest_ref')}")
    lines.append(f"- data_dir: {trace.get('data_dir')}")
    lines.append(f"- checkpoint_persistence: {trace.get('checkpoint_persistence')}")
    lines.append(f"- created_at: {trace.get('created_at')}")
    lines.append(f"- ledger_events: {trace.get('ledger_event_count')}, artifacts: {trace.get('artifact_count')}")
    by_origin = trace.get("decisions_by_origin") or {}
    lines.append(f"- decisions_by_origin: {by_origin}")
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
            lines.append(f"- artifact {ref.get('artifact_type')}: {ref.get('file_path')}")
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
            lines.append(f"- {artifact.get('artifact_type')}: {artifact.get('file_path')}")
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
    """Build and durably register the run trace as JSON and Markdown artifacts.

    Regeneration is idempotent: unchanged trace content reuses the existing
    artifact paths and does not append duplicate artifact-index rows or
    ledger registration events.

    A trace for a session with no durable ledger record is still built and
    returned for display, but nothing is written to disk: there is no session
    to attach artifacts to, so persisting files would only leave orphans under
    ``artifacts/run_trace/<id>/``. In that case ``json_ref`` and
    ``markdown_ref`` are ``None``.
    """
    trace = build_session_trace(data_dir, session_id)
    markdown_text = render_trace_markdown(trace)

    if not trace.get("session_found"):
        return {
            "trace": trace,
            "json_ref": None,
            "markdown_ref": None,
            "markdown": markdown_text,
        }

    out_dir = Path(data_dir) / "artifacts" / "run_trace" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    json_ref = out_dir / "run_trace.json"
    markdown_ref = out_dir / "run_trace.md"
    json_text = json.dumps(trace, ensure_ascii=False, indent=2, default=str)

    ledger = Ledger(data_dir)
    ledger.create_session(session_id, str(trace.get("input_manifest_ref") or ""))

    _write_trace_artifact(data_dir, session_id, json_ref, json_text, TRACE_JSON_ARTIFACT_TYPE, ledger)
    _write_trace_artifact(data_dir, session_id, markdown_ref, markdown_text, TRACE_MARKDOWN_ARTIFACT_TYPE, ledger)

    return {
        "trace": trace,
        "json_ref": str(json_ref),
        "markdown_ref": str(markdown_ref),
        "markdown": markdown_text,
    }


def _write_trace_artifact(
    data_dir: str | Path,
    session_id: str,
    ref: Path,
    text: str,
    artifact_type: str,
    ledger: Ledger,
) -> None:
    already_current = _path_has_same_content(ref, text) and _artifact_record_exists(
        data_dir, session_id, artifact_type, ref
    )
    if already_current:
        return
    ref.write_text(text, encoding="utf-8")
    if _artifact_record_exists(data_dir, session_id, artifact_type, ref):
        # Content changed in place but the index row already points here;
        # do not append a duplicate registration row.
        return
    artifact = ArtifactMeta.from_file(
        ref,
        stage="run_trace",
        method_name=artifact_type,
        artifact_type=artifact_type,
        metadata={"session_id": session_id},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    ledger.record_artifact_registered(session_id, artifact.model_dump(mode="json"))


def _artifact_record_exists(data_dir: str | Path, session_id: str, artifact_type: str, ref: Path) -> bool:
    for record in read_artifact_records(data_dir, session_id):
        if record.get("artifact_type") == artifact_type and Path(record.get("file_path", "")) == ref:
            return True
    return False


def _path_has_same_content(path: Path, expected: str) -> bool:
    if not path.exists():
        return False
    try:
        return path.read_text(encoding="utf-8") == expected
    except OSError:
        return False
