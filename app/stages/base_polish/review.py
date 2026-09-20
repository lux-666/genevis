"""Durable manual adoption of one validated base-polishing result."""

from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from app.control.lifecycle import ResumeValidationError
from app.control.workflow_plan import current_workflow_step_id, fail_workflow_step
from app.review.application import (
    PreparedReviewApplication,
    ReviewIntentV1,
    VerifiedReviewApplication,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request, persist_review_decision
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.base_polish.nodes import METHOD, deliver_candidate, source_candidate
from app.stages.base_polish.state import read_base_polish_stage_output as output
from app.stages.base_polish.state import update_base_polish_stage_output as update
from app.stages.base_polish.validation import load_comparison
from app.storage.artifacts import resolve_artifact_ref
from app.storage.assembly_candidates import (
    load_executed_candidate_bundle,
)

GATE = "base_polish_adoption"
EFFECT = "base_polish.adoption.v1"
DECISIONS = ("accept_candidate", "keep_prior_assembly", "stop_human_review")


class BasePolishAdoptionEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["accept_candidate", "keep_prior_assembly", "stop_human_review"]
    decision_ref: ArtifactRef
    source_candidate_ref: ArtifactRef
    candidate_ref: ArtifactRef


def validate_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    request_record: Mapping[str, Any],
) -> ResumeReviewRequest:
    interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    ref = ArtifactRef.model_validate(interrupt.get("request_ref"))
    step_id = current_workflow_step_id(state, expected_stage="base_polish")
    if (
        request.session_id != state["session_id"]
        or request.stage != "base_polish"
        or request.gate_id != GATE
        or request.decision not in DECISIONS
        or request.decided_by != "human"
        or request.review_request_id != interrupt.get("review_request_id")
        or request.review_request_hash != ref.sha256
        or interrupt.get("request_hash") != ref.sha256
        or interrupt.get("workflow_step_id") != step_id
        or request_record.get("review_request_id") != request.review_request_id
        or request_record.get("stage") != "base_polish"
        or request_record.get("gate_id") != GATE
        or request_record.get("workflow_step_id") != step_id
    ):
        raise ResumeValidationError(
            "BasePolish adoption identity or manual decision mismatch"
        )
    if (
        request.candidate_id
        or request.candidate_ids
        or request.method_name
        or request.rejected_candidates
    ):
        raise ResumeValidationError(
            "BasePolish adopts the single request-bound result, not another candidate"
        )
    cursor = output(state)
    candidate = source_candidate(state)
    refs = {
        key: dict(cursor).get(key)
        for key in (
            "source_candidate_ref",
            "output_ref",
            "comparison_ref",
            "polished_candidate_ref",
        )
    }
    if (request_record.get("payload") or {}).get("candidates") != [refs]:
        raise ResumeValidationError(
            "BasePolish result differs from the recorded review request"
        )
    if candidate.ref != ArtifactRef.model_validate(refs["source_candidate_ref"]):
        raise ResumeValidationError("BasePolish source differs from review request")
    for raw in refs.values():
        resolve_artifact_ref(
            state["data_dir"], state["session_id"], ArtifactRef.model_validate(raw)
        )
    report = load_comparison(
        state["data_dir"],
        state["session_id"],
        refs["comparison_ref"],
        candidate_refs=[
            candidate.ref,
            ArtifactRef.model_validate(refs["polished_candidate_ref"]),
        ],
    )
    if report["output_ref"] != refs["output_ref"]:
        raise ResumeValidationError("BasePolish review output differs from comparison")
    return request


def prepare_application(
    state: TopLevelState, intent: ReviewIntentV1
) -> PreparedReviewApplication:
    record = load_review_request(
        state["data_dir"], intent.request_ref, session_id=state["session_id"]
    )
    request = validate_intent_request(state, intent.request, record)
    if intent.workflow_step_id != current_workflow_step_id(state):
        raise ResumeValidationError("BasePolish intent occurrence mismatch")
    active: Mapping[str, Any] = state.get("active_interrupt") or {}
    if intent.request_ref != ArtifactRef.model_validate(active.get("request_ref")):
        raise ResumeValidationError("BasePolish intent ref mismatch")
    source = source_candidate(state)
    cursor = output(state)
    decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=intent.review_request_id,
        stage="base_polish",
        gate_id=GATE,
        decision=request,
        request_hash=intent.request_hash,
        request_ref=intent.request_ref,
        method_name=METHOD,
    )
    candidate = source
    if request.decision == "accept_candidate":
        candidate = load_executed_candidate_bundle(
            state["data_dir"],
            state["session_id"],
            ArtifactRef.model_validate(cursor["polished_candidate_ref"]),
        )
    return PreparedReviewApplication(
        effect_kind=EFFECT,
        decision_ref=decision_ref,
        effect=BasePolishAdoptionEffect(
            decision=cast(
                Literal["accept_candidate", "keep_prior_assembly", "stop_human_review"],
                request.decision,
            ),
            decision_ref=decision_ref,
            source_candidate_ref=source.ref,
            candidate_ref=candidate.ref,
        ),
        required_refs=(
            source.ref,
            candidate.ref,
            candidate.primary_output_ref,
            ArtifactRef.model_validate(cursor["comparison_ref"]),
            ArtifactRef.model_validate(cursor["output_ref"]),
            ArtifactRef.model_validate(cursor["polished_candidate_ref"]),
        ),
    )


def apply_application(
    state: TopLevelState, application: VerifiedReviewApplication
) -> StateUpdate:
    effect = application.effect
    identity = application.application
    interrupt: Mapping[str, Any] = state.get("active_interrupt") or {}
    if not isinstance(effect, BasePolishAdoptionEffect):
        raise ResumeValidationError("invalid BasePolish adoption effect")
    if (
        identity.effect_kind != EFFECT
        or identity.stage != "base_polish"
        or identity.gate_id != GATE
        or identity.session_id != state["session_id"]
        or identity.workflow_step_id
        != current_workflow_step_id(state, expected_stage="base_polish")
        or identity.decision_ref != effect.decision_ref
        or identity.review_request_id != interrupt.get("review_request_id")
        or identity.request_hash != interrupt.get("request_hash")
        or source_candidate(state).ref != effect.source_candidate_ref
    ):
        raise ResumeValidationError("BasePolish application binding mismatch")
    candidate = load_executed_candidate_bundle(
        state["data_dir"], state["session_id"], effect.candidate_ref
    )
    if effect.decision == "accept_candidate":
        if candidate.primary_output_ref != ArtifactRef.model_validate(
            output(state)["output_ref"]
        ):
            raise ResumeValidationError("BasePolish adopted candidate output mismatch")
    elif candidate.ref != effect.source_candidate_ref:
        raise ResumeValidationError("BasePolish retained candidate mismatch")
    stage_update = update(
        state,
        {
            "status": "failed"
            if effect.decision == "stop_human_review"
            else "completed",
            "active_interrupt": None,
            "decision_ref": effect.decision_ref.model_dump(mode="json"),
            "candidate_ref": candidate.ref.model_dump(mode="json"),
        },
    )
    if effect.decision == "stop_human_review":
        return {
            **fail_workflow_step(state, "base_polish", "stopped by human review"),
            **stage_update,
            "active_interrupt": None,
        }
    return {
        **deliver_candidate(
            state, candidate, materialized=effect.decision == "accept_candidate"
        ),
        **stage_update,
    }
