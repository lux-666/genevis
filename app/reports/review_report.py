"""Final review decision report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records, read_artifact_records
from app.review.records import counts_by_origin, decision_records
from app.schemas.artifacts import ArtifactMeta


def write_operator_report(data_dir: str | Path, session_id: str, result: dict[str, Any]) -> str | None:
    supervision = result.get("supervision_state")
    if supervision not in {"blocked_human", "failed_terminal"}:
        return None

    from app.control.lifecycle import build_result_next_action

    ledger = Ledger(data_dir)
    ledger.create_session(session_id, str(result.get("input_manifest_ref") or ""))

    report = {
        "session_id": session_id,
        "current_stage": result.get("current_stage"),
        "supervision_state": supervision,
        "last_error": result.get("last_error"),
        "active_interrupt": _compact_active_interrupt(result.get("active_interrupt")),
        "next_action": build_result_next_action({**result, "session_id": session_id, "data_dir": str(data_dir)}),
        "relevant_refs": _selected_result_refs(result),
        "route_trace_tail": list(result.get("route_trace") or [])[-5:],
    }
    report_kind = "blocked_human" if supervision == "blocked_human" else "failed_terminal"
    artifact_type = f"operator_report.{report_kind}"
    out_dir = Path(data_dir) / "artifacts" / "operator_report" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    report_ref = _operator_report_path(data_dir, session_id, report_kind, result.get("current_stage"), report_text)
    if _path_has_same_content(report_ref, report_text) and _artifact_record_exists(data_dir, session_id, artifact_type, report_ref):
        return str(report_ref)
    report_ref.write_text(report_text, encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        report_ref,
        stage="operator_report",
        method_name=artifact_type,
        artifact_type=artifact_type,
        metadata={"supervision_state": supervision, "current_stage": result.get("current_stage")},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    ledger.record_artifact_registered(session_id, artifact.model_dump(mode="json"))
    return str(report_ref)


def write_final_review_report(data_dir: str | Path, session_id: str) -> str:
    events = Ledger(data_dir).read_events(session_id)
    artifacts = read_artifact_records(data_dir, session_id)
    decisions = decision_records(events)
    pending = _pending_confirmations(events, decisions)
    report = {
        "session_id": session_id,
        "pending_human_confirmations": pending,
        "decisions_by_origin": counts_by_origin(decisions),
        "decisions": decisions,
        "artifact_count": len(artifacts),
    }
    out_dir = Path(data_dir) / "artifacts" / "final_report" / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    report_ref = out_dir / "review_decision_report.json"
    report_ref.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        report_ref,
        stage="final_report",
        method_name="final_report.review_decisions",
        artifact_type="final_report.review_decisions",
        metadata={"pending_human_confirmations": len(pending)},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return str(report_ref)


def _operator_report_path(
    data_dir: str | Path,
    session_id: str,
    report_kind: str,
    current_stage: Any,
    report_text: str,
) -> Path:
    out_dir = Path(data_dir) / "artifacts" / "operator_report" / session_id
    base = out_dir / f"{report_kind}_report.json"
    if _path_has_same_content(base, report_text):
        return base
    if not base.exists():
        return base

    stage = _safe_report_segment(str(current_stage or "unknown"))
    staged = out_dir / f"{report_kind}_{stage}_report.json"
    if _path_has_same_content(staged, report_text) or not staged.exists():
        return staged

    index = 2
    while True:
        candidate = out_dir / f"{report_kind}_{stage}_{index}_report.json"
        if _path_has_same_content(candidate, report_text) or not candidate.exists():
            return candidate
        index += 1


def _artifact_record_exists(data_dir: str | Path, session_id: str, artifact_type: str, report_ref: Path) -> bool:
    for record in read_artifact_records(data_dir, session_id):
        if record.get("artifact_type") == artifact_type and Path(record.get("file_path", "")) == report_ref:
            return True
    return False


def _path_has_same_content(path: Path, expected: str) -> bool:
    if not path.exists():
        return False
    try:
        return path.read_text(encoding="utf-8") == expected
    except OSError:
        return False


def _safe_report_segment(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value).strip("_") or "unknown"


def _compact_active_interrupt(active_interrupt: Any) -> dict[str, Any] | None:
    if not isinstance(active_interrupt, dict):
        return None
    compact: dict[str, Any] = {}
    for key in (
        "stage",
        "reason",
        "decisions_allowed",
        "candidate_methods",
        "review_request_artifact_ref",
        "auto_decidable",
        "pending_human_confirmation",
        "blocked_at",
        "report_refs",
        "candidate_comparison_context",
    ):
        if key in active_interrupt:
            compact[key] = active_interrupt[key]
    candidates = active_interrupt.get("candidates")
    if isinstance(candidates, list):
        compact["candidate_ids"] = [
            candidate.get("candidate_id")
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("candidate_id")
        ]
    return compact


def _selected_result_refs(result: dict[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    for key in ("asset_registry_ref", "stage_plan_ref"):
        if result.get(key):
            refs[key] = result[key]
    stage_outputs = result.get("stage_outputs") or {}
    if not isinstance(stage_outputs, dict):
        return refs
    for stage, output in stage_outputs.items():
        if not isinstance(output, dict):
            continue
        stage_refs = {
            key: value
            for key, value in output.items()
            if (key.endswith("_ref") or key.endswith("_artifact_ref")) and isinstance(value, str)
        }
        if output.get("env_target"):
            stage_refs["env_target"] = output["env_target"]
        if output.get("missing_tools"):
            stage_refs["missing_tools"] = output["missing_tools"]
        if stage_refs:
            refs[stage] = stage_refs
    return refs


def _pending_confirmations(events: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # KNOWN LIMITATION: resolution is tracked at stage-level granularity. If a
    # stage emits multiple review_requested events (e.g. retry/rerun), a single
    # decision will mark all of them resolved. This is acceptable today because
    # no committed code path emits more than one review_requested per stage per
    # session. If multi-round review lands, switch to per-request binding via
    # `review_request_id` (or the request artifact ref).
    decided_stages = {decision.get("stage") for decision in decisions}
    pending = []
    for event in events:
        if event.get("event_type") != "review_requested":
            continue
        if event.get("pending_human_confirmation") or event.get("auto_decidable") is False:
            pending.append(
                {
                    "timestamp": event.get("timestamp"),
                    "stage": event.get("stage"),
                    "reason": event.get("reason"),
                    "review_request_artifact_ref": event.get("review_request_artifact_ref"),
                    "auto_decidable": event.get("auto_decidable"),
                    "policy_mode": event.get("policy_mode"),
                    "resolved": event.get("stage") in decided_stages,
                }
            )
    return pending
