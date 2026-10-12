"""Lifecycle helpers for inspecting and resuming human review gates."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.control.errors import ResumeValidationError
from app.control.inspection import _decision_requirements_from_state
from app.control.resume import _preferred_template_decision
from app.review.gates import (
    find_review_gate,
)


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

    review_status = summary.get("latest_review_status")
    if (
        summary.get("supervision_state") == "blocked_human"
        and isinstance(review_status, Mapping)
        and review_status.get("integrity_error")
    ):
        return {
            "kind": "review_integrity_error",
            "summary": str(review_status["integrity_error"]),
            "stage": review_status.get("stage"),
            "review_status": review_status.get("status"),
            "required_fields": [],
            "relevant_refs": _artifact_refs(
                review_status.get("intent_ref"),
                review_status.get("application_ref"),
                summary.get("latest_review_request_artifact"),
            ),
        }
    if (
        summary.get("supervision_state") == "blocked_human"
        and isinstance(review_status, Mapping)
        and review_status.get("requires_recovery") is True
    ):
        abs_data_dir = str(Path(data_dir).expanduser().resolve(strict=False))
        session_id = summary.get("session_id")
        return {
            "kind": "review_recovery_required",
            "summary": (
                "A review decision is already committed; recover that exact "
                "intent instead of submitting a different decision."
            ),
            "stage": review_status.get("stage"),
            "review_status": review_status.get("status"),
            "required_fields": [],
            "command": (
                "gene-agent resume "
                f"--session-id {session_id} --data-dir {abs_data_dir} "
                "--checkpointer sqlite"
            ),
            "relevant_refs": _artifact_refs(
                review_status.get("intent_ref"),
                review_status.get("application_ref"),
            ),
        }

    requirements = summary.get("decision_requirements")
    if requirements:
        return _review_gate_next_action(summary, data_dir=data_dir)

    if summary.get("supervision_state") == "completed":
        refs = _artifact_refs(
            summary.get("latest_final_report"),
        )
        return {
            "kind": "completed",
            "summary": ("Workflow completed; inspect the listed final report."),
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
        decisions_allowed = list(requirements.get("decisions_allowed") or [])
        preferred = _preferred_template_decision(gate, decisions_allowed)
        fields.extend(
            field
            for field in gate.required_resume_fields_for(preferred)
            if field not in fields
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
