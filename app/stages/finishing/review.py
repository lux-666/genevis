"""Request-bound human approvals and pure projections for finishing actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.control.errors import ResumeValidationError
from app.control.workflow_plan import current_workflow_step_id
from app.llm.finishing import FinishingRecommendation
from app.review.application import (
    PreparedReviewApplication,
    ReviewIntentV1,
    VerifiedReviewApplication,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request, persist_review_decision
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.finishing.advisor import (
    GATE,
    STAGES,
    history,
    load_json,
    loop,
    maximum_attempts,
    output,
    settings,
    source,
    update,
    validate_proposal,
)

EFFECT = "finishing.recommendation.v1"


class FinishingReviewEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["run", "accept", "keep_source", "stop"]
    selected_attempt: int | None
    parameters: dict[str, Any] | None
    decision_ref: ArtifactRef
    context: dict[str, Any]


def context(state: TopLevelState, stage: str) -> dict[str, Any]:
    f = loop(state, stage)
    return {
        k: f[k]
        for k in (
            "source_ref",
            "history_refs",
            "recommendation_ref",
            "quast_ref",
            "config_ref",
        )
    }


def validate_intent_request(
    state: TopLevelState, request: ResumeReviewRequest, record: Mapping[str, Any]
) -> ResumeReviewRequest:
    stage = request.stage
    if stage not in STAGES:
        raise ResumeValidationError("unsupported finishing stage")
    interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    ref = ArtifactRef.model_validate(interrupt.get("request_ref"))
    verified = load_review_request(
        state["data_dir"], ref, session_id=state["session_id"]
    )
    if (
        record != verified
        or request.session_id != state["session_id"]
        or request.gate_id != GATE
        or request.stage != interrupt.get("stage")
        or interrupt.get("gate_id") != GATE
        or request.review_request_id != interrupt.get("review_request_id")
        or request.review_request_hash != ref.sha256
        or interrupt.get("request_hash") != ref.sha256
        or record.get("workflow_step_id")
        != current_workflow_step_id(state, expected_stage=stage)
        or interrupt.get("workflow_step_id") != current_workflow_step_id(state)
        or request.decision not in record["payload"]["decisions_allowed"]
        or loop(state, stage).get("phase") != "blocked"
        or request.decided_by != "human"
    ):
        raise ResumeValidationError("finishing human review identity/decision mismatch")
    if record["payload"]["candidate_comparison_context"] != context(state, stage):
        raise ResumeValidationError("finishing review evidence changed")
    records = history(state, stage)
    if request.decision == "follow_recommendation":
        proposal = FinishingRecommendation.model_validate(
            load_json(state, loop(state, stage)["recommendation_ref"])["proposal"]
        )
        validate_proposal(stage, proposal, records, maximum_attempts(state, stage))
        if proposal.action == "human_review":
            raise ResumeValidationError("finishing requires human judgment")
    if request.decision == "accept_candidate":
        if request.candidate_id not in {
            f"attempt_{r['attempt']}" for r in records if r["materialized"]
        }:
            raise ResumeValidationError("select a completed finishing candidate")
    elif request.candidate_id:
        raise ResumeValidationError(
            "candidate override is only valid for accept_candidate"
        )
    if (
        request.candidate_ids
        or request.method_name
        or request.rejected_candidates
        or request.metadata
    ):
        raise ResumeValidationError(
            "finishing does not accept overrides or automatic decisions"
        )
    return request


def prepare_application(
    state: TopLevelState, intent: ReviewIntentV1
) -> PreparedReviewApplication:
    record = load_review_request(
        state["data_dir"], intent.request_ref, session_id=state["session_id"]
    )
    request = validate_intent_request(state, intent.request, record)
    if intent.workflow_step_id != current_workflow_step_id(
        state
    ) or intent.request_ref != ArtifactRef.model_validate(
        dict(state.get("active_interrupt") or {}).get("request_ref")
    ):
        raise ResumeValidationError("finishing intent occurrence/ref mismatch")
    action: Literal["run", "accept", "keep_source", "stop"] = "keep_source"
    parameters, selected = None, None
    if request.decision == "follow_recommendation":
        proposal = FinishingRecommendation.model_validate(
            load_json(state, loop(state, request.stage)["recommendation_ref"])[
                "proposal"
            ]
        )
        if proposal.action == "human_review":
            raise ResumeValidationError("no executable finishing recommendation")
        action = proposal.action
        parameters = (
            proposal.parameters.model_dump(mode="json") if proposal.parameters else None
        )
        selected = proposal.selected_attempt
    elif request.decision == "accept_candidate":
        action, selected = (
            "accept",
            int(str(request.candidate_id).removeprefix("attempt_")),
        )
    elif request.decision == "stop_human_review":
        action = "stop"
    decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=intent.review_request_id,
        stage=request.stage,
        gate_id=GATE,
        decision=request,
        request_hash=intent.request_hash,
        request_ref=intent.request_ref,
    )
    evidence_context = context(state, request.stage)
    effect = FinishingReviewEffect(
        action=action,
        selected_attempt=selected,
        parameters=parameters,
        decision_ref=decision_ref,
        context=evidence_context,
    )
    return PreparedReviewApplication(
        effect_kind=EFFECT,
        effect=effect,
        decision_ref=decision_ref,
        required_refs=(
            intent.request_ref,
            *[
                ArtifactRef.model_validate(evidence_context[k])
                for k in ("source_ref", "recommendation_ref", "quast_ref", "config_ref")
            ],
            *[ArtifactRef.model_validate(r) for r in evidence_context["history_refs"]],
        ),
    )


def apply_application(
    state: TopLevelState, application: VerifiedReviewApplication
) -> StateUpdate:
    effect, identity = application.effect, application.application
    stage = identity.stage
    interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    if (
        not isinstance(effect, FinishingReviewEffect)
        or stage not in STAGES
        or identity.effect_kind != EFFECT
        or identity.session_id != state["session_id"]
        or identity.workflow_step_id
        != current_workflow_step_id(state, expected_stage=stage)
        or identity.gate_id != GATE
        or identity.review_request_id != interrupt.get("review_request_id")
        or identity.request_hash != interrupt.get("request_hash")
        or identity.decision_ref != effect.decision_ref
        or effect.context != context(state, stage)
        or loop(state, stage).get("phase") != "blocked"
    ):
        raise ResumeValidationError("finishing application binding mismatch")
    f = loop(state, stage)
    records = history(state, stage)
    if effect.action == "run":
        proposal = FinishingRecommendation.model_validate(
            load_json(state, f["recommendation_ref"])["proposal"]
        )
        validate_proposal(stage, proposal, records, maximum_attempts(state, stage))
        if (
            proposal.action != "run"
            or proposal.parameters is None
            or proposal.parameters.model_dump(mode="json") != effect.parameters
        ):
            raise ResumeValidationError(
                "finishing approved parameters differ from proposal"
            )
        # Clear every old execution/product cursor before the next source-based attempt.
        changes = settings(state, stage)
        if stage != "t2t":
            changes.update(effect.parameters or {})
        if stage == "t2t":
            changes["status"] = "motif_resolved"
        else:
            changes["status"] = "ready"
        changes["execution_attempts"] = []
        changes["active_interrupt"] = None
        changes["finishing"] = {
            **f,
            "phase": "run",
            "attempt": len(records),
            "parameters": effect.parameters,
            "decision_ref": effect.decision_ref.model_dump(mode="json"),
        }
        # A replacement is needed: the normal helper merges, leaving stale products.
        cleared: TopLevelState = {
            **state,
            "stage_outputs": {**state["stage_outputs"], stage: {}},
        }
        result = update(cleared, stage, changes)
    else:
        changes = {
            "active_interrupt": None,
            "finishing": {
                **f,
                "phase": "failed" if effect.action == "stop" else "done",
                "decision_ref": effect.decision_ref.model_dump(mode="json"),
                "selected_attempt": effect.selected_attempt,
                "action": effect.action,
            },
        }
        if effect.action == "accept":
            selected = next(
                (
                    r
                    for r in records
                    if r["attempt"] == effect.selected_attempt and r["materialized"]
                ),
                None,
            )
            if selected is None:
                raise ResumeValidationError("finishing selected attempt unavailable")
            changes = {**selected["cursor"], **changes}
            changes["finishing"]["candidate_ref"] = selected["candidate_ref"]
        elif effect.action == "keep_source":
            changes["finishing"]["candidate_ref"] = source(state, stage).ref.model_dump(
                mode="json"
            )
        result = update(state, stage, changes)
    return {**result, "active_interrupt": None, "supervision_state": "running_stage"}


def approved_proposal(
    state: TopLevelState, stage: str, *, before_execution: bool = True
) -> FinishingRecommendation:
    f = loop(state, stage)
    proposal = FinishingRecommendation.model_validate(
        load_json(state, f["recommendation_ref"])["proposal"]
    )
    decision = load_json(state, f["decision_ref"])
    request = load_review_request(
        state["data_dir"],
        ArtifactRef.model_validate(decision["request_ref"]),
        session_id=state["session_id"],
    )
    approved_context = request["payload"]["candidate_comparison_context"]
    if (
        decision["decision"]["decision"] != "follow_recommendation"
        or decision["decision"]["decided_by"] != "human"
        or decision["gate_id"] != GATE
        or decision["stage"] != stage
        or decision["workflow_step_id"]
        != current_workflow_step_id(state, expected_stage=stage)
        or decision["request_hash"]
        != ArtifactRef.model_validate(decision["request_ref"]).sha256
        or proposal.action != "run"
        or proposal.parameters is None
        or approved_context["source_ref"]
        != source(state, stage).ref.model_dump(mode="json")
        or approved_context["recommendation_ref"] != f["recommendation_ref"]
        or proposal.parameters.model_dump(mode="json") != f["parameters"]
    ):
        raise ValueError("finishing execution differs from its human approval")
    if before_execution:
        records = history(state, stage)
        validate_proposal(stage, proposal, records, maximum_attempts(state, stage))
        if (
            f["attempt"] != len(records)
            or approved_context["history_refs"] != f["history_refs"]
            or f["phase"] != "run"
        ):
            raise ValueError("finishing attempt/history differs from approval")
        cursor = output(state, stage)
        if stage != "t2t" and any(
            cursor.get(k) != v for k, v in f["parameters"].items()
        ):
            raise ValueError("finishing execution parameters changed after approval")
    return proposal
