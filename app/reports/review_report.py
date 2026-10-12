"""Best-effort operator-status and session-summary report entry points."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.reports._projection import atomic_write_text, source_watermark
from app.schemas.limits import ROUTE_TRACE_LIMIT
from app.storage.ledger import Ledger
from app.storage.paths import DataLayoutError, SessionPaths


def write_operator_report(
    data_dir: str | Path,
    session_id: str,
    result: dict[str, Any],
) -> str | None:
    """Overwrite ``reports/current_status.json`` from the latest live result.

    This function has no ledger or artifact-index side effects.  It is a read
    model writer, not a lifecycle transition.
    """
    supervision = result.get("supervision_state")
    if supervision not in {"blocked_human", "failed_terminal"}:
        return None

    from app.control.next_action import build_result_next_action

    paths = SessionPaths.from_run_dir(data_dir)
    paths.require_session_id(session_id)
    ledger = Ledger(paths.run_dir)
    if ledger.read_session(session_id) is None:
        return None

    report = {
        "schema_version": 2,
        "report_type": "current_status",
        "session_id": session_id,
        "source_watermark": source_watermark(paths, ledger),
        "current_stage": result.get("current_stage"),
        "supervision_state": supervision,
        "last_error": _bounded_text(result.get("last_error"), 4096),
        "active_interrupt": _compact_active_interrupt(
            result.get("active_interrupt"), paths
        ),
        "next_action": build_result_next_action(
            {**result, "session_id": session_id, "data_dir": str(paths.run_dir)}
        ),
        "relevant_refs": _selected_result_refs(result, paths),
        "route_trace_tail": [
            str(item)
            for item in list(result.get("route_trace") or [])[-ROUTE_TRACE_LIMIT:]
        ],
    }
    report_ref = paths.report_path("current_status.json")
    atomic_write_text(
        report_ref,
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
    )
    return str(report_ref)


def write_final_review_report(data_dir: str | Path, session_id: str) -> str:
    """Generate the unified session summary and return its JSON path."""
    from app.reports.trace_writer import write_session_trace

    result = write_session_trace(data_dir, session_id)
    report_ref = result.get("json_ref")
    if not report_ref:
        raise ValueError(
            f"cannot write a session summary for unknown session {session_id!r}"
        )
    return str(report_ref)


def _compact_active_interrupt(
    active_interrupt: Any,
    paths: SessionPaths,
) -> dict[str, Any] | None:
    if not isinstance(active_interrupt, dict):
        return None
    compact: dict[str, Any] = {}
    for key in (
        "stage",
        "gate_id",
        "reason",
        "decisions_allowed",
        "blocked_at",
        "review_request_id",
        "request_hash",
    ):
        if key in active_interrupt:
            compact[key] = _bounded_value(active_interrupt[key])

    request_ref = _normalize_ref(
        paths, active_interrupt.get("review_request_artifact_ref")
    )
    if request_ref:
        compact["review_request_artifact_ref"] = request_ref
    compact_request_ref = _normalize_ref(paths, active_interrupt.get("request_ref"))
    if compact_request_ref:
        compact["request_ref"] = compact_request_ref

    candidate_methods = active_interrupt.get("candidate_methods")
    if isinstance(candidate_methods, list):
        compact["candidate_methods"] = [
            str(item)[:256] for item in candidate_methods[:32]
        ]
    candidates = active_interrupt.get("candidates")
    if isinstance(candidates, list):
        compact["candidate_ids"] = [
            str(candidate.get("candidate_id"))[:256]
            for candidate in candidates[:32]
            if isinstance(candidate, dict) and candidate.get("candidate_id")
        ]

    report_refs = _normalize_ref_mapping(paths, active_interrupt.get("report_refs"))
    if report_refs:
        compact["report_refs"] = report_refs
    comparison = active_interrupt.get("candidate_comparison_context")
    if isinstance(comparison, dict):
        candidate_ids = comparison.get("candidate_ids")
        compact["candidate_comparison_context"] = {
            "candidate_ids": [str(item)[:256] for item in candidate_ids[:32]]
            if isinstance(candidate_ids, list)
            else [],
        }
    return compact


def _selected_result_refs(
    result: dict[str, Any],
    paths: SessionPaths,
) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("asset_registry_ref", "stage_plan_ref"):
        normalized = _normalize_ref(paths, result.get(key))
        if normalized:
            refs[key] = normalized
    stage_outputs = result.get("stage_outputs") or {}
    if not isinstance(stage_outputs, dict):
        return refs
    for stage, output in stage_outputs.items():
        if not isinstance(output, dict):
            continue
        stage_refs: dict[str, Any] = {}
        for key, value in output.items():
            if not (key.endswith("_ref") or key.endswith("_artifact_ref")):
                continue
            normalized = _normalize_ref(paths, value)
            if normalized:
                stage_refs[key] = normalized
        if output.get("env_target"):
            stage_refs["env_target"] = _bounded_text(output["env_target"], 512)
        if isinstance(output.get("missing_tools"), list):
            stage_refs["missing_tools"] = [
                str(item)[:256] for item in output["missing_tools"][:32]
            ]
        if stage_refs:
            refs[str(stage)] = stage_refs
    return refs


def _pending_review_requests(
    review_occurrences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project request resolution only from the shared review-status records."""

    return [
        {
            key: occurrence.get(key)
            for key in (
                "workflow_step_id",
                "review_request_id",
                "request_hash",
                "timestamp",
                "stage",
                "gate_id",
                "reason",
                "review_request_artifact_ref",
                "status",
                "resolved",
                "resolution_basis",
                "integrity_error",
            )
            if occurrence.get(key) is not None
        }
        for occurrence in review_occurrences
    ]


def _normalize_ref(paths: SessionPaths, value: Any) -> str | None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        value = value.get("path_ref") or value.get("file_path")
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return None
    try:
        path = Path(value)
        if path.is_absolute():
            return paths.to_ref(path)
        return paths.to_ref(paths.resolve_ref(path))
    except DataLayoutError:
        return None


def _normalize_ref_mapping(paths: SessionPaths, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, candidate in list(value.items())[:32]:
        ref = _normalize_ref(paths, candidate)
        if ref:
            normalized[str(key)] = ref
    return normalized


def _bounded_value(value: Any) -> Any:
    if isinstance(value, list):
        return [str(item)[:256] for item in value[:32]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _bounded_text(value, 1024)


def _bounded_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
