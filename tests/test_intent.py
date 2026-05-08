"""Tests for natural-language entry intent parsing."""

from __future__ import annotations

import json
import unittest

from app.entrypoints.intent import parse_intent_response, parse_request_with_llm, reject_lifecycle_request
from app.llm.config import load_llm_profile
from app.schemas.entry import EntryRequest


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.bound_formats: list[dict] = []

    def bind(self, **kwargs):
        self.bound_formats.append(kwargs)
        return self

    def invoke(self, messages):
        self.messages = messages
        return _FakeResponse(self.content)


class IntentParsingTests(unittest.TestCase):
    def test_entry_request_converts_to_run_argv(self):
        request = EntryRequest(session_id="s1", manifest="inputs/manifest.yaml")

        self.assertEqual(
            request.to_cli_argv(),
            [
                "run",
                "--manifest",
                "inputs/manifest.yaml",
                "--data-dir",
                "data",
                "--session-id",
                "s1",
            ],
        )

    def test_entry_request_can_omit_session_id(self):
        request = EntryRequest(manifest="inputs/manifest.yaml")

        self.assertIsNone(request.session_id)
        self.assertEqual(request.to_cli_argv(), ["run", "--manifest", "inputs/manifest.yaml", "--data-dir", "data"])

    def test_valid_json_parses_entry_request(self):
        request = parse_intent_response(
            '{"command":"run","session_id":"s1","manifest":"inputs/manifest.yaml","data_dir":"out"}'
        )

        self.assertEqual(request.session_id, "s1")
        self.assertEqual(request.manifest, "inputs/manifest.yaml")
        self.assertEqual(request.data_dir, "out")

    def test_fenced_json_parses_entry_request(self):
        request = parse_intent_response(
            '```json\n{"command":"run","session_id":"s1","manifest":"inputs/manifest.yaml"}\n```'
        )

        self.assertEqual(request.data_dir, "data")

    def test_defaults_fill_missing_stable_fields_without_override(self):
        request = parse_intent_response(
            '{"command":"run","session_id":"parsed","manifest":"inputs/manifest.yaml"}',
            defaults={"session_id": "default", "data_dir": "custom"},
        )

        self.assertEqual(request.session_id, "parsed")
        self.assertEqual(request.data_dir, "custom")

    def test_missing_required_manifest_fails(self):
        with self.assertRaisesRegex(ValueError, "manifest"):
            parse_intent_response('{"command":"run","session_id":"s1"}')

    def test_blank_paths_are_rejected(self):
        for field in ("manifest", "data_dir"):
            with self.subTest(field=field):
                payload = {"command": "run", "session_id": "s1", "manifest": "inputs/manifest.yaml", "data_dir": "data"}
                payload[field] = ""
                with self.assertRaisesRegex(ValueError, field):
                    parse_intent_response(json.dumps(payload))

    def test_blank_session_id_is_rejected_when_explicit(self):
        with self.assertRaisesRegex(ValueError, "session_id"):
            EntryRequest(session_id="", manifest="inputs/manifest.yaml")

    def test_path_shaped_session_id_is_rejected(self):
        for session_id in ("/tmp/x", "nested/s1", "nested\\s1", ".", ".."):
            with self.subTest(session_id=session_id):
                with self.assertRaisesRegex(ValueError, "session_id"):
                    EntryRequest(session_id=session_id, manifest="inputs/manifest.yaml")

    def test_extra_fields_are_rejected(self):
        for field in ("stages", "method", "threshold", "review_decision"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    parse_intent_response(
                        '{"command":"run","session_id":"s1","manifest":"inputs/manifest.yaml","%s":"x"}' % field
                    )

    def test_non_object_json_fails(self):
        with self.assertRaisesRegex(ValueError, "object"):
            parse_intent_response('[{"command":"run"}]')

    def test_no_json_fails(self):
        with self.assertRaisesRegex(ValueError, "Could not find"):
            parse_intent_response("run session s1")

    def test_malformed_json_fails(self):
        with self.assertRaisesRegex(ValueError, "decoding failed"):
            parse_intent_response('{"command":"run",')

    def test_multiple_json_objects_fail(self):
        with self.assertRaisesRegex(ValueError, "multiple JSON objects"):
            parse_intent_response(
                '{"command":"run","session_id":"s1","manifest":"a.yaml"}\n'
                '{"command":"run","session_id":"s2","manifest":"b.yaml"}'
            )

    def test_lifecycle_requests_are_rejected(self):
        for text in ("resume session s1", "inspect s1", "retry assembly", "继续 s1", "重建 s1"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "Lifecycle requests"):
                    reject_lifecycle_request(text)

    def test_fake_model_integration_returns_entry_request(self):
        model = _FakeModel('{"command":"run","session_id":"s1","manifest":"inputs/manifest.yaml"}')

        request = parse_request_with_llm("use inputs/manifest.yaml for s1", model=model)

        self.assertEqual(request.session_id, "s1")
        self.assertTrue(model.bound_formats)
        self.assertEqual(model.messages[0]["role"], "system")
        self.assertEqual(model.messages[1]["role"], "user")

    def test_llm_profile_uses_gene_agent_namespace_only(self):
        profile = load_llm_profile(
            {
                "BIOAGENT_LLM_API_KEY": "sk-bioagent",
                "GENE_AGENT_LLM_API_KEY": "sk-gene",
                "GENE_AGENT_LLM_MODEL": "gene-model",
            }
        )

        self.assertEqual(profile.api_key, "sk-gene")
        self.assertEqual(profile.model, "gene-model")

    def test_llm_profile_does_not_fallback_to_bioagent_namespace(self):
        profile = load_llm_profile({"BIOAGENT_LLM_API_KEY": "sk-bioagent"})

        self.assertIsNone(profile.api_key)


if __name__ == "__main__":
    unittest.main()
