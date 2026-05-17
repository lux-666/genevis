"""Final review decision report."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.control.ledger import Ledger
from app.execution.artifacts import append_artifact_records, read_artifact_records
from app.review.records import counts_by_origin, decision_records
from app.schemas.artifacts import ArtifactMeta


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
