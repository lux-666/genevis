"""Lifecycle helpers for inspecting and resuming human review gates."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from app.control.errors import ResumeValidationError
from app.control.inspection import (
    _blocked_timeout_status,
    _validate_active_interrupt_workflow_step,
)
from app.domains.assembly.selection import (
    route_from_read_type,
    validate_assembly_selection,
)
from app.review.application import (
    ReviewApplicationError,
    canonicalize_review_intent_request,
)
from app.review.approval import ensure_decision_allowed, load_verified_interrupt_request
from app.review.base import ResumeReviewRequest
from app.review.gates import (
    ReviewGateDeclaration,
    find_review_gate,
    get_review_gate,
    registered_review_stage_label,
)
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import (
    ActiveInterrupt,
    TopLevelState,
)


def normalize_resume_review_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    *,
    blocked_timeout_seconds: float | None = None,
    decision_source: str = "manual",
    now: datetime | None = None,
) -> ResumeReviewRequest:
    """Validate one proposal and return the canonical request to commit.

    This boundary performs no artifact, Ledger, or checkpoint writes. Automatic
    timeout rules may only normalize the request; durable preparation is owned
    by the review-application coordinator.
    """

    active_interrupt, request_record = _validated_resume_context(state, request)
    active_gate_id = active_interrupt.get("gate_id")
    canonical_request = (
        request.model_copy(update={"gate_id": active_gate_id})
        if request.gate_id is None and isinstance(active_gate_id, str)
        else request
    )
    ensure_decision_allowed(request_record, canonical_request)
    if blocked_timeout_seconds is not None:
        if decision_source != "foreground_countdown":
            raise ResumeValidationError(
                "foreground review decisions require foreground_countdown "
                "decision source"
            )
        if isinstance(blocked_timeout_seconds, bool) or not isinstance(
            blocked_timeout_seconds, (int, float)
        ):
            raise ResumeValidationError(
                "foreground review decisions require a numeric countdown timeout"
            )
        configured_seconds = _review_request_max_blocked_seconds(request_record)
        if blocked_timeout_seconds != configured_seconds:
            raise ResumeValidationError(
                "foreground countdown timeout does not match the verified "
                "review request"
            )
        if canonical_request.decided_by == "human":
            raise ResumeValidationError(
                "foreground review countdown requires an automatic decision origin"
            )
        _validate_foreground_automatic_request(
            active_interrupt,
            canonical_request,
            configured_seconds=configured_seconds,
        )
    elif canonical_request.decided_by == "auto_rule":
        raise ResumeValidationError(
            "auto_rule decisions are only valid in the foreground countdown path"
        )

    final_request = canonical_request
    final_record = request_record
    if blocked_timeout_seconds is not None:
        timeout = _blocked_timeout_status(
            active_interrupt, blocked_timeout_seconds, now=now
        )
        if not timeout or not timeout.get("expired"):
            raise ResumeValidationError("foreground review countdown has not expired")
        if canonical_request.decided_by == "auto_rule":
            try:
                gate = get_review_gate(
                    canonical_request.stage,
                    canonical_request.gate_id,
                )
            except KeyError as exc:
                raise ResumeValidationError(
                    f"Unsupported timeout stage: {canonical_request.stage}"
                ) from exc
            normalizer = gate.load_timeout_request_normalizer()
            if normalizer is None:
                raise ResumeValidationError(
                    f"review gate {canonical_request.stage}/{gate.gate_id} has no "
                    "automatic timeout resolver"
                )
            normalized = normalizer(state, canonical_request, timeout)
            _active, normalized_record = _validated_resume_context(state, normalized)
            ensure_decision_allowed(normalized_record, normalized)
            final_request = normalized
            final_record = normalized_record
    try:
        return canonicalize_review_intent_request(
            state,
            final_request,
            request_record=final_record,
        )
    except ReviewApplicationError as exc:
        raise ResumeValidationError(str(exc)) from exc


def _validated_resume_context(
    state: TopLevelState,
    request: ResumeReviewRequest,
) -> tuple[ActiveInterrupt, dict[str, Any]]:
    if state.get("session_id") != request.session_id:
        raise ResumeValidationError("resume session_id does not match blocked state")
    if state.get("supervision_state") != "blocked_human":
        raise ResumeValidationError("resume requires supervision_state blocked_human")
    active_interrupt = state.get("active_interrupt")
    if not active_interrupt:
        raise ResumeValidationError("resume requires an active_interrupt")
    if active_interrupt.get("stage") != request.stage:
        raise ResumeValidationError(
            "resume stage does not match active_interrupt.stage"
        )
    _validate_active_interrupt_workflow_step(state, active_interrupt)
    active_gate_id = active_interrupt.get("gate_id")
    if (
        active_gate_id == "parameter_approval"
        and request.gate_id != "parameter_approval"
    ):
        raise ResumeValidationError(
            "parameter approval resume requires explicit gate_id=parameter_approval"
        )
    if request.gate_id and active_gate_id and request.gate_id != active_gate_id:
        raise ResumeValidationError(
            "resume gate_id does not match active_interrupt.gate_id"
        )
    interrupt_request_id = active_interrupt.get("review_request_id")
    interrupt_request_hash = active_interrupt.get("request_hash")
    interrupt_request_ref = active_interrupt.get("request_ref")
    missing = [
        key
        for key, value in (
            ("review_request_id", interrupt_request_id),
            ("request_hash", interrupt_request_hash),
            ("request_ref", interrupt_request_ref),
        )
        if not value
    ]
    if missing:
        raise ResumeValidationError(
            "v2 resume requires compact request identity fields: " + ", ".join(missing)
        )
    if request.review_request_id is None or request.review_request_hash is None:
        raise ResumeValidationError(
            "v2 resume decision requires review_request_id and review_request_hash"
        )
    if request.review_request_id != interrupt_request_id:
        raise ResumeValidationError(
            "resume review_request_id does not match active_interrupt.review_request_id"
        )
    if request.review_request_hash != interrupt_request_hash:
        raise ResumeValidationError(
            "resume review_request_hash does not match active_interrupt.request_hash"
        )
    _request_ref, request_record = load_verified_interrupt_request(
        state,
        active_interrupt,
    )
    return active_interrupt, request_record


def _review_request_max_blocked_seconds(request_record: Mapping[str, Any]) -> int:
    payload = request_record.get("payload")
    value = payload.get("max_blocked_seconds") if isinstance(payload, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value < -1:
        raise ResumeValidationError(
            "verified review request max_blocked_seconds must be an integer >= -1"
        )
    return value


def _validate_foreground_automatic_request(
    active_interrupt: Mapping[str, Any],
    request: ResumeReviewRequest,
    *,
    configured_seconds: int,
) -> None:
    request_ref = active_interrupt.get("request_ref")
    try:
        expected_rationale = ArtifactRef.model_validate(request_ref)
    except (TypeError, ValueError):
        expected_rationale = None
    metadata = request.metadata
    elapsed_seconds = metadata.get("elapsed_seconds")
    if (
        metadata.get("source") != "foreground_countdown"
        or metadata.get("configured_seconds") != configured_seconds
        or not isinstance(elapsed_seconds, (int, float))
        or isinstance(elapsed_seconds, bool)
        or elapsed_seconds < configured_seconds
    ):
        raise ResumeValidationError(
            "automatic decision is not bound to the verified foreground countdown"
        )
    if (
        request.decided_by == "auto_rule"
        and request.rationale_artifact_ref != expected_rationale
    ):
        raise ResumeValidationError(
            "auto_rule decision is not bound to the verified review request"
        )


def build_resume_decision_template(summary: dict[str, Any]) -> dict[str, Any]:
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    gate_id = requirements.get("gate_id")
    if not isinstance(stage, str):
        raise ResumeValidationError(
            "no active registered review decision requirements; registered stages: "
            f"{registered_review_stage_label()}"
        )
    try:
        gate = get_review_gate(stage, gate_id)
    except KeyError as exc:
        raise ResumeValidationError(
            "no active registered review decision requirements; registered stages: "
            f"{registered_review_stage_label()}"
        ) from exc

    decisions_allowed = list(requirements.get("decisions_allowed") or [])
    decision = _preferred_template_decision(gate, decisions_allowed)
    recommended_route = requirements.get("recommended_route")
    recommended_route_decision = (
        f"select_{recommended_route}" if isinstance(recommended_route, str) else None
    )
    if (
        gate.normalize_timeout_request
        and recommended_route_decision in decisions_allowed
    ):
        decision = str(recommended_route_decision)
    template: dict[str, Any] = {
        "session_id": summary.get("session_id"),
        "stage": stage,
        "gate_id": gate.gate_id,
        "review_request_id": requirements.get("review_request_id"),
        "review_request_hash": requirements.get("request_hash"),
        "decision": decision,
        "notes": ["replace this note with the operator rationale"],
        "metadata": dict(requirements.get("resume_metadata") or {}),
        "decided_by": "human",
        "rationale_artifact_ref": None,
    }
    required_fields = set(gate.required_resume_fields_for(decision))
    if requirements.get("candidate_ids"):
        candidate_ids = list(requirements.get("candidate_ids") or [])
        if "candidate_ids" in required_fields:
            template["candidate_ids"] = candidate_ids
        else:
            selected_candidate = candidate_ids[0] if candidate_ids else "<candidate_id>"
            template["candidate_id"] = selected_candidate
            template["rejected_candidates"] = []
    if requirements.get("candidate_methods"):
        candidate_methods = list(requirements.get("candidate_methods") or [])
        template["method_name"] = (
            candidate_methods[0] if candidate_methods else "<method_name>"
        )
    recommended_selection = requirements.get("recommended_assembly_selection")
    if gate.normalize_timeout_request and isinstance(recommended_selection, dict):
        recommended_method = recommended_selection.get("primary_method")
        if recommended_method:
            template["method_name"] = str(recommended_method)
    return template


def review_max_blocked_seconds(summary: dict[str, Any]) -> int:
    """Return the verified per-request foreground review wait."""

    requirements = summary.get("decision_requirements") or {}
    value = requirements.get("max_blocked_seconds")
    if not isinstance(value, int) or isinstance(value, bool) or value < -1:
        raise ResumeValidationError(
            "verified review request max_blocked_seconds must be an integer >= -1"
        )
    return value


def foreground_automatic_decision_label(
    summary: dict[str, Any],
) -> str | None:
    """Describe the gate's existing automatic resolver, if one is eligible."""

    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    gate_id = requirements.get("gate_id")
    if not isinstance(stage, str):
        return None
    gate = find_review_gate(stage, gate_id if isinstance(gate_id, str) else None)
    if gate is None:
        return None
    if gate.normalize_timeout_request:
        return _foreground_rule_decision(gate, requirements)
    if gate.prompt_bundle and requirements.get("auto_decidable") is True:
        return "LLM review recommendation"
    return None


def _foreground_rule_decision(
    gate: ReviewGateDeclaration, requirements: Mapping[str, Any]
) -> str | None:
    """A normalizer alone does not prove this request has an automatic action."""
    decision = gate.template_preferred_decision
    if gate.key == ("assembly", "route_selection"):
        try:
            selection = validate_assembly_selection(
                requirements.get("recommended_assembly_selection")
            )
        except (TypeError, ValueError):
            return None
        route = route_from_read_type(selection.read_type)
        if requirements.get("recommended_route") != route:
            return None
        decision = f"select_{route}"
    return (
        decision if decision in (requirements.get("decisions_allowed") or []) else None
    )


def build_foreground_auto_resume_request(
    summary: dict[str, Any],
    *,
    configured_seconds: int,
    elapsed_seconds: float,
    data_dir: str | Path | None = None,
) -> ResumeReviewRequest:
    """Run the gate's existing automatic resolver after countdown expiry."""

    if configured_seconds < 0:
        raise ResumeValidationError(
            "an indefinite review wait cannot build an automatic decision"
        )
    requirements = summary.get("decision_requirements") or {}
    stage = requirements.get("stage")
    gate_id = requirements.get("gate_id")
    if not isinstance(stage, str):
        raise ResumeValidationError("foreground review gate is not registered")
    try:
        gate = get_review_gate(
            stage,
            gate_id if isinstance(gate_id, str) else None,
        )
    except KeyError as exc:
        raise ResumeValidationError("foreground review gate is not registered") from exc

    countdown_metadata = {
        "source": "foreground_countdown",
        "configured_seconds": configured_seconds,
        "elapsed_seconds": elapsed_seconds,
    }
    if gate.prompt_bundle and requirements.get("auto_decidable") is True:
        if data_dir is None:
            raise ResumeValidationError(
                "LLM review recommendation requires the frozen session data directory"
            )
        review_request_id = requirements.get("review_request_id")
        request_ref = requirements.get("request_ref")
        request_hash = requirements.get("request_hash")
        if (
            not isinstance(review_request_id, str)
            or not review_request_id
            or not isinstance(request_ref, Mapping)
            or not isinstance(request_hash, str)
            or not request_hash
        ):
            raise ResumeValidationError(
                "LLM review recommendation requires a verified immutable request"
            )
        from app.review.recommender import (
            RecommendationError,
            recommend_review_decision,
        )

        try:
            recommendation = recommend_review_decision(
                session_id=str(summary.get("session_id") or ""),
                stage=stage,
                review_request_id=review_request_id,
                request_ref=request_ref,
                request_hash=request_hash,
                data_dir=data_dir,
            )
        except (RecommendationError, ValueError) as exc:
            raise ResumeValidationError(
                f"automatic review recommendation failed: {exc}"
            ) from exc
        return recommendation.model_copy(
            update={
                "notes": [
                    *recommendation.notes,
                    "foreground review countdown expired",
                ],
                "metadata": {
                    **recommendation.metadata,
                    **countdown_metadata,
                },
            }
        )

    if (
        not gate.normalize_timeout_request
        or _foreground_rule_decision(gate, requirements) is None
    ):
        raise ResumeValidationError(
            f"review gate {stage}/{gate.gate_id} has no eligible automatic resolver"
        )

    template = build_resume_decision_template(summary)
    template_decision = str(template.get("decision") or "")
    for field_name in gate.required_resume_fields_for(template_decision):
        field_value = template.get(field_name)
        if not field_value or (
            isinstance(field_value, str)
            and field_value.startswith("<")
            and field_value.endswith(">")
        ):
            raise ResumeValidationError(
                "foreground automatic resolver request is missing required field "
                f"{field_name!r}"
            )
    rationale_ref = requirements.get("request_ref")
    try:
        rationale_artifact_ref = ArtifactRef.model_validate(rationale_ref)
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "foreground automatic resolver requires the verified review request "
            "artifact"
        ) from exc
    payload = {
        **template,
        "notes": ["accepted by the foreground review countdown"],
        "metadata": {
            **dict(template.get("metadata") or {}),
            **countdown_metadata,
        },
        "decided_by": "auto_rule",
        "rationale_artifact_ref": rationale_artifact_ref.model_dump(mode="json"),
    }
    try:
        return ResumeReviewRequest.model_validate(payload)
    except Exception as exc:
        raise ResumeValidationError(
            f"foreground automatic resolver request failed validation: {exc}"
        ) from exc


def _preferred_template_decision(
    gate: ReviewGateDeclaration,
    decisions_allowed: list[str],
) -> str:
    if gate.template_preferred_decision in decisions_allowed:
        return str(gate.template_preferred_decision)
    return (
        decisions_allowed[0]
        if decisions_allowed
        else (gate.template_preferred_decision or gate.decisions_allowed[0])
    )
