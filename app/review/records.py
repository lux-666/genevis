"""Shared review decision record helpers."""

from __future__ import annotations

from typing import Any

KNOWN_ORIGINS = ("human", "llm_provisional", "auto_rule")


def decision_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project review_decision events into a stable record shape.

    Missing `decided_by` defaults to "human" because older events do not carry
    the field.
    """
    records: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "review_decision":
            continue
        records.append(
            {
                "timestamp": event.get("timestamp"),
                "stage": event.get("stage"),
                "gate_id": event.get("gate_id"),
                "decision": event.get("decision"),
                "candidate_id": event.get("candidate_id"),
                "method_name": event.get("method_name"),
                "decided_by": event.get("decided_by") or "human",
                "rationale_artifact_ref": event.get("rationale_artifact_ref"),
                "artifact_ref": event.get("artifact_ref"),
            }
        )
    return records


def counts_by_origin(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {origin: 0 for origin in KNOWN_ORIGINS}
    for decision in decisions:
        origin = str(decision.get("decided_by") or "human")
        counts[origin] = counts.get(origin, 0) + 1
    return counts
