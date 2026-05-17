"""LLM-backed review recommendations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.execution.artifacts import append_artifact_records
from app.llm.factory import build_chat_model
from app.llm.prompts import PROMPT_ROOT
from app.review.base import ResumeReviewRequest, ReviewPayload
from app.schemas.artifacts import ArtifactMeta


class RecommendationError(ValueError):
    """Raised when an LLM recommendation cannot be used."""


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
    prompt = _load_stage_prompt(stage)
    model = chat_model or build_chat_model()
    raw_text = _invoke_model(
        model,
        _messages(session_id=session_id, stage=stage, request=request, prompt=prompt),
    )
    raw = _parse_json(raw_text)
    draft = RecommendationDraft.model_validate(raw)
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
            "prompt_path": str((PROMPT_ROOT / "review" / stage / "recommend.md").resolve()),
            "decisions_allowed": list(request.decisions_allowed),
            "review_request": request.model_dump(mode="json"),
            "recommendation": raw,
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


def _load_stage_prompt(stage: str) -> str:
    path = PROMPT_ROOT / "review" / stage / "recommend.md"
    if not path.exists():
        raise RecommendationError(f"review recommendation prompt not found for stage: {stage}")
    return path.read_text(encoding="utf-8").strip()


def _messages(*, session_id: str, stage: str, request: ReviewPayload, prompt: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You recommend a provisional genome workflow review decision. "
                "Return only JSON fields accepted by ResumeReviewRequest. "
                "Do not include decided_by or rationale_artifact_ref."
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    prompt,
                    f"session_id: {session_id}",
                    f"stage: {stage}",
                    "review_request_json:",
                    json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2),
                ]
            ),
        },
    ]


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
