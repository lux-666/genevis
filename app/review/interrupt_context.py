"""Stage-specific interrupt-context projections for lifecycle decision display.

Each context builder takes the active_interrupt mapping and returns a dict of
extra fields to include in the ``_decision_requirements`` response.  Builders
are registered on :class:`~app.review.gates.ReviewGateDeclaration` via the
``interrupt_context_builder`` field and called by ``lifecycle._decision_requirements``.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.schemas.artifacts import ArtifactRef


def assembly_review_interrupt_context(
    interrupt: Mapping[str, Any],
) -> dict[str, Any]:
    """Extra fields for assembly_review/assembly_review gates."""

    candidates = interrupt.get("candidates") or []
    signals = interrupt.get("signals") or []
    return {
        "candidate_ids": [
            candidate.get("candidate_id")
            for candidate in candidates
            if isinstance(candidate, dict)
        ],
        "candidate_primary_output_refs": _candidate_primary_output_refs(candidates),
        "candidate_report_refs": _candidate_report_refs(signals),
        "report_refs": dict(interrupt.get("report_refs") or {}),
        "candidate_comparison_context": dict(
            interrupt.get("candidate_comparison_context") or {}
        ),
    }


def hic_scaffolding_interrupt_context(
    interrupt: Mapping[str, Any],
) -> dict[str, Any]:
    """Extra fields for hic_scaffolding review gates."""

    method_candidates = dict(interrupt.get("method_candidates") or {})
    return {
        "candidate_methods": _candidate_methods(interrupt),
        "executed_candidate_methods": [
            name
            for name, candidate in method_candidates.items()
            if isinstance(candidate, dict) and candidate.get("status") == "executed"
        ],
        "method_candidates": method_candidates,
    }


def assembly_route_selection_interrupt_context(
    interrupt: Mapping[str, Any],
) -> dict[str, Any]:
    """Extra fields for assembly/route_selection gates."""

    return {
        "recommended_route": interrupt.get("recommended_route"),
        "recommended_assembly_selection": dict(
            interrupt.get("recommended_assembly_selection") or {}
        ),
        "recommendation_reasoning": interrupt.get("recommendation_reasoning"),
        "uncertainty_notes": list(interrupt.get("uncertainty_notes") or []),
        "available_routes": list(interrupt.get("available_routes") or []),
        "available_read_types": list(interrupt.get("available_read_types") or []),
        "compatible_methods": dict(interrupt.get("compatible_methods") or {}),
    }


# ---------------------------------------------------------------------------
# Shared field-extraction helpers
# ---------------------------------------------------------------------------


def _candidate_primary_output_refs(
    candidates: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(candidates, list):
        return {}
    refs: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        primary_output_ref = candidate.get("primary_output_ref")
        if candidate_id and primary_output_ref:
            refs[str(candidate_id)] = ArtifactRef.model_validate(
                primary_output_ref
            ).model_dump(mode="json")
    return refs


def _candidate_report_refs(signals: Any) -> dict[str, list[str]]:
    if not isinstance(signals, list):
        return {}
    refs: dict[str, list[str]] = {}
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        candidate_id = signal.get("candidate_id")
        if not candidate_id:
            continue
        metric_refs = signal.get("metric_refs") or []
        if not isinstance(metric_refs, list):
            continue
        bucket = refs.setdefault(str(candidate_id), [])
        for ref in metric_refs:
            if ref and str(ref) not in bucket:
                bucket.append(str(ref))
    return refs


def generic_interrupt_context(interrupt: Mapping[str, Any]) -> dict[str, Any]:
    """Default extra fields for gates without a stage-specific builder."""

    return {
        "candidate_ids": candidate_ids(interrupt),
        "candidate_methods": _candidate_methods(interrupt),
    }


def candidate_ids(interrupt: Mapping[str, Any]) -> list[str]:
    """Extract candidate IDs from an interrupt payload."""
    candidates = interrupt.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        str(c.get("candidate_id"))
        for c in candidates
        if isinstance(c, dict) and c.get("candidate_id") is not None
    ]


def _candidate_methods(interrupt: Mapping[str, Any]) -> list[str]:
    methods = interrupt.get("candidate_methods")
    if isinstance(methods, list):
        return [str(m) for m in methods]
    candidates = interrupt.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        str(c.get("method_name"))
        for c in candidates
        if isinstance(c, dict) and c.get("method_name") is not None
    ]
