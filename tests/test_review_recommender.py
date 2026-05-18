"""Tests for LLM review recommendation artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.review.base import ReviewPayload
from app.review.recommender import RecommendationError, recommend_review_decision


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    def __init__(self, content: str, *, model: str | None = "fake-model-v1") -> None:
        self.content = content
        self.model_name = model

    def invoke(self, messages):
        self.messages = messages
        return _Response(self.content)


class ReviewRecommenderTests(unittest.TestCase):
    def test_recommender_writes_artifact_and_returns_llm_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = ReviewPayload(
                stage="assembly_review",
                reason="review",
                candidates=[{"candidate_id": "flye", "final_assembly_ref": "draft.fasta"}],
                decisions_allowed=["accept", "stop_human_review"],
            )
            chat = _FakeChat(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "accept",
                        "candidate_id": "flye",
                        "final_assembly_ref": "draft.fasta",
                        "notes": ["metrics support provisional accept"],
                    }
                )
            )

            decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                request=request,
                data_dir=tmp,
                chat_model=chat,
            )

            self.assertEqual(decision.decided_by, "llm_provisional")
            self.assertTrue(decision.rationale_artifact_ref)
            self.assertTrue(Path(decision.rationale_artifact_ref or "").exists())
            records = (Path(tmp) / "artifacts" / "s1" / "artifacts.jsonl").read_text(encoding="utf-8")
            self.assertIn("assembly_review.review_recommendation", records)
            artifact_payload = json.loads(Path(decision.rationale_artifact_ref or "").read_text(encoding="utf-8"))
            self.assertEqual(artifact_payload["model"], "fake-model-v1")
            self.assertTrue(artifact_payload["prompt_path"].endswith("review/assembly_review/recommend.md"))
            self.assertEqual(artifact_payload["decisions_allowed"], ["accept", "stop_human_review"])
            self.assertIn("review_request", artifact_payload)
            self.assertIn("raw_recommendation", artifact_payload)
            self.assertIn("recommendation", artifact_payload)
            self.assertIn("rationale_text", artifact_payload)

    def test_recommender_normalizes_common_llm_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = ReviewPayload(stage="assembly_review", reason="review", decisions_allowed=["stop_human_review"])
            chat = _FakeChat(
                json.dumps(
                    {
                        "decision": "stop_human_review",
                        "notes": "Only QUAST is available; keep a human in the loop.",
                    }
                )
            )

            decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                request=request,
                data_dir=tmp,
                chat_model=chat,
            )

            self.assertEqual(decision.session_id, "s1")
            self.assertEqual(decision.stage, "assembly_review")
            self.assertEqual(decision.notes, ["Only QUAST is available; keep a human in the loop."])
            artifact_payload = json.loads(Path(decision.rationale_artifact_ref or "").read_text(encoding="utf-8"))
            self.assertNotIn("session_id", artifact_payload["raw_recommendation"])
            self.assertEqual(artifact_payload["recommendation"]["session_id"], "s1")
            self.assertEqual(artifact_payload["recommendation"]["stage"], "assembly_review")

    def test_recommender_handles_llm_invocation_exception(self):
        class _RaisingChat:
            def invoke(self, messages):
                raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            request = ReviewPayload(stage="assembly_review", reason="review", decisions_allowed=["accept"])
            with self.assertRaisesRegex(RecommendationError, "LLM call failed"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    request=request,
                    data_dir=tmp,
                    chat_model=_RaisingChat(),
                )

    def test_recommender_rejects_disallowed_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = ReviewPayload(
                stage="assembly_review",
                reason="review",
                decisions_allowed=["accept"],
            )
            chat = _FakeChat(
                json.dumps(
                    {
                        "session_id": "s1",
                        "stage": "assembly_review",
                        "decision": "purge",
                    }
                )
            )

            with self.assertRaisesRegex(RecommendationError, "not allowed"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    request=request,
                    data_dir=tmp,
                    chat_model=chat,
                )

    def test_recommender_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = ReviewPayload(stage="assembly_review", reason="review", decisions_allowed=["accept"])

            with self.assertRaisesRegex(RecommendationError, "not valid JSON"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    request=request,
                    data_dir=tmp,
                    chat_model=_FakeChat("not json"),
                )


if __name__ == "__main__":
    unittest.main()
