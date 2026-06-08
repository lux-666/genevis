"""Tests for natural-language entry intent parsing."""

from __future__ import annotations

import json
import unittest
from typing import get_args

from app.entrypoints.intent import (
    _make_schema_strict,
    parse_intent_response,
    parse_request_with_llm,
    reject_lifecycle_request,
)
from app.llm.config import load_llm_profile
from app.llm.prompts import load_prompt_bundle
from app.schemas.entry import EntryAssetTypeHint, EntryRequest
from app.schemas.run_config import RequestableStage


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

    def test_entry_request_can_start_from_source_paths_before_manifest_materialization(self):
        request = EntryRequest(session_id="s1", source_paths=["inputs/assembly.fa"], data_dir="out")

        self.assertEqual(request.source_paths, ["inputs/assembly.fa"])
        with self.assertRaisesRegex(ValueError, "manifest"):
            request.to_cli_argv()

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

    def test_missing_manifest_and_source_paths_fails(self):
        with self.assertRaisesRegex(ValueError, "manifest or source_paths"):
            parse_intent_response('{"command":"run","session_id":"s1"}')

    def test_manifest_and_source_paths_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "manifest and source_paths are mutually exclusive"):
            parse_intent_response(
                '{"command":"run","session_id":"s1","manifest":"inputs/manifest.yaml","source_paths":["reads.fastq"]}'
            )

    def test_blank_paths_are_rejected(self):
        for field in ("manifest", "data_dir"):
            with self.subTest(field=field):
                payload = {"command": "run", "session_id": "s1", "manifest": "inputs/manifest.yaml", "data_dir": "data"}
                payload[field] = ""
                with self.assertRaisesRegex(ValueError, field):
                    parse_intent_response(json.dumps(payload))

    def test_blank_source_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "source_paths"):
            parse_intent_response('{"command":"run","session_id":"s1","source_paths":[""]}')

    def test_non_positive_genome_size_hint_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "genome_size_estimate"):
            parse_intent_response(
                '{"command":"run","session_id":"s1","source_paths":["reads.fastq"],"genome_size_estimate":0}'
            )

    def test_intent_prompt_lists_all_entry_asset_type_hints(self):
        system_prompt = load_prompt_bundle("intent.parse").system

        for asset_type_hint in get_args(EntryAssetTypeHint):
            with self.subTest(asset_type_hint=asset_type_hint):
                self.assertIn(asset_type_hint, system_prompt)

    def test_intent_prompt_lists_requestable_stages(self):
        system_prompt = load_prompt_bundle("intent.parse").system

        self.assertIn("requested_stages", system_prompt)
        for stage in get_args(RequestableStage):
            with self.subTest(stage=stage):
                self.assertIn(stage, system_prompt)
        for phrase in ("组装/assemble", "质控/read QC", "功能注释/functional annotation", "全流程"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, system_prompt)

    def test_intent_prompt_describes_cyclone_fasta_raw_read_rules(self):
        system_prompt = load_prompt_bundle("intent.parse").system

        self.assertIn("cyclone文件", system_prompt)
        self.assertIn("cycloneseq", system_prompt)
        self.assertIn("raw_long_reads", system_prompt)
        self.assertIn(".fasta", system_prompt)

    def test_cyclone_nanopore_fasta_raw_reads_parse_result_is_valid(self):
        request = parse_intent_response(
            json.dumps(
                {
                    "command": "run",
                    "source_paths": ["/data/work/genevis/data/bht.Nanopore.pass.fa"],
                    "platform": "cycloneseq",
                    "asset_type_hint": "raw_long_reads",
                    "requested_stages": ["assembly"],
                }
            )
        )

        self.assertEqual(request.source_paths, ["/data/work/genevis/data/bht.Nanopore.pass.fa"])
        self.assertEqual(request.platform, "cycloneseq")
        self.assertEqual(request.asset_type_hint, "raw_long_reads")
        self.assertEqual(request.requested_stages, ["assembly"])

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

    def test_parse_request_does_not_infer_stage_goal_when_model_omits_it(self):
        model = _FakeModel(
            json.dumps(
                {
                    "command": "run",
                    "source_paths": ["/data/input/reads.fastq.gz"],
                    "platform": "cycloneseq",
                    "asset_type_hint": "raw_long_reads",
                }
            )
        )

        request = parse_request_with_llm("/data/input/reads.fastq.gz 做一下组装", model=model)

        self.assertIsNone(request.requested_stages)

    def test_parse_request_does_not_treat_method_name_as_stage_goal(self):
        model = _FakeModel(
            json.dumps(
                {
                    "command": "run",
                    "source_paths": ["/data/input/reads.fastq.gz"],
                    "platform": "cycloneseq",
                    "asset_type_hint": "raw_long_reads",
                }
            )
        )

        request = parse_request_with_llm("/data/input/reads.fastq.gz 用 Flye", model=model)

        self.assertIsNone(request.requested_stages)

    def test_strict_response_schema_recursively_forbids_extra_properties(self):
        schema = _make_schema_strict(
            {
                "type": "object",
                "properties": {
                    "outer": {
                        "type": "object",
                        "properties": {"inner": {"type": "string"}},
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                        },
                    },
                },
            }
        )

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["outer", "items"])
        self.assertFalse(schema["properties"]["outer"]["additionalProperties"])
        self.assertEqual(schema["properties"]["outer"]["required"], ["inner"])
        item_schema = schema["properties"]["items"]["items"]
        self.assertFalse(item_schema["additionalProperties"])
        self.assertEqual(item_schema["required"], ["name"])

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
