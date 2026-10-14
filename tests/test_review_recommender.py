"""Tests for the persisted assembly-review LLM recommendation contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from langchain_openai import ChatOpenAI

from app.llm.prompts import load_prompt_bundle, render_prompt_messages
from app.review.base import ReviewPayload
from app.review.persistence import persist_review_request
from app.review.recommender import RecommendationError, recommend_review_decision
from app.schemas.state import initial_state
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChat:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model_name = "fake-model-v1"
        self.bound_formats: list[dict] = []
        self.invocation_count = 0

    def bind(self, **kwargs):
        self.bound_formats.append(kwargs)
        return self

    def invoke(self, messages):
        self.invocation_count += 1
        self.messages = messages
        return _Response(self.content)


_ASSEMBLY_REVIEW_WORKFLOW_STEPS = [
    {
        "selected_method_names": [],
        "step_id": "step-000-inventory",
        "stage": "inventory",
        "allow_local_execution": False,
    },
    {
        "step_id": "step-001-assembly",
        "stage": "assembly",
        "allow_local_execution": True,
    },
    {
        "selected_method_names": [],
        "step_id": "step-002-assembly_review",
        "stage": "assembly_review",
        "allow_local_execution": True,
        "review_target": {
            "resource_type": "assembly",
            "producer_step_id": "step-001-assembly",
        },
    },
    {
        "selected_method_names": [],
        "step_id": "step-003-hic_scaffolding",
        "stage": "hic_scaffolding",
        "allow_local_execution": True,
    },
]


def _assembly_review_state(run_dir: Path) -> dict:
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
        workflow_steps=_ASSEMBLY_REVIEW_WORKFLOW_STEPS,
        stage_plan_ref="control/run_config.json",
        stage_seeds={"assembly_review": {"status": "pending"}},
    )
    state.update(
        {
            "workflow_step_index": 2,
            "entered_workflow_step_index": 2,
            "current_stage": "assembly_review",
        }
    )
    return state


def _persist_request(run_dir: Path, request: ReviewPayload):
    return persist_review_request(
        _assembly_review_state(run_dir),
        stage=request.stage,
        gate_id=request.gate_id or request.stage,
        reason=request.reason,
        payload=request,
    )


class ReviewRecommenderTests(unittest.TestCase):
    def test_assembly_review_prompt_bundle_renders_bound_context(self):
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
        self.assertIn('"decisions_allowed": ["accept"]', messages[1]["content"])

    def test_recommender_persists_llm_provisional_rationale_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    candidates=[{"candidate_id": "flye"}],
                    decisions_allowed=["accept", "stop_human_review"],
                    auto_decidable=True,
                ),
            )
            chat = _FakeChat(
                json.dumps(
                    {
                        "decision": "accept",
                        "candidate_id": "flye",
                        "notes": ["metrics support provisional accept"],
                    }
                )
            )

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
            paths = SessionPaths.from_run_dir(run_dir)
            self.assertEqual(persisted.workflow_step_id, "step-002-assembly_review")
            assert decision.rationale_artifact_ref is not None
            artifact_path = paths.resolve_ref(decision.rationale_artifact_ref.path_ref)
            self.assertEqual(
                artifact_path.parent,
                paths.workflow_step_artifact_path(
                    persisted.workflow_step_id,
                    "assembly_review",
                    "reviews",
                    persisted.review_request_id,
                ),
            )
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(artifact["model"], "fake-model-v1")
            self.assertEqual(chat.invocation_count, 1)
            self.assertEqual(artifact["review_request_id"], persisted.review_request_id)
            self.assertEqual(artifact["request_hash"], persisted.request_hash)
            self.assertIn("raw_recommendation", artifact)
            self.assertEqual(
                chat.bound_formats[0]["response_format"]["type"], "json_schema"
            )

    def test_recommender_context_includes_related_species_evidence_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    decisions_allowed=["stop_human_review"],
                    related_species_evidence_pack={
                        "matching_mode": "same_genus",
                        "selected_records": [{"accession": "GCF_019176455.1"}],
                    },
                ),
            )
            chat = _FakeChat(json.dumps({"decision": "stop_human_review"}))

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
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    decisions_allowed=["stop_human_review"],
                ),
            )

            decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                review_request_id=persisted.review_request_id,
                request_ref=persisted.request_ref,
                request_hash=persisted.request_hash,
                data_dir=run_dir,
                chat_model=_FakeChat(
                    json.dumps(
                        {
                            "decision": "stop_human_review",
                            "notes": (
                                "Only QUAST is available; keep a human in the loop."
                            ),
                        }
                    )
                ),
            )

            self.assertEqual(decision.session_id, "s1")
            self.assertEqual(decision.stage, "assembly_review")
            self.assertEqual(
                decision.notes, ["Only QUAST is available; keep a human in the loop."]
            )

    def test_recommender_handles_llm_invocation_exception(self):
        class _RaisingChat:
            def invoke(self, _messages):
                raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    decisions_allowed=["accept"],
                ),
            )
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

    def test_recommender_rejects_non_text_provider_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    decisions_allowed=["accept"],
                ),
            )
            chat = _FakeChat("{}")
            chat.content = {"decision": "accept"}  # type: ignore[assignment]

            with self.assertRaisesRegex(RecommendationError, "empty content"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    review_request_id=persisted.review_request_id,
                    request_ref=persisted.request_ref,
                    request_hash=persisted.request_hash,
                    data_dir=run_dir,
                    chat_model=chat,
                )

    def test_recommender_rejects_disallowed_decision_without_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    decisions_allowed=["accept"],
                ),
            )

            with self.assertRaisesRegex(RecommendationError, "not allowed"):
                recommend_review_decision(
                    session_id="s1",
                    stage="assembly_review",
                    review_request_id=persisted.review_request_id,
                    request_ref=persisted.request_ref,
                    request_hash=persisted.request_hash,
                    data_dir=run_dir,
                    chat_model=_FakeChat(json.dumps({"decision": "purge"})),
                )

    def test_real_client_sends_closed_required_schema_and_preserves_metadata(self):
        response = {
            "session_id": "s1",
            "stage": "assembly_review",
            "decision": "stop_human_review",
            "candidate_id": None,
            "method_name": None,
            "rejected_candidates": [],
            "notes": ["fixture rationale"],
            "metadata": {
                "evidence_refs": [],
                "available_signals": ["fixture_signal"],
                "missing_or_pending_signals": [],
            },
        }
        calls = []

        def check_schema(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(
                        set(node.get("required", [])), set(node["properties"])
                    )
                self.assertNotIn("default", node)
                for value in node.values():
                    check_schema(value)
            elif isinstance(node, list):
                for value in node:
                    check_schema(value)

        def handle(request):
            payload = json.loads(request.content)
            calls.append(payload)
            fmt = payload["response_format"]
            self.assertEqual(fmt["type"], "json_schema")
            self.assertIs(fmt["json_schema"]["strict"], True)
            check_schema(fmt["json_schema"]["schema"])
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "returned-fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(response),
                            },
                        }
                    ],
                },
            )

        with (
            tempfile.TemporaryDirectory() as tmp,
            httpx.Client(transport=httpx.MockTransport(handle)) as client,
        ):
            run_dir = v2_run_dir(tmp, "s1")
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    decisions_allowed=["stop_human_review"],
                ),
            )
            chat = ChatOpenAI(
                model="fixture-model",
                api_key="test-only",
                http_client=client,
                base_url="https://fixture.invalid/v1",
                max_retries=0,
            )
            decision = recommend_review_decision(
                session_id="s1",
                stage="assembly_review",
                review_request_id=persisted.review_request_id,
                request_ref=persisted.request_ref,
                request_hash=persisted.request_hash,
                data_dir=run_dir,
                chat_model=chat,
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(decision.metadata, response["metadata"])
            artifact = json.loads(
                SessionPaths.from_run_dir(run_dir)
                .resolve_ref(decision.rationale_artifact_ref.path_ref)
                .read_text()
            )
            self.assertEqual(artifact["model"], "returned-fixture-model")

    def test_recommender_rejects_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            persisted = _persist_request(
                run_dir,
                ReviewPayload(
                    stage="assembly_review",
                    reason="review",
                    decisions_allowed=["accept"],
                ),
            )
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


if __name__ == "__main__":
    unittest.main()
