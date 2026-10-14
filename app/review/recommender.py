"""LLM-backed review recommendations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError

from app.llm.factory import build_chat_model
from app.llm.prompts import (
    PROMPT_ROOT,
    PromptBundle,
    load_prompt_bundle,
    render_prompt_messages,
)
from app.llm.recommender import StructuredRecommender
from app.review.base import ResumeReviewRequest
from app.review.gates import review_prompt_bundle
from app.review.persistence import (
    ReviewPersistenceError,
    load_review_request,
    persist_review_recommendation,
)
from app.schemas.artifacts import ArtifactRef


class RecommendationError(ValueError):
    """Raised when an LLM recommendation cannot be used."""


class RecommendationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_refs: list[str]
    available_signals: list[str]
    missing_or_pending_signals: list[str]


class RecommendationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    stage: str
    decision: str
    candidate_id: str | None
    method_name: str | None
    rejected_candidates: list[str]
    notes: list[str]
    metadata: RecommendationMetadata


def recommend_review_decision(
    *,
    session_id: str,
    stage: str,
    review_request_id: str,
    request_ref: ArtifactRef | Mapping[str, Any],
    request_hash: str,
    data_dir: str | Path,
    chat_model: Any | None = None,
) -> ResumeReviewRequest:
    ref, request_record = _load_bound_review_request(
        session_id=session_id,
        stage=stage,
        review_request_id=review_request_id,
        request_ref=request_ref,
        request_hash=request_hash,
        data_dir=data_dir,
    )
    request_body = request_record["payload"]
    decisions_allowed = request_body.get("decisions_allowed")
    if not isinstance(decisions_allowed, list) or not all(
        isinstance(decision, str) for decision in decisions_allowed
    ):
        raise RecommendationError(
            "canonical review request does not contain valid decisions_allowed"
        )
    gate_id = request_record["gate_id"]
    bundle = _load_stage_prompt_bundle(stage, gate_id)
    messages = render_prompt_messages(
        bundle,
        {
            "session_id": session_id,
            "stage": stage,
            "review_request_json": json.dumps(
                request_record,
                ensure_ascii=False,
                indent=2,
            ),
        },
    )
    result = StructuredRecommender(
        lambda payload: payload,
        schema=RecommendationDraft,
        method="json_schema",
    ).invoke(messages, model_factory=lambda: chat_model or build_chat_model())
    if not result.valid:
        error = result.error or "unknown model response failure"
        if error.startswith("JSONDecodeError:"):
            raise RecommendationError(
                f"review recommendation is not valid JSON: {error}"
            )
        if "LLM returned empty content" in error:
            raise RecommendationError(
                "review recommendation LLM returned empty content"
            )
        if "LLM response JSON must be an object" in error:
            raise RecommendationError("review recommendation JSON must be an object")
        raise RecommendationError(f"review recommendation LLM call failed: {error}")
    raw_text = result.raw_response
    raw = result.parsed
    assert raw is not None
    normalized = _normalize_recommendation(raw, session_id=session_id, stage=stage)
    draft = RecommendationDraft.model_validate(normalized)
    if draft.session_id != session_id:
        raise RecommendationError(
            "recommendation session_id does not match review request"
        )
    if draft.stage != stage:
        raise RecommendationError("recommendation stage does not match review request")
    if draft.decision not in decisions_allowed:
        raise RecommendationError(
            f"recommendation decision {draft.decision!r} is not allowed"
        )

    try:
        artifact_ref = persist_review_recommendation(
            data_dir,
            session_id=session_id,
            stage=stage,
            review_request_id=review_request_id,
            request_ref=ref,
            request_hash=request_hash,
            payload={
                "model": result.model_id,
                "prompt_bundle": bundle.name,
                "prompt_paths": _prompt_paths(bundle.name),
                "raw_recommendation": raw,
                "recommendation": normalized,
                "rationale_text": raw_text,
            },
        )
    except (ReviewPersistenceError, FileExistsError) as exc:
        raise RecommendationError(
            f"review recommendation artifact could not be published: {exc}"
        ) from exc
    completed = {
        **draft.model_dump(mode="json"),
        "gate_id": gate_id,
        "review_request_id": review_request_id,
        "review_request_hash": request_hash,
        "decided_by": "llm_provisional",
        "rationale_artifact_ref": artifact_ref.model_dump(mode="json"),
    }
    try:
        return ResumeReviewRequest.model_validate(completed)
    except ValidationError as exc:
        raise RecommendationError(
            f"recommendation failed resume schema validation: {exc}"
        ) from exc


def _load_stage_prompt_bundle(
    stage: str,
    gate_id: str | None = None,
) -> PromptBundle:
    try:
        bundle_name = review_prompt_bundle(stage, gate_id)
    except KeyError as exc:
        raise RecommendationError(
            f"review gate is not registered for stage: {stage}"
        ) from exc
    if not bundle_name:
        raise RecommendationError(
            f"review recommendation prompt not found for stage: {stage}"
        )
    return load_prompt_bundle(bundle_name)


def _load_bound_review_request(
    *,
    session_id: str,
    stage: str,
    review_request_id: str,
    request_ref: ArtifactRef | Mapping[str, Any],
    request_hash: str,
    data_dir: str | Path,
) -> tuple[ArtifactRef, dict[str, Any]]:
    try:
        ref = (
            request_ref
            if isinstance(request_ref, ArtifactRef)
            else ArtifactRef.model_validate(request_ref)
        )
    except Exception as exc:
        raise RecommendationError(
            "review recommendation request_ref is invalid"
        ) from exc
    if request_hash != ref.sha256:
        raise RecommendationError(
            "review recommendation request_hash does not match request_ref.sha256"
        )
    try:
        record = load_review_request(
            data_dir,
            ref,
            session_id=session_id,
        )
    except ReviewPersistenceError as exc:
        raise RecommendationError(
            f"canonical review request could not be verified: {exc}"
        ) from exc
    if record.get("review_request_id") != review_request_id:
        raise RecommendationError(
            "review recommendation ID does not match canonical review request"
        )
    if record.get("stage") != stage:
        raise RecommendationError(
            "review recommendation stage does not match canonical review request"
        )
    return ref, record


def _prompt_paths(bundle_name: str) -> dict[str, str]:
    domain, task = bundle_name.split(".", 1)
    prompt_dir = PROMPT_ROOT / domain
    return {
        "system": str((prompt_dir / f"{task}_system.md").resolve()),
        "user": str((prompt_dir / f"{task}_user.md").resolve()),
    }


def _normalize_recommendation(
    raw: dict[str, Any], *, session_id: str, stage: str
) -> dict[str, Any]:
    normalized = dict(raw)
    normalized.setdefault("session_id", session_id)
    normalized.setdefault("stage", stage)
    if isinstance(normalized.get("notes"), str):
        normalized["notes"] = [normalized["notes"]]
    if "rationale" in normalized and "notes" not in normalized:
        rationale = normalized.pop("rationale")
        normalized["notes"] = [rationale] if isinstance(rationale, str) else rationale
    normalized.setdefault("candidate_id", None)
    normalized.setdefault("method_name", None)
    normalized.setdefault("rejected_candidates", [])
    normalized.setdefault("notes", [])
    metadata = normalized.setdefault("metadata", {})
    if isinstance(metadata, dict):
        normalized["metadata"] = {
            "evidence_refs": [],
            "available_signals": [],
            "missing_or_pending_signals": [],
            **metadata,
        }
    return normalized
