"""Tests for LLM review recommendation artifacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.llm.prompts import load_prompt_bundle, render_prompt_messages
from app.review.base import ReviewPayload
from app.review.persistence import PersistedReviewRequest, persist_review_request
from app.review.recommender import RecommendationError, recommend_review_decision
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    def __init__(self, content: str, *, model: str | None = "fake-model-v1") -> None:
        self.content = content
        self.model_name = model
        self.bound_formats = []

    def bind(self, **kwargs):
        self.bound_formats.append(kwargs)
        return self

    def invoke(self, messages):
        self.messages = messages
        return _Response(self.content)


def _persist_request(run_dir: Path, request: ReviewPayload) -> PersistedReviewRequest:
    return persist_review_request(
        {"session_id": "s1", "data_dir": str(run_dir)},
        stage=request.stage,
        gate_id=request.gate_id or request.stage,
        reason=request.reason,
        payload=request,
    )


def _artifact_path(run_dir: Path, path_ref: str | None) -> Path:
    assert path_ref is not None
    return SessionPaths.from_run_dir(run_dir).resolve_ref(path_ref)


class ReviewRecommenderTests(unittest.TestCase):
    def test_review_prompt_bundle_loads_and_renders_context(self):
        bundle = load_prompt_bundle("review.assembly_review_recommend")
        messages = render_prompt_messages(
            bundle,
            {
                "session_id": "s1",
                "stage": "assembly_review",
                "review_request_json": '{"decisions_allowed": ["accept"]}',
            },
        )

        self.assertIn("Return exactly one JSON object", messages[0]["content"])
        self.assertIn("session_id:\ns1", messages[1]["content"])
        self.assertIn("stage:\nassembly_review", messages[1]["content"])
        self.assertIn('"decisions_allowed": ["accept"]', messages[1]["content"])

    def test_recommender_writes_artifact_and_returns_llm_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
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
            persisted = _persist_request(run_dir, request)

            decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                review_request_id=persisted.review_request_id,
                request_ref=persisted.request_ref,
                request_hash=persisted.request_hash,
                data_dir=run_dir,
                chat_model=chat,
            )

            self.assertEqual(decision.decided_by, "llm_provisional")
            self.assertEqual(decision.review_request_id, persisted.review_request_id)
            self.assertEqual(decision.review_request_hash, persisted.request_hash)
            self.assertTrue(decision.rationale_artifact_ref)
            artifact_path = _artifact_path(run_dir, decision.rationale_artifact_ref)
            self.assertTrue(artifact_path.exists())
            records = (run_dir / "artifacts" / "index.jsonl").read_text(encoding="utf-8")
            self.assertIn("assembly_review.review_recommendation", records)
            artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact_payload["model"], "fake-model-v1")
            self.assertEqual(artifact_payload["prompt_bundle"], "review.assembly_review_recommend")
            self.assertTrue(artifact_payload["prompt_paths"]["system"].endswith("review/assembly_review_recommend_system.md"))
            self.assertTrue(artifact_payload["prompt_paths"]["user"].endswith("review/assembly_review_recommend_user.md"))
            self.assertEqual(artifact_payload["review_request_id"], persisted.review_request_id)
            self.assertEqual(artifact_payload["request_hash"], persisted.request_hash)
            self.assertEqual(artifact_payload["request_ref"], persisted.request_ref.model_dump(mode="json"))
            self.assertNotIn("decisions_allowed", artifact_payload)
            self.assertNotIn("review_request", artifact_payload)
            self.assertIn("raw_recommendation", artifact_payload)
            self.assertIn("recommendation", artifact_payload)
            self.assertIn("rationale_text", artifact_payload)
            self.assertEqual(chat.bound_formats[0]["response_format"]["type"], "json_schema")
            self.assertEqual(chat.messages[0]["role"], "system")
            self.assertIn("Return exactly one JSON object", chat.messages[0]["content"])
            self.assertEqual(chat.messages[1]["role"], "user")
            self.assertIn("session_id:\ns1", chat.messages[1]["content"])
            self.assertIn("review_request_json:", chat.messages[1]["content"])

    def test_recommender_context_includes_related_species_evidence_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(
                stage="assembly_review",
                reason="review",
                decisions_allowed=["stop_human_review"],
                related_species_evidence_pack={
                    "matching_mode": "same_genus",
                    "selected_records": [{"accession": "GCF_019176455.1"}],
                    "computed_summary": {"selected_record_count": 1},
                },
            )
            chat = _FakeChat(json.dumps({"decision": "stop_human_review"}))
            persisted = _persist_request(run_dir, request)

            recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                review_request_id=persisted.review_request_id,
                request_ref=persisted.request_ref,
                request_hash=persisted.request_hash,
                data_dir=run_dir,
                chat_model=chat,
            )

            self.assertIn("related_species_evidence_pack", chat.messages[1]["content"])
            self.assertIn("GCF_019176455.1", chat.messages[1]["content"])

    def test_recommender_normalizes_common_llm_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(stage="assembly_review", reason="review", decisions_allowed=["stop_human_review"])
            chat = _FakeChat(
                json.dumps(
                    {
                        "decision": "stop_human_review",
                        "notes": "Only QUAST is available; keep a human in the loop.",
                    }
                )
            )
            persisted = _persist_request(run_dir, request)

            decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                review_request_id=persisted.review_request_id,
                request_ref=persisted.request_ref,
                request_hash=persisted.request_hash,
                data_dir=run_dir,
                chat_model=chat,
            )

            self.assertEqual(decision.session_id, "s1")
            self.assertEqual(decision.stage, "assembly_review")
            self.assertEqual(decision.notes, ["Only QUAST is available; keep a human in the loop."])
            artifact_payload = json.loads(
                _artifact_path(run_dir, decision.rationale_artifact_ref).read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("session_id", artifact_payload["raw_recommendation"])
            self.assertEqual(artifact_payload["recommendation"]["session_id"], "s1")
            self.assertEqual(artifact_payload["recommendation"]["stage"], "assembly_review")

    def test_recommender_handles_llm_invocation_exception(self):
        class _RaisingChat:
            def invoke(self, messages):
                raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(stage="assembly_review", reason="review", decisions_allowed=["accept"])
            persisted = _persist_request(run_dir, request)
            with self.assertRaisesRegex(RecommendationError, "LLM call failed"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    review_request_id=persisted.review_request_id,
                    request_ref=persisted.request_ref,
                    request_hash=persisted.request_hash,
                    data_dir=run_dir,
                    chat_model=_RaisingChat(),
                )

    def test_recommender_rejects_disallowed_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
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
            persisted = _persist_request(run_dir, request)

            with self.assertRaisesRegex(RecommendationError, "not allowed"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    review_request_id=persisted.review_request_id,
                    request_ref=persisted.request_ref,
                    request_hash=persisted.request_hash,
                    data_dir=run_dir,
                    chat_model=chat,
                )

    def test_recommender_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            request = ReviewPayload(stage="assembly_review", reason="review", decisions_allowed=["accept"])
            persisted = _persist_request(run_dir, request)

            with self.assertRaisesRegex(RecommendationError, "not valid JSON"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    review_request_id=persisted.review_request_id,
                    request_ref=persisted.request_ref,
                    request_hash=persisted.request_hash,
                    data_dir=run_dir,
                    chat_model=_FakeChat("not json"),
                )

    def test_recommender_keeps_multiple_rounds_for_one_stage_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            first_request = ReviewPayload(
                stage="assembly_review",
                reason="first review",
                candidates=[{"candidate_id": "flye"}],
                decisions_allowed=["accept", "stop_human_review"],
            )
            second_request = ReviewPayload(
                stage="assembly_review",
                reason="second review",
                candidates=[{"candidate_id": "canu"}],
                decisions_allowed=["accept", "stop_human_review"],
            )
            first_persisted = _persist_request(run_dir, first_request)
            first_decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                review_request_id=first_persisted.review_request_id,
                request_ref=first_persisted.request_ref,
                request_hash=first_persisted.request_hash,
                data_dir=run_dir,
                chat_model=_FakeChat(
                    json.dumps(
                        {
                            "decision": "accept",
                            "candidate_id": "flye",
                            "notes": ["first round"],
                        }
                    )
                ),
            )
            first_path = _artifact_path(run_dir, first_decision.rationale_artifact_ref)
            first_bytes = first_path.read_bytes()

            second_persisted = _persist_request(run_dir, second_request)
            second_decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                review_request_id=second_persisted.review_request_id,
                request_ref=second_persisted.request_ref,
                request_hash=second_persisted.request_hash,
                data_dir=run_dir,
                chat_model=_FakeChat(
                    json.dumps(
                        {
                            "decision": "accept",
                            "candidate_id": "canu",
                            "notes": ["second round"],
                        }
                    )
                ),
            )
            second_path = _artifact_path(run_dir, second_decision.rationale_artifact_ref)

            self.assertNotEqual(first_persisted.review_request_id, second_persisted.review_request_id)
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(first_path.read_bytes(), first_bytes)
            self.assertTrue(second_path.exists())
            recommendation_rows = [
                json.loads(line)
                for line in (run_dir / "artifacts" / "index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if "review_recommendation" in line
            ]
            self.assertEqual(len(recommendation_rows), 2)


if __name__ == "__main__":
    unittest.main()
