"""LLM-backed review recommendations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.execution.artifacts import append_artifact_records
from app.llm.factory import build_chat_model
from app.llm.prompts import PROMPT_ROOT, load_prompt_bundle, render_prompt_messages
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.schemas.artifacts import ArtifactMeta


class RecommendationError(ValueError):
    """Raised when an LLM recommendation cannot be used."""


_REVIEW_RECOMMENDATION_BUNDLES = {
    "assembly_review": "review.assembly_review_recommend",
}


class RecommendationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    stage: str
    decision: str
    candidate_id: str | None = None
    method_name: str | None = None
    final_assembly_ref: str | None = None
    rejected_candidates: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def recommend_review_decision(
    *,
    session_id: str,
    stage: str,
    request: ReviewPayload,
    data_dir: str | Path,
    chat_model: Any | None = None,
) -> ResumeReviewRequest:
    bundle = _load_stage_prompt_bundle(stage)
    messages = render_prompt_messages(
        bundle,
        {
            "session_id": session_id,
            "stage": stage,
            "review_request_json": json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2),
        },
    )
    model = _bind_recommendation_response_format(chat_model or build_chat_model())
    raw_text = _invoke_model(model, messages)
    raw = _parse_json(raw_text)
    normalized = _normalize_recommendation(raw, session_id=session_id, stage=stage)
    draft = RecommendationDraft.model_validate(normalized)
    if draft.session_id != session_id:
        raise RecommendationError("recommendation session_id does not match review request")
    if draft.stage != stage:
        raise RecommendationError("recommendation stage does not match review request")
    if draft.decision not in request.decisions_allowed:
        raise RecommendationError(f"recommendation decision {draft.decision!r} is not allowed")

    artifact_ref = _write_recommendation_artifact(
        session_id=session_id,
        stage=stage,
        data_dir=data_dir,
        payload={
            "session_id": session_id,
            "stage": stage,
            "model": _model_identifier(model),
            "prompt_bundle": bundle.name,
            "prompt_paths": _prompt_paths(bundle.name),
            "decisions_allowed": list(request.decisions_allowed),
            "review_request": request.model_dump(mode="json"),
            "raw_recommendation": raw,
            "recommendation": normalized,
            "rationale_text": raw_text,
        },
    )
    completed = {
        **draft.model_dump(mode="json"),
        "decided_by": "llm_provisional",
        "rationale_artifact_ref": artifact_ref,
    }
    try:
        return ResumeReviewRequest.model_validate(completed)
    except ValidationError as exc:
        raise RecommendationError(f"recommendation failed resume schema validation: {exc}") from exc


def _load_stage_prompt_bundle(stage: str):
    bundle_name = _REVIEW_RECOMMENDATION_BUNDLES.get(stage)
    if not bundle_name:
        raise RecommendationError(f"review recommendation prompt not found for stage: {stage}")
    return load_prompt_bundle(bundle_name)


def _prompt_paths(bundle_name: str) -> dict[str, str]:
    domain, task = bundle_name.split(".", 1)
    prompt_dir = PROMPT_ROOT / domain
    return {
        "system": str((prompt_dir / f"{task}_system.md").resolve()),
        "user": str((prompt_dir / f"{task}_user.md").resolve()),
    }


def _bind_recommendation_response_format(chat_model: Any) -> Any:
    bind = getattr(chat_model, "bind", None)
    if not callable(bind):
        return chat_model

    schema = RecommendationDraft.model_json_schema()
    schema["additionalProperties"] = False
    try:
        return bind(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "review_recommendation",
                    "schema": schema,
                    "strict": True,
                },
            }
        )
    except Exception:
        return bind(response_format={"type": "json_object"})


def _invoke_model(chat_model: Any, messages: list[dict[str, str]]) -> str:
    try:
        response = chat_model.invoke(messages)
    except Exception as exc:
        raise RecommendationError(f"review recommendation LLM call failed: {type(exc).__name__}: {exc}") from exc
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
    if not isinstance(content, str) or not content.strip():
        raise RecommendationError("review recommendation LLM returned empty content")
    return content.strip()


def _normalize_recommendation(raw: dict[str, Any], *, session_id: str, stage: str) -> dict[str, Any]:
    normalized = dict(raw)
    normalized.setdefault("session_id", session_id)
    normalized.setdefault("stage", stage)
    if isinstance(normalized.get("notes"), str):
        normalized["notes"] = [normalized["notes"]]
    if "rationale" in normalized and "notes" not in normalized:
        rationale = normalized.pop("rationale")
        normalized["notes"] = [rationale] if isinstance(rationale, str) else rationale
    return normalized


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RecommendationError(f"review recommendation is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecommendationError("review recommendation JSON must be an object")
    return payload


def _model_identifier(model: Any) -> str | None:
    for attr in ("model_name", "model", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _write_recommendation_artifact(
    *,
    session_id: str,
    stage: str,
    data_dir: str | Path,
    payload: dict[str, Any],
) -> str:
    out_dir = Path(data_dir) / "artifacts" / stage / session_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stage}_review_recommendation.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact = ArtifactMeta.from_file(
        path,
        stage=stage,
        method_name=f"{stage}.review_recommender",
        artifact_type=f"{stage}.review_recommendation",
        metadata={"decided_by": "llm_provisional"},
    )
    append_artifact_records(data_dir, session_id, [artifact])
    return str(path)
