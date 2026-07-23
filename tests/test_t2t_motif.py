"""Tests for the T2T species-motif resolution boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas.resource_ref import ResourceRef
from app.schemas.state import initial_state
from app.stages.base import stage_output
from app.stages.t2t.builder import build_stage
from app.stages.t2t.nodes import enter_t2t
from app.stages.t2t.parameters.storage import (
    read_t2t_parameter_artifact,
    write_t2t_parameter_artifact,
)
from app.stages.t2t.parameters.workflow import (
    recommend_t2t_parameters,
    resolve_t2t_telomere_motif,
)
from app.stages.t2t.routing import route_after_enter
from tests.data_v2 import v2_run_dir


class T2TMotifResolutionTests(unittest.TestCase):
    def _state(self, tmpdir: str, *, species_facts: dict | None = None) -> dict:
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
        )
        state["stage_outputs"] = {
            "t2t": {
                "status": "resolved",
                "execution_mode": "local",
                "ont_read_refs": ["reads.fastq"],
                "input_assembly_ref": "assembly.fa",
                "configured_telomere_motif": None,
            }
        }
        if species_facts is not None:
            state["shared_parameters"] = {"species_facts": species_facts}
        return state

    @staticmethod
    def _response(payload: dict) -> SimpleNamespace:
        return SimpleNamespace(content=json.dumps(payload))

    def test_species_facts_are_resolved_and_persisted_as_one_canonical_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Arabidopsis thaliana",
                    "taxon_id": 3702,
                },
            )
            state["stage_outputs"]["t2t"]["configured_telomere_motif"] = "TTAGGG"
            with (
                patch(
                    "app.stages.t2t.parameters.workflow.write_t2t_parameter_artifact",
                    wraps=write_t2t_parameter_artifact,
                ) as write_artifact,
                patch(
                    "app.llm.parameters.t2t.motif.build_chat_model"
                ) as build_model,
            ):
                build_model.return_value.invoke.return_value = self._response(
                    {
                        "status": "resolved",
                        "telomere_motif": "tttaggg",
                        "summary": (
                            "The supplied plant identity supports the common plant "
                            "repeat."
                        ),
                    }
                )
                update = resolve_t2t_telomere_motif(state)

            resolved = {
                **state,
                **update,
                "stage_outputs": update["stage_outputs"],
            }
            output = stage_output(resolved, "t2t")
            self.assertEqual(output["status"], "motif_resolved")
            self.assertEqual(output["resolved_telomere_motif"], "TTTAGGG")
            self.assertEqual(
                output["resolved_telomere_motif_source"], "llm_species_resolution"
            )
            self.assertNotIn("telomere_motif", output)
            request = read_t2t_parameter_artifact(
                resolved,
                output["telomere_motif_model_request_ref"],
            )
            request_json = json.dumps(request)
            self.assertIn("Arabidopsis thaliana", request_json)
            self.assertIn("Drosophila melanogaster", request_json)
            resolution = read_t2t_parameter_artifact(
                resolved,
                output["telomere_motif_resolution_ref"],
            )
            self.assertEqual(resolution["resolved_telomere_motif"], "TTTAGGG")
            self.assertEqual(build_model.return_value.invoke.call_count, 1)
            resolution_writes = [
                call
                for call in write_artifact.call_args_list
                if call.args[2] == "motif_resolution.json"
            ]
            self.assertEqual(len(resolution_writes), 1)

    def test_stage_routes_species_motif_before_threshold_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "assembly.fa"
            assembly.write_text(">chr1\nTTTAGGGTTTAGGG\n", encoding="utf-8")
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text(
                "@read1\nTTTAGGGTTTAGGG\n+\nIIIIIIIIIIIIII\n",
                encoding="utf-8",
            )
            registry = Path(tmpdir) / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "assets": [
                            {
                                "asset_id": "read-1",
                                "asset_type": "clean_long_reads",
                                "path": str(reads),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(v2_run_dir(tmpdir, "s1")),
            )
            state["asset_registry_ref"] = str(registry)
            state["resource_refs"]["assembly"] = ResourceRef(
                resource_type="assembly",
                paths=[str(assembly)],
                source_stage="assembly_review",
            ).to_dict()
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "completed",
                    "final_assembly_ref": str(assembly),
                },
                "t2t": {"execution_mode": "local"},
            }
            state["shared_parameters"] = {
                "species_facts": {"scientific_name": "Arabidopsis thaliana"}
            }

            with (
                patch("app.llm.parameters.t2t.motif.build_chat_model") as motif_model,
                patch(
                    "app.llm.parameters.t2t.recommender.build_chat_model"
                ) as parameter_model,
            ):
                motif_model.return_value.invoke.return_value = self._response(
                    {"status": "resolved", "telomere_motif": "TTTAGGG"}
                )
                parameter_model.return_value.invoke.return_value = self._response(
                    {"parameters": {}}
                )
                result = build_stage().invoke(state)

            output = stage_output(result, "t2t")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(output["resolved_telomere_motif"], "TTTAGGG")
            self.assertEqual(motif_model.return_value.invoke.call_count, 1)
            self.assertEqual(parameter_model.return_value.invoke.call_count, 1)

    def test_existing_model_response_is_replayed_without_a_second_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={"scientific_name": "Bombyx mori"},
            )
            response = self._response(
                {
                    "status": "resolved",
                    "telomere_motif": "TTAGG",
                    "summary": "Species-specific insect example.",
                }
            )
            with patch(
                "app.llm.parameters.t2t.motif.build_chat_model"
            ) as build_model:
                build_model.return_value.invoke.return_value = response
                first_update = resolve_t2t_telomere_motif(state)
            first_state = {**state, **first_update}
            first_state["stage_outputs"]["t2t"]["status"] = "resolved"

            with patch(
                "app.llm.parameters.t2t.motif.build_chat_model",
                side_effect=AssertionError("motif response should be recovered"),
            ):
                second_update = resolve_t2t_telomere_motif(first_state)

            output = stage_output({**first_state, **second_update}, "t2t")
            self.assertEqual(output["resolved_telomere_motif"], "TTAGG")
            self.assertEqual(output["status"], "motif_resolved")

    def test_legacy_checkpoint_is_migrated_before_scanning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"]["t2t"].update(
                {
                    "status": "parameters_ready",
                    "configured_telomere_motif": None,
                    "telomere_motif": "ttaggg",
                }
            )

            update = enter_t2t(state)
            resumed = {**state, **update}
            output = stage_output(resumed, "t2t")

            self.assertEqual(output["resolved_telomere_motif"], "TTAGGG")
            self.assertEqual(
                output["resolved_telomere_motif_source"], "legacy_t2t_checkpoint"
            )
            self.assertEqual(route_after_enter(resumed), "scan_assembly")

    def test_invalid_legacy_checkpoint_routes_through_fail_node(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"]["t2t"].update(
                {
                    "status": "parameters_ready",
                    "configured_telomere_motif": None,
                    "telomere_motif": "not-dna",
                }
            )

            update = enter_t2t(state)
            resumed = {**state, **update}
            output = stage_output(resumed, "t2t")

            self.assertEqual(output["status"], "failed")
            self.assertNotIn("supervision_state", update)
            self.assertNotIn("last_error", update)
            self.assertEqual(route_after_enter(resumed), "fail")

    def test_threshold_request_consumes_only_the_resolved_motif(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={"scientific_name": "Arabidopsis thaliana"},
            )
            with patch(
                "app.llm.parameters.t2t.motif.build_chat_model"
            ) as build_motif_model:
                build_motif_model.return_value.invoke.return_value = self._response(
                    {
                        "status": "resolved",
                        "telomere_motif": "TTTAGGG",
                    }
                )
                motif_update = resolve_t2t_telomere_motif(state)
            resolved_state = {**state, **motif_update}

            with patch(
                "app.llm.parameters.t2t.recommender.build_chat_model"
            ) as build_parameter_model:
                build_parameter_model.return_value.invoke.return_value = self._response(
                    {"parameters": {}, "summary": "Use executor defaults."}
                )
                recommendation_update = recommend_t2t_parameters(resolved_state)

            blocked_state = {**resolved_state, **recommendation_update}
            output = stage_output(blocked_state, "t2t")
            profile = read_t2t_parameter_artifact(
                blocked_state,
                output["current_parameter_recommendation"]["manifest_ref"],
            )
            input_profile = read_t2t_parameter_artifact(
                blocked_state,
                profile["refs"]["input_profile_ref"],
            )
            self.assertEqual(input_profile["resolved_telomere_motif"], "TTTAGGG")

    def test_malformed_or_unresolved_model_response_fails_before_scan(self):
        cases = [
            {"status": "resolved", "telomere_motif": "not-dna"},
            {
                "status": "unresolved",
                "telomere_motif": None,
                "summary": "No simple repeat.",
            },
            {"status": "unresolved"},
        ]
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmpdir:
                state = self._state(
                    tmpdir,
                    species_facts={"scientific_name": "Drosophila melanogaster"},
                )
                with patch(
                    "app.llm.parameters.t2t.motif.build_chat_model"
                ) as build_model:
                    build_model.return_value.invoke.return_value = self._response(
                        payload
                    )
                    update = resolve_t2t_telomere_motif(state)
                output = stage_output({**state, **update}, "t2t")
                self.assertEqual(output["status"], "failed")
                self.assertNotIn("resolved_telomere_motif", output)
                self.assertIn("telomere_motif_model_response_ref", output)
                self.assertIn("telomere_motif_resolution_ref", output)

    def test_no_species_facts_uses_explicit_compatibility_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"]["t2t"]["configured_telomere_motif"] = "TTAGGG"
            with patch(
                "app.llm.parameters.t2t.motif.build_chat_model",
                side_effect=AssertionError(
                    "compatibility fallback should not call model"
                ),
            ):
                update = resolve_t2t_telomere_motif(state)
            output = stage_output({**state, **update}, "t2t")
            self.assertEqual(output["resolved_telomere_motif"], "TTAGGG")
            self.assertEqual(
                output["resolved_telomere_motif_source"], "legacy_t2t_config"
            )

    def test_no_species_facts_or_compatibility_hint_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            with patch(
                "app.llm.parameters.t2t.motif.build_chat_model",
                side_effect=AssertionError("missing species context must not call model"),
            ):
                update = resolve_t2t_telomere_motif(state)
            output = stage_output({**state, **update}, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertNotIn("resolved_telomere_motif", output)
            self.assertIn("requires species facts", output["reason"])

    def test_invalid_compatibility_hint_fails_as_stage_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"]["t2t"]["configured_telomere_motif"] = "not-dna"
            update = resolve_t2t_telomere_motif(state)
            output = stage_output({**state, **update}, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("configured_telomere_motif", output["reason"])


if __name__ == "__main__":
    unittest.main()
