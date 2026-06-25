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
from app.review.gates import (
    apply_review_gate_decision,
    find_review_gate,
    get_review_gate,
    registered_review_stage_label,
)
from app.review.records import counts_by_origin, decision_records
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
    """Inspect durable ledger/artifact records and checkpoint availability."""

    from app.graph.checkpointer import checkpoint_db_path

    ledger = Ledger(data_dir)
    session = ledger.read_session(session_id)
    events = ledger.read_events(session_id)
    artifacts = read_artifact_records(data_dir, session_id)
    latest_review_request = _latest_event(events, "review_requested")
    latest_review_decision = _latest_event(events, "review_decision")
    latest_env_preflight_block = _latest_event(events, "env_preflight_blocked")
    review_decisions = decision_records(events)
    decisions_by_origin = counts_by_origin(review_decisions)
    latest_review_lifecycle_event = _latest_review_lifecycle_event(events)
    latest_unresolved_review_request = latest_review_lifecycle_event if latest_review_lifecycle_event and latest_review_lifecycle_event.get("event_type") == "review_requested" else None
    latest_request_artifact = _latest_artifact_by_suffix(artifacts, ".request")
    latest_decision_artifact = _latest_artifact_by_suffix(artifacts, ".decision")
    latest_failed_report = _latest_artifact_by_type(artifacts, "operator_report.failed_terminal")
    latest_blocked_report = _latest_artifact_by_type(artifacts, "operator_report.blocked_human")
    latest_final_report = _latest_artifact_by_type(artifacts, "final_report.review_decisions")
    latest_function_bundle = _latest_artifact_by_type(artifacts, "function_annotation.bundle")
    latest_operator_report = _latest_artifact_by_types(
        artifacts,
        {"operator_report.failed_terminal", "operator_report.blocked_human"},
    )
    latest_operator_report_payload = _read_json_artifact(latest_operator_report)

    supervision_state = "unknown"
    if latest_unresolved_review_request:
        supervision_state = "blocked_human"
    elif latest_final_report or latest_function_bundle:
        supervision_state = "completed"
    elif latest_failed_report:
        supervision_state = "failed_terminal"
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
        "latest_env_preflight_block": latest_env_preflight_block,
        "review_decisions": review_decisions,
        "decisions_by_origin": decisions_by_origin,
        "latest_review_lifecycle_event": latest_review_lifecycle_event,
        "latest_unresolved_review_request": latest_unresolved_review_request,
        "latest_review_request_artifact": latest_request_artifact,
        "latest_review_decision_artifact": latest_decision_artifact,
        "latest_operator_report": latest_operator_report,
        "latest_failed_operator_report": latest_failed_report,
        "latest_blocked_operator_report": latest_blocked_report,
        "latest_operator_report_payload": latest_operator_report_payload,
        "latest_final_report": latest_final_report,
        "latest_function_annotation_bundle": latest_function_bundle,
        "latest_relevant_artifacts": _latest_relevant_artifacts(artifacts),
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
    summary["next_action"] = build_next_action(summary, data_dir=data_dir)
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

    try:
        return apply_review_gate_decision(state, request)
    except KeyError as exc:
        raise ResumeValidationError(f"Unsupported resume stage: {request.stage}") from exc


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
    active_gate_id = active_interrupt.get("gate_id")
    if request.gate_id and active_gate_id and request.gate_id != active_gate_id:
        raise ResumeValidationError("resume gate_id does not match active_interrupt.gate_id")
    _ensure_decision_allowed_by_interrupt(active_interrupt, request)


def _ensure_decision_allowed_by_interrupt(active_interrupt: dict[str, Any], request: ResumeReviewRequest) -> None:
    allowed = active_interrupt.get("decisions_allowed")
    if allowed is None:
        gate = find_review_gate(request.stage, active_interrupt.get("gate_id"))
        if gate is not None:
            allowed = list(gate.decisions_allowed)
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
    try:
        update = apply_review_gate_decision(state, timeout_request)
    except KeyError as exc:
        raise ResumeValidationError(f"Unsupported timeout stage: {request.stage}") from exc
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
    gate = find_review_gate(stage, active_interrupt.get("gate_id"))
    decisions_allowed = active_interrupt.get("decisions_allowed")
    if decisions_allowed is None and gate is not None:
        decisions_allowed = list(gate.decisions_allowed)
    if stage == "assembly_review":
        return _with_review_policy_requirements(
            active_interrupt,
            {
                "stage": stage,
                "gate_id": active_interrupt.get("gate_id") or (gate.gate_id if gate else None),
                "decisions_allowed": decisions_allowed or [],
                "candidate_ids": [candidate.get("candidate_id") for candidate in active_interrupt.get("candidates", [])],
                "candidate_final_assembly_refs": _candidate_final_assembly_refs(active_interrupt.get("candidates", [])),
                "report_refs": dict(active_interrupt.get("report_refs") or {}),
                "candidate_comparison_context": dict(active_interrupt.get("candidate_comparison_context") or {}),
                "requires_structured_resume": True,
            },
        )
    if stage == "hic_scaffolding":
        return _with_review_policy_requirements(
            active_interrupt,
            {
                "stage": stage,
                "gate_id": active_interrupt.get("gate_id") or (gate.gate_id if gate else None),
                "decisions_allowed": decisions_allowed or [],
                "candidate_methods": _candidate_methods(active_interrupt),
                "executed_candidate_methods": [
                    name
                    for name, candidate in dict(active_interrupt.get("method_candidates") or {}).items()
                    if candidate.get("status") == "executed"
                ],
                "method_candidates": dict(active_interrupt.get("method_candidates") or {}),
                "requires_structured_resume": True,
            },
        )
    return _with_review_policy_requirements(
        active_interrupt,
        {
            "stage": stage,
            "gate_id": active_interrupt.get("gate_id") or (gate.gate_id if gate else None),
            "decisions_allowed": decisions_allowed or [],
            "candidate_ids": _candidate_ids(active_interrupt),
            "candidate_methods": _candidate_methods(active_interrupt),
            "requires_structured_resume": True,
        },
    )


def _candidate_final_assembly_refs(candidates: Any) -> dict[str, str]:
    if not isinstance(candidates, list):
        return {}
    refs: dict[str, str] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        final_ref = candidate.get("final_assembly_ref") or candidate.get("assembly_ref")
        if candidate_id and final_ref:
            refs[str(candidate_id)] = str(final_ref)
    return refs


def _candidate_ids(payload: dict[str, Any]) -> list[str]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        str(candidate.get("candidate_id"))
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("candidate_id") is not None
    ]


def _candidate_methods(payload: dict[str, Any]) -> list[str]:
    methods = payload.get("candidate_methods")
    if isinstance(methods, list):
        return [str(method) for method in methods]
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        str(candidate.get("method_name"))
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("method_name") is not None
    ]


def _with_review_policy_requirements(active_interrupt: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(requirements)
    for key in ("auto_decidable", "pending_human_confirmation", "review_request_artifact_ref"):
        if key in active_interrupt:
            enriched[key] = active_interrupt.get(key)
    policy = active_interrupt.get("review_policy")
    if isinstance(policy, dict) and policy.get("mode"):
        enriched["review_policy.mode"] = policy.get("mode")
    return enriched


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


def _latest_artifact_by_type(artifacts: list[dict[str, Any]], artifact_type: str) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        if artifact.get("artifact_type") == artifact_type:
            return artifact
    return None


def _latest_artifact_by_types(artifacts: list[dict[str, Any]], artifact_types: set[str]) -> dict[str, Any] | None:
    for artifact in reversed(artifacts):
        if artifact.get("artifact_type") in artifact_types:
            return artifact
    return None


def _latest_relevant_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    relevant_prefixes = (
        "operator_report.",
        "final_report.",
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


def _read_json_artifact(artifact: ArtifactMeta | dict[str, Any] | None) -> dict[str, Any] | None:
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
    return payload if isinstance(payload, dict) else None


def build_next_action(summary: dict[str, Any], *, data_dir: str | Path = "data") -> dict[str, Any]:
    if summary.get("error"):
        return {
            "kind": "inspect_unavailable",
            "summary": f"No durable ledger session found for {summary.get('session_id')!r}.",
            "required_fields": [],
            "relevant_refs": [],
        }

    requirements = summary.get("decision_requirements")
    if requirements:
        return _review_gate_next_action(summary, data_dir=data_dir)

    if summary.get("latest_final_report") or summary.get("latest_function_annotation_bundle"):
        refs = _artifact_refs(
            summary.get("latest_final_report"),
            summary.get("latest_function_annotation_bundle"),
        )
        return {
            "kind": "completed",
            "summary": "Workflow completed; inspect the listed final report and function annotation artifacts.",
            "required_fields": [],
            "relevant_refs": refs,
        }

    report_payload = summary.get("latest_operator_report_payload")
    if (
        summary.get("supervision_state") in {"blocked_human", "failed_terminal"}
        and isinstance(report_payload, dict)
        and isinstance(report_payload.get("next_action"), dict)
    ):
        return report_payload["next_action"]

    env_block = summary.get("latest_env_preflight_block")
    if env_block:
        return _env_block_next_action(env_block)

    if summary.get("supervision_state") == "failed_terminal":
        return _failed_terminal_next_action(summary)

    return {
        "kind": "unknown",
        "summary": "No active operator action was inferred from durable ledger and artifact records.",
        "required_fields": [],
        "relevant_refs": _artifact_refs(*summary.get("latest_relevant_artifacts", [])),
    }


def build_result_next_action(result: dict[str, Any]) -> dict[str, Any]:
    supervision = result.get("supervision_state")
    active_interrupt = result.get("active_interrupt")
    if supervision == "blocked_human" and active_interrupt:
        requirements = _decision_requirements(active_interrupt)
        checkpoint = result.get("checkpoint_persistence")
        summary = {
            "session_id": result.get("session_id"),
            "decision_requirements": requirements,
            "durable_resume_available": checkpoint != "memory",
        }
        return _review_gate_next_action(summary, data_dir=result.get("data_dir", "data"))
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
    try:
        gate = get_review_gate(stage, gate_id)
    except KeyError as exc:
        raise ResumeValidationError(
            f"no active registered review decision requirements; registered stages: {registered_review_stage_label()}"
        ) from exc

    decisions_allowed = list(requirements.get("decisions_allowed") or [])
    decision = _preferred_template_decision(gate, decisions_allowed)
    guidance = {
        "decisions_allowed": decisions_allowed,
        "review_request_artifact_ref": requirements.get("review_request_artifact_ref"),
    }
    if requirements.get("report_refs"):
        guidance["report_refs"] = dict(requirements.get("report_refs") or {})
    if requirements.get("candidate_comparison_context"):
        guidance["candidate_comparison_context"] = dict(requirements.get("candidate_comparison_context") or {})
    template: dict[str, Any] = {
        "session_id": summary.get("session_id"),
        "stage": stage,
        "gate_id": gate.gate_id,
        "decision": decision,
        "notes": ["replace this note with the operator rationale"],
        "metadata": {"template_guidance": guidance},
        "decided_by": "human",
        "rationale_artifact_ref": None,
    }
    if requirements.get("candidate_ids"):
        candidate_ids = list(requirements.get("candidate_ids") or [])
        candidate_refs = dict(requirements.get("candidate_final_assembly_refs") or {})
        guidance["candidate_ids"] = candidate_ids
        guidance["candidate_final_assembly_refs"] = candidate_refs
        if "candidate_ids" in gate.required_resume_fields:
            template["candidate_ids"] = candidate_ids
        else:
            selected_candidate = candidate_ids[0] if candidate_ids else "<candidate_id>"
            template["candidate_id"] = selected_candidate
            if "final_assembly_ref" in gate.required_resume_fields or candidate_refs:
                template["final_assembly_ref"] = candidate_refs.get(selected_candidate, "<final_assembly_ref>")
            template["rejected_candidates"] = []
    if requirements.get("candidate_methods"):
        candidate_methods = list(requirements.get("candidate_methods") or [])
        guidance["candidate_methods"] = candidate_methods
        template["method_name"] = candidate_methods[0] if candidate_methods else "<method_name>"
    return template


def _preferred_template_decision(gate, decisions_allowed: list[str]) -> str:
    if gate.template_preferred_decision in decisions_allowed:
        return str(gate.template_preferred_decision)
    return decisions_allowed[0] if decisions_allowed else (gate.template_preferred_decision or gate.decisions_allowed[0])


def _review_gate_next_action(summary: dict[str, Any], *, data_dir: str | Path) -> dict[str, Any]:
    session_id = summary.get("session_id")
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage", "review")
    abs_data_dir = str(Path(data_dir).expanduser().resolve(strict=False))
    template_hint = str(Path(abs_data_dir) / "decisions" / f"{session_id}_{stage}_decision.json")
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
        resume_summary = "Durable resume is unavailable until the session has a sqlite checkpoint database."
    else:
        resume_summary = f"Human review is required for {stage}; write and edit a decision file, then resume."
    fields = ["session_id", "stage", "decision", "decided_by"]
    gate = find_review_gate(stage, requirements.get("gate_id"))
    if gate is not None:
        fields.extend(field for field in gate.required_resume_fields if field not in fields)
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
            requirements.get("report_refs"),
            _candidate_comparison_relevant_refs(requirements.get("candidate_comparison_context")),
        ),
    }


def _env_block_next_action(event: dict[str, Any]) -> dict[str, Any]:
    status = event.get("status")
    kind = "missing_tool" if status == "missing_tool" else "blocked_env"
    missing_tools = list(event.get("missing_tools") or [])
    env_target = event.get("env_target")
    detail = event.get("detail") or "environment preflight failed"
    if kind == "missing_tool":
        summary = f"Install or expose required tool(s) in {env_target}: {', '.join(missing_tools)}."
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
        summary = f"Provide or correct the missing input path, then rerun with the original manifest: {blocked_ref}."
    else:
        summary = f"Provide or correct the missing input referenced by the manifest: {detail}."
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
    payload = summary.get("latest_operator_report_payload") or {}
    last_error = str(payload.get("last_error") or summary.get("last_error") or "unknown failure")
    stage = payload.get("current_stage") or summary.get("current_stage")
    missing_input = _report_missing_input(payload)
    if missing_input:
        return _missing_input_next_action(missing_input)
    if _is_function_protein_artifact_error(last_error):
        return {
            "kind": "materialized_proteins_required",
            "summary": "Function annotation requires a materialized local predicted-proteins FASTA matching database_provenance input_proteins_ref.",
            "stage": stage or "function_annotation",
            "last_error": last_error,
            "required_fields": [
                "manifest.assets[predicted_proteins].paths[0]",
                "stage_outputs.gene_annotation.proteins_fasta_ref",
                "function_annotation.database_provenance[*].input_proteins_ref",
            ],
            "manifest_asset_type": "predicted_proteins",
            "run_config_path": "function_annotation.database_provenance[*].input_proteins_ref",
            "relevant_refs": _artifact_refs(payload.get("relevant_refs")),
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
            "summary": "Function annotation requires database_provenance records matching a materialized proteins_fasta_ref.",
            "stage": stage or "function_annotation",
            "last_error": last_error,
            "required_fields": required_fields,
            "run_config_path": "function_annotation.database_provenance",
            "relevant_refs": _artifact_refs(payload.get("relevant_refs")),
        }
    return {
        "kind": "failed_terminal",
        "summary": last_error,
        "stage": stage,
        "last_error": last_error,
        "required_fields": [],
        "relevant_refs": _artifact_refs(payload.get("relevant_refs")),
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
        key: context[key]
        for key in ("candidate_comparison_ref", "assembly_candidate_comparison_report_ref")
        if context.get(key)
    }


def _artifact_refs(*values: Any) -> list[str]:
    refs: list[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, list):
            refs.extend(_artifact_refs(*value))
        elif isinstance(value, dict):
            file_path = value.get("file_path")
            if file_path:
                refs.append(str(file_path))
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
    gate_id = payload.get("gate_id")
    gate = find_review_gate(stage, gate_id)
    decisions_allowed = list(payload.get("decisions_allowed") or [])
    if not decisions_allowed and gate is not None:
        decisions_allowed = list(gate.decisions_allowed)
    requirements: dict[str, Any] = {
        "stage": stage,
        "gate_id": gate_id or (gate.gate_id if gate else None),
        "decisions_allowed": decisions_allowed,
        "requires_structured_resume": True,
    }
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        requirements["candidate_ids"] = [
            c.get("candidate_id")
            for c in candidates
            if isinstance(c, dict) and c.get("candidate_id") is not None
        ]
        requirements["candidate_final_assembly_refs"] = _candidate_final_assembly_refs(candidates)
    candidate_methods = _candidate_methods(payload)
    if candidate_methods:
        requirements["candidate_methods"] = candidate_methods
    report_refs = payload.get("report_refs")
    if isinstance(report_refs, dict):
        requirements["report_refs"] = dict(report_refs)
    comparison_context = payload.get("candidate_comparison_context")
    if isinstance(comparison_context, dict):
        requirements["candidate_comparison_context"] = dict(comparison_context)
    for key in ("auto_decidable", "pending_human_confirmation", "review_request_artifact_ref"):
        if key in payload:
            requirements[key] = payload.get(key)
    policy = payload.get("review_policy")
    if isinstance(policy, dict) and policy.get("mode"):
        requirements["review_policy.mode"] = policy.get("mode")
    return requirements


def _review_decisions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return decision_records(events)


def _decisions_by_origin(decisions: list[dict[str, Any]]) -> dict[str, int]:
    return counts_by_origin(decisions)
