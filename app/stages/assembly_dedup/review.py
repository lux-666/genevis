"""Apply request-bound dedup recommendations through the existing review gate."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from app.control.lifecycle import ResumeValidationError
from app.control.workflow_plan import current_workflow_step_id
from app.llm.dedup import PurgeParameters, PurgeRecommendation
from app.review.application import (
    ForegroundReviewMetadataV1,
    PreparedReviewApplication,
    ReviewIntentV1,
    VerifiedReviewApplication,
)
from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request, persist_review_decision
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import StateUpdate, TopLevelState
from app.stages.assembly_dedup.advisor import (
    GATE,
    STAGE,
    history,
    load_json,
    validate_proposal,
)
from app.stages.assembly_dedup.state import assembly_dedup_output as output
from app.stages.assembly_dedup.state import update_assembly_dedup_output as update
from app.stages.base import stage_artifacts_dir

EFFECT = "assembly_dedup.recommendation.v1"


def validate_approved_attempt(state: TopLevelState) -> None:
    """Recheck the immutable approval before constructing any executable script."""
    cursor = output(state)
    records = history(state)
    proposal = PurgeRecommendation.model_validate(
        load_json(state, cursor["recommendation_ref"])["proposal"]
    )
    validate_proposal(proposal, records, cursor.get("max_attempts", 3))
    decision = load_json(state, cursor["decision_ref"])
    request = load_review_request(
        state["data_dir"],
        ArtifactRef.model_validate(decision["request_ref"]),
        session_id=state["session_id"],
    )
    context = request["payload"]["candidate_comparison_context"]
    approved = proposal.parameters
    if (
        proposal.action != "run"
        or approved is None
        or decision["decision"]["decision"] != "follow_recommendation"
        or decision["gate_id"] != GATE
        or decision["workflow_step_id"] != current_workflow_step_id(state)
        or context["recommendation_ref"] != cursor["recommendation_ref"]
        or context["source_ref"] != cursor["candidate_bundle_ref"]
        or context["history_refs"] != cursor.get("history_refs", [])
        or cursor.get("current_attempt") != len(records)
        or cursor.get("coverage_mode") != approved.coverage_mode
        or cursor.get("self_alignment_preset") != approved.self_alignment_preset
        or cursor.get("get_seqs_exclude_haplotigs") != approved.end_only
        or cursor.get("output_assembly_ref")
        != str(
            stage_artifacts_dir(
                state,
                STAGE,
                "attempts",
                f"{len(records):03d}",
                "purged_assembly.fasta",
            )
        )
    ):
        raise ValueError("dedup execution differs from its approved recommendation")


class DedupReviewEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["run", "accept", "keep_source", "stop"]
    parameters: PurgeParameters | None
    selected_attempt: int | None
    recommendation_ref: ArtifactRef
    decision_ref: ArtifactRef
    source_ref: ArtifactRef
    history_refs: list[ArtifactRef]


def validate_intent_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    record: Mapping[str, Any],
) -> ResumeReviewRequest:
    cursor = output(state)
    interrupt: Mapping[str, Any] = (
        state.get("active_interrupt") or cursor.get("active_interrupt") or {}
    )
    request_ref = ArtifactRef.model_validate(interrupt.get("request_ref"))
    verified = load_review_request(
        state["data_dir"], request_ref, session_id=state["session_id"]
    )
    if (
        record != verified
        or request.session_id != state["session_id"]
        or request.stage != STAGE
        or request.gate_id != GATE
        or request.review_request_id != interrupt.get("review_request_id")
        or request.review_request_hash != request_ref.sha256
        or interrupt.get("request_hash") != request_ref.sha256
        or record.get("workflow_step_id")
        != current_workflow_step_id(state, expected_stage=STAGE)
        or request.decision not in record["payload"]["decisions_allowed"]
        or cursor.get("status") != "blocked_human"
    ):
        raise ResumeValidationError("dedup review identity or decision mismatch")
    context = record["payload"]["candidate_comparison_context"]
    if (
        context["recommendation_ref"] != cursor.get("recommendation_ref")
        or context["source_ref"] != cursor.get("candidate_bundle_ref")
        or context["history_refs"] != cursor.get("history_refs", [])
        or context["quast_report_ref"] != cursor.get("quast_report_ref")
    ):
        raise ResumeValidationError("dedup review evidence changed")
    records = history(state)
    ForegroundReviewMetadataV1.model_validate(request.metadata)
    if request.decided_by != "human" and (
        request.decided_by != "auto_rule"
        or request.decision != "follow_recommendation"
        or request.metadata.get("source") != "foreground_countdown"
    ):
        raise ResumeValidationError(
            "dedup automatic decisions require the foreground countdown"
        )
    if request.decision == "follow_recommendation":
        proposal = PurgeRecommendation.model_validate(
            load_json(state, cursor["recommendation_ref"])["proposal"]
        )
        validate_proposal(proposal, records, cursor.get("max_attempts", 3))
        if proposal.action == "human_review":
            raise ResumeValidationError("dedup recommendation requires human judgment")
    elif request.decision == "accept_candidate":
        if request.candidate_id not in {f"attempt_{row['attempt']}" for row in records}:
            raise ResumeValidationError("select a completed dedup candidate")
    if request.method_name or request.candidate_ids or request.rejected_candidates:
        raise ResumeValidationError(
            "dedup review does not accept method or candidate-list overrides"
        )
    return request


def normalize_timeout_request(
    state: TopLevelState,
    request: ResumeReviewRequest,
    timeout: dict[str, Any],
) -> ResumeReviewRequest:
    metadata = ForegroundReviewMetadataV1.model_validate(request.metadata)
    if (
        request.decided_by != "auto_rule"
        or request.decision != "follow_recommendation"
        or metadata.source != "foreground_countdown"
        or timeout.get("configured_seconds") != metadata.configured_seconds
        or timeout.get("expired") is not True
    ):
        raise ResumeValidationError(
            "dedup recommendation is not approved by an expired countdown"
        )
    return request


def prepare_application(
    state: TopLevelState, intent: ReviewIntentV1
) -> PreparedReviewApplication:
    record = load_review_request(
        state["data_dir"], intent.request_ref, session_id=state["session_id"]
    )
    request = validate_intent_request(state, intent.request, record)
    cursor = output(state)
    if intent.workflow_step_id != current_workflow_step_id(state):
        raise ResumeValidationError("dedup intent occurrence mismatch")
    action, parameters, selected = "keep_source", None, None
    if request.decision == "follow_recommendation":
        proposal = PurgeRecommendation.model_validate(
            load_json(state, cursor["recommendation_ref"])["proposal"]
        )
        action, parameters, selected = (
            proposal.action,
            proposal.parameters,
            proposal.selected_attempt,
        )
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
        stage=STAGE,
        gate_id=GATE,
        decision=request,
        request_hash=intent.request_hash,
        request_ref=intent.request_ref,
    )
    effect = DedupReviewEffect(
        action=cast(Literal["run", "accept", "keep_source", "stop"], action),
        parameters=parameters,
        selected_attempt=selected,
        recommendation_ref=ArtifactRef.model_validate(cursor["recommendation_ref"]),
        decision_ref=decision_ref,
        source_ref=ArtifactRef.model_validate(cursor["candidate_bundle_ref"]),
        history_refs=[
            ArtifactRef.model_validate(ref) for ref in cursor.get("history_refs", [])
        ],
    )
    return PreparedReviewApplication(
        effect_kind=EFFECT,
        decision_ref=decision_ref,
        effect=effect,
        required_refs=(
            effect.recommendation_ref,
            effect.source_ref,
            *effect.history_refs,
            ArtifactRef.model_validate(cursor["quast_report_ref"]),
        ),
    )


def apply_application(
    state: TopLevelState, application: VerifiedReviewApplication
) -> StateUpdate:
    effect, identity = application.effect, application.application
    cursor = output(state)
    interrupt: Mapping[str, Any] = (
        state.get("active_interrupt") or cursor.get("active_interrupt") or {}
    )
    if (
        not isinstance(effect, DedupReviewEffect)
        or identity.effect_kind != EFFECT
        or identity.session_id != state["session_id"]
        or identity.stage != STAGE
        or identity.gate_id != GATE
        or identity.workflow_step_id != current_workflow_step_id(state)
        or identity.review_request_id != interrupt.get("review_request_id")
        or identity.request_hash != interrupt.get("request_hash")
        or identity.decision_ref != effect.decision_ref
        or effect.source_ref
        != ArtifactRef.model_validate(cursor["candidate_bundle_ref"])
        or effect.recommendation_ref
        != ArtifactRef.model_validate(cursor["recommendation_ref"])
        or [ref.model_dump(mode="json") for ref in effect.history_refs]
        != cursor.get("history_refs", [])
    ):
        raise ResumeValidationError("dedup application binding mismatch")
    records = history(state)
    changes: dict[str, Any] = {
        "active_interrupt": None,
        "decision_ref": effect.decision_ref.model_dump(mode="json"),
    }
    if effect.action == "run":
        proposal = PurgeRecommendation.model_validate(
            load_json(state, effect.recommendation_ref)["proposal"]
        )
        validate_proposal(proposal, records, cursor.get("max_attempts", 3))
        if proposal.action != "run" or proposal.parameters != effect.parameters:
            raise ResumeValidationError(
                "approved dedup parameters differ from proposal"
            )
        assert effect.parameters is not None
        changes.update(
            status="ready_to_run",
            current_attempt=len(records),
            execution_attempts=[],
            coverage_mode=effect.parameters.coverage_mode,
            self_alignment_preset=effect.parameters.self_alignment_preset,
            get_seqs_exclude_haplotigs=effect.parameters.end_only,
            cutoffs_strategy="calcuts_auto"
            if effect.parameters.coverage_mode == "read_depth"
            else "none",
            output_assembly_ref=str(
                stage_artifacts_dir(
                    state,
                    STAGE,
                    "attempts",
                    f"{len(records):03d}",
                    "purged_assembly.fasta",
                )
            ),
        )
    elif effect.action == "accept":
        selected = next(
            (row for row in records if row["attempt"] == effect.selected_attempt), None
        )
        if selected is None:
            raise ResumeValidationError("adopted dedup attempt is unavailable")
        changes.update(
            status="dedup_selected",
            adopted_attempt=effect.selected_attempt,
            purged_candidate_bundle_ref=selected["candidate_ref"],
            purged_primary_output_ref=selected["primary_ref"],
        )
    elif effect.action == "keep_source":
        changes.update(
            status="pass_through",
            adopted_attempt=None,
            purge_dups_status="completed" if records else "skipped",
            purge_dups_skip_reason="source_retained_by_review",
        )
    else:
        changes.update(status="failed", reason="stopped by dedup review")
    return {
        **update(state, changes),
        "active_interrupt": None,
        "supervision_state": "running_stage",
    }
