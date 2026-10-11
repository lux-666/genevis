"""Focused tests for canonical T2T motif and threshold evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.llm.parameters.t2t.motif import T2TMotifRequest
from app.schemas.artifacts import (
    T2T_MOTIF_INPUT_SNAPSHOT,
    T2T_MOTIF_RESOLUTION,
    T2T_MOTIF_RESOLUTION_MANIFEST,
    T2T_PARAMETER_POLICY_RESPONSE,
    ArtifactMeta,
)
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    CanonicalSpeciesFacts,
    T2TMotifInputSnapshotV3,
)
from app.schemas.state import initial_state
from app.stages.base import stage_output
from app.stages.t2t.nodes import enter_t2t, scan_assembly_telomeres
from app.stages.t2t.parameters.catalog import load_t2t_parameter_catalog
from app.stages.t2t.parameters.execution import load_t2t_frozen_execution_inputs
from app.stages.t2t.parameters.storage import (
    read_artifact as read_t2t_parameter_artifact,
)
from app.stages.t2t.parameters.storage import (
    write_artifact as write_t2t_parameter_artifact,
)
from app.stages.t2t.parameters.workflow import (
    freeze_t2t_parameters,
    recommend_t2t_parameters,
    resolve_t2t_telomere_motif,
)
from app.storage.artifacts import publish_artifact_record
from app.storage.parameter_evidence import (
    load_approved_parameter_chain_v3,
)
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class T2TMotifResolutionTests(unittest.TestCase):
    def _state(
        self,
        tmpdir: str,
        *,
        species_facts: dict | None = None,
        read_count: int = 1,
    ) -> dict:
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(tmpdir, "s1")),
            workflow_steps=[
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-001-t2t",
                    "stage": "t2t",
                    "allow_local_execution": True,
                },
                {
                    "step_id": "step-002-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": False,
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-001-t2t",
                    },
                },
            ],
        )
        state["current_stage"] = "t2t"
        state["workflow_step_index"] = 1
        state["entered_workflow_step_index"] = 1
        read_inputs: list[dict[str, object]] = []
        read_refs: list[dict] = []
        paths = SessionPaths.from_run_dir(state["data_dir"])
        assembly_source = paths.workflow_step_work_path(
            "step-000-inventory", "inventory", "assembly.fa"
        )
        assembly_source.parent.mkdir(parents=True, exist_ok=True)
        assembly_source.write_text(">contig-1\nTTAGGGTTAGGG\n", encoding="utf-8")
        assembly_artifact = ArtifactMeta.from_file(
            assembly_source,
            stage="inventory",
            method_name="test.input_registry",
            artifact_type="input.assembly_fasta",
        )
        published_assembly = publish_artifact_record(
            state["data_dir"], state["session_id"], assembly_artifact
        )
        for index in range(read_count):
            source = paths.workflow_step_work_path(
                "step-000-inventory", "inventory", f"ont_{index:04d}.fastq"
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                f"@read-{index}\nTTAGGGTTAGGG\n+\nIIIIIIIIIIII\n",
                encoding="utf-8",
            )
            artifact = ArtifactMeta.from_file(
                source,
                stage="inventory",
                method_name="test.input_registry",
                artifact_type="input.long_reads",
            )
            published = publish_artifact_record(
                state["data_dir"], state["session_id"], artifact
            )
            read_inputs.append(
                {
                    "member_id": f"ont-{index:04d}",
                    "source_asset_id": f"ont-{index:04d}",
                    "path": str(published.path),
                    "platform": "ont",
                    "read_artifact_ref": published.ref.model_dump(mode="json"),
                }
            )
            read_refs.append(published.ref.model_dump(mode="json"))
        state["stage_outputs"] = {
            "t2t": {
                "status": "resolved",
                "execution_mode": "local",
                "mode": "auto_patch",
                "recommendation_mode": "advisor",
                "chromosome_list_ref": None,
                "long_read_refs": [str(item["path"]) for item in read_inputs],
                "long_read_inputs": read_inputs,
                "input_assembly_ref": str(published_assembly.path),
                "work_dir": str(
                    paths.workflow_step_work_path(
                        "step-001-t2t", "t2t", "t2t_finishing"
                    )
                ),
                "execution_enabled": True,
            }
        }
        Path(state["stage_outputs"]["t2t"]["work_dir"]).mkdir(
            parents=True, exist_ok=True
        )
        state["_test_assembly_ref"] = published_assembly.ref.model_dump(mode="json")
        state["_test_read_refs"] = read_refs
        if species_facts is not None:
            state["shared_parameters"] = {"species_facts": species_facts}
        return state

    @staticmethod
    def _response(payload: dict) -> SimpleNamespace:
        return SimpleNamespace(content=json.dumps(payload))

    @staticmethod
    def _merge(state: dict, update: dict) -> dict:
        merged = {**state, **update}
        if "stage_outputs" in update:
            merged["stage_outputs"] = update["stage_outputs"]
        return merged

    def test_recommendation_requires_deterministic_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            resolved = self._resolve_provided(state)
            failed = self._merge(resolved, recommend_t2t_parameters(resolved))
            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("deterministic_baseline", output["reason"])

    def test_baseline_rejects_recovered_saved_advisor_response(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            resolved = self._resolve_provided(state)
            resolved["stage_outputs"]["t2t"]["recommendation_mode"] = (
                "deterministic_baseline"
            )
            write_t2t_parameter_artifact(
                resolved,
                0,
                "policy_response.json",
                {
                    "schema_version": 2,
                    "raw_response": json.dumps(
                        {"parameters": {"min_mapq": 35}, "summary": "advisor"}
                    ),
                    "validity": "parsed",
                    "error": None,
                    "model_id": "t2t.parameter_recommender",
                },
                artifact_type=T2T_PARAMETER_POLICY_RESPONSE,
                method_name="t2t.parameter_recommender",
            )

            failed = self._merge(resolved, recommend_t2t_parameters(resolved))

            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn(
                "baseline response does not match its policy", output["reason"]
            )

    def test_model_requests_require_typed_canonical_inputs(self):
        with self.assertRaises(ValueError):
            T2TMotifRequest.model_validate(
                {"facts": {"scientific_name": "Homo sapiens"}}
            )

    def test_baseline_persists_and_recovers_without_prompt_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            resolved = self._resolve_provided(state)
            with patch("app.llm.prompts.PROMPT_ROOT", Path(tmpdir) / "no-prompts"):
                approved = self._recommend(resolved)
                replayed = self._recommend(resolved)
                ready = self._merge(replayed, freeze_t2t_parameters(replayed))
            output = stage_output(approved, "t2t")
            self.assertEqual(stage_output(ready, "t2t")["status"], "parameters_ready")
            self.assertEqual(
                output["parameter_attempts"],
                stage_output(replayed, "t2t")["parameter_attempts"],
            )
            bundle = load_approved_parameter_chain_v3(
                approved,
                attempt=output["parameter_attempts"][0],
                expected_stage="t2t",
                expected_method_id="t2t.alignment.minimap2",
            )
            refs = bundle.chain.manifest.refs
            self.assertEqual(
                bundle.chain.model_request,
                {
                    "schema_version": 3,
                    "policy": "deterministic_baseline",
                    "method_id": "t2t.alignment.minimap2",
                    "task": "recommend_initial",
                    "input_snapshot_sha256": refs.input_snapshot_ref.sha256,
                    "basis_sha256": refs.basis_snapshot_ref.sha256,
                },
            )
            self.assertFalse(bundle.chain.model_response["model_invoked"])
            self.assertEqual(bundle.chain.model_response["parameters"], {})
            self.assertNotIn("raw_response", bundle.chain.model_response)

    def _resolve_provided(self, state: dict) -> dict:
        with patch(
            "app.llm.parameters.t2t.motif.build_chat_model",
            side_effect=AssertionError("provided motif must not invoke a model"),
        ):
            update = resolve_t2t_telomere_motif(state)
        return self._merge(state, update)

    def _recommend(self, state: dict) -> dict:
        state["stage_outputs"]["t2t"]["recommendation_mode"] = "deterministic_baseline"
        with patch(
            "app.llm.parameters.base.build_chat_model",
            side_effect=AssertionError("deterministic baseline must not invoke model"),
        ):
            update = recommend_t2t_parameters(state)
        return self._merge(state, update)

    def test_provided_species_fact_persists_chain_without_model_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "taxon_id": 9606,
                    "telomere_motif": "ttaggg",
                },
            )
            resolved = self._resolve_provided(state)
            output = stage_output(resolved, "t2t")

            self.assertEqual(output["status"], "motif_resolved")
            self.assertEqual(output["resolved_telomere_motif"], "TTAGGG")
            self.assertEqual(
                output["resolved_telomere_motif_source"], "provided_species_fact"
            )
            for key in (
                "telomere_motif_model_request_ref",
                "telomere_motif_model_response_ref",
                "telomere_motif_response_parse_ref",
            ):
                self.assertNotIn(key, output)

            snapshot = read_t2t_parameter_artifact(
                resolved, output["telomere_motif_input_snapshot_ref"]
            )
            resolution = read_t2t_parameter_artifact(
                resolved, output["telomere_motif_resolution_ref"]
            )
            manifest = read_t2t_parameter_artifact(
                resolved, output["telomere_motif_resolution_manifest_ref"]
            )
            self.assertEqual(snapshot["schema_version"], 3)
            self.assertEqual(snapshot["snapshot_kind"], "t2t_motif_resolution")
            self.assertEqual(snapshot["resolver_id"], "t2t.telomere_motif_resolver")
            self.assertEqual(resolution["source"], "provided_species_fact")
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["resolution_mode"], "provided_species_fact")
            self.assertEqual(
                set(manifest["refs"]), {"input_snapshot_ref", "resolution_ref"}
            )

    def test_llm_resolution_requires_and_persists_all_conditional_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Arabidopsis thaliana",
                    "taxon_id": 3702,
                },
            )
            with patch("app.llm.parameters.t2t.motif.build_chat_model") as build_model:
                build_model.return_value.invoke.return_value = self._response(
                    {
                        "status": "resolved",
                        "telomere_motif": "tttaggg",
                        "summary": "Canonical plant repeat.",
                    }
                )
                update = resolve_t2t_telomere_motif(state)
            resolved = self._merge(state, update)
            output = stage_output(resolved, "t2t")

            self.assertEqual(output["status"], "motif_resolved")
            self.assertEqual(
                output["resolved_telomere_motif_source"], "llm_species_resolution"
            )
            manifest = read_t2t_parameter_artifact(
                resolved, output["telomere_motif_resolution_manifest_ref"]
            )
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["resolution_mode"], "llm_species_resolution")
            self.assertEqual(
                set(manifest["refs"]),
                {
                    "input_snapshot_ref",
                    "resolution_ref",
                    "model_request_ref",
                    "model_response_ref",
                    "response_parse_ref",
                },
            )
            request_record = read_t2t_parameter_artifact(
                resolved, output["telomere_motif_model_request_ref"]
            )
            rendered = json.dumps(request_record)
            self.assertIn(
                '\\"resolver_id\\":\\"t2t.telomere_motif_resolver\\"', rendered
            )
            self.assertIn('\\"facts\\":', rendered)
            self.assertNotIn('\\"task\\":', rendered)
            self.assertEqual(build_model.return_value.invoke.call_count, 1)

    def test_valid_manifest_replays_without_model_or_mutable_species_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Bombyx mori",
                    "telomere_motif": "TTAGG",
                },
            )
            first = self._resolve_provided(state)
            first["stage_outputs"]["t2t"]["status"] = "resolved"
            first["shared_parameters"] = {
                "species_facts": {"scientific_name": "Changed species"}
            }

            with patch(
                "app.llm.parameters.t2t.motif.build_chat_model",
                side_effect=AssertionError("valid motif chain must replay"),
            ):
                update = resolve_t2t_telomere_motif(first)
            replayed = self._merge(first, update)
            self.assertEqual(
                stage_output(replayed, "t2t")["resolved_telomere_motif"], "TTAGG"
            )

    def test_saved_motif_response_without_recommendation_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir, species_facts={"scientific_name": "Homo sapiens"}
            )
            with patch("app.llm.parameters.t2t.motif.build_chat_model") as model:
                model.return_value.invoke.return_value = self._response(
                    {
                        "status": "resolved",
                        "telomere_motif": "TTAGGG",
                        "summary": "Saved legacy response.",
                    }
                )
                first = self._merge(state, resolve_t2t_telomere_motif(state))
            cursor = first["stage_outputs"]["t2t"]
            cursor.pop("recommendation_mode")
            cursor.pop("telomere_motif_resolution_manifest_ref")
            cursor["status"] = "resolved"
            with patch("app.llm.parameters.t2t.motif.build_chat_model") as model:
                replay = self._merge(first, resolve_t2t_telomere_motif(first))
            model.assert_not_called()
            output = stage_output(replay, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn(
                "explicit advisor or deterministic_baseline", output["reason"]
            )

    def test_provided_resolution_rejects_motif_that_disagrees_with_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "CCCC",
                },
            )
            snapshot_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_input_snapshot.json",
                T2TMotifInputSnapshotV3(
                    schema_version=3,
                    facts=CanonicalSpeciesFacts(
                        scientific_name="Homo sapiens",
                        supplied_telomere_motif="CCCC",
                    ),
                ).model_dump(mode="json"),
                artifact_type=T2T_MOTIF_INPUT_SNAPSHOT,
                method_name="t2t.telomere_motif_resolver",
            )
            resolution_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_resolution.json",
                {
                    "schema_version": 1,
                    "resolution_contract": "t2t_telomere_motif_v1",
                    "status": "resolved",
                    "resolved_telomere_motif": "TTAGGG",
                    "source": "provided_species_fact",
                    "summary": None,
                },
                artifact_type=T2T_MOTIF_RESOLUTION,
                method_name="t2t.telomere_motif_resolver",
            )
            manifest_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_resolution_manifest.json",
                {
                    "schema_version": 2,
                    "resolution_contract": "t2t_telomere_motif_v1",
                    "resolver_id": "t2t.telomere_motif_resolver",
                    "resolution_mode": "provided_species_fact",
                    "status": "resolved",
                    "refs": {
                        "input_snapshot_ref": snapshot_ref.model_dump(mode="json"),
                        "resolution_ref": resolution_ref.model_dump(mode="json"),
                    },
                },
                artifact_type=T2T_MOTIF_RESOLUTION_MANIFEST,
                method_name="t2t.telomere_motif_resolver",
            )
            state["stage_outputs"]["t2t"]["telomere_motif_resolution_manifest_ref"] = (
                manifest_ref.model_dump(mode="json")
            )

            failed = self._merge(state, resolve_t2t_telomere_motif(state))

            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("supplied motif", output["reason"])

    def test_wrong_schema_manifest_and_snapshot_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir, species_facts={"scientific_name": "Homo sapiens"}
            )
            old_manifest_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_resolution_manifest.json",
                {"schema_version": 1, "refs": {}},
                artifact_type=T2T_MOTIF_RESOLUTION_MANIFEST,
                method_name="t2t.telomere_motif_resolver",
            )
            state["stage_outputs"]["t2t"]["telomere_motif_resolution_manifest_ref"] = (
                old_manifest_ref.model_dump(mode="json")
            )
            failed = self._merge(state, resolve_t2t_telomere_motif(state))
            self.assertEqual(stage_output(failed, "t2t")["status"], "failed")
            self.assertIn("manifest schema", stage_output(failed, "t2t")["reason"])

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir, species_facts={"scientific_name": "Homo sapiens"}
            )
            snapshot_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_input_snapshot.json",
                {"schema_version": 2},
                artifact_type=T2T_MOTIF_INPUT_SNAPSHOT,
                method_name="t2t.telomere_motif_resolver",
            )
            resolution_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_resolution.json",
                {
                    "schema_version": 1,
                    "resolution_contract": "t2t_telomere_motif_v1",
                    "status": "resolved",
                    "resolved_telomere_motif": "TTAGGG",
                    "source": "provided_species_fact",
                    "summary": None,
                },
                artifact_type=T2T_MOTIF_RESOLUTION,
                method_name="t2t.telomere_motif_resolver",
            )
            manifest_ref = write_t2t_parameter_artifact(
                state,
                0,
                "motif_resolution_manifest.json",
                {
                    "schema_version": 2,
                    "resolution_contract": "t2t_telomere_motif_v1",
                    "resolver_id": "t2t.telomere_motif_resolver",
                    "resolution_mode": "provided_species_fact",
                    "status": "resolved",
                    "refs": {
                        "input_snapshot_ref": snapshot_ref.model_dump(mode="json"),
                        "resolution_ref": resolution_ref.model_dump(mode="json"),
                    },
                },
                artifact_type=T2T_MOTIF_RESOLUTION_MANIFEST,
                method_name="t2t.telomere_motif_resolver",
            )
            state["stage_outputs"]["t2t"]["telomere_motif_resolution_manifest_ref"] = (
                manifest_ref.model_dump(mode="json")
            )
            failed = self._merge(state, resolve_t2t_telomere_motif(state))
            self.assertEqual(stage_output(failed, "t2t")["status"], "failed")
            self.assertIn("snapshot schema", stage_output(failed, "t2t")["reason"])

    def test_legacy_checkpoint_fields_are_rejected(self):
        for field in (
            "configured_telomere_motif",
            "telomere_motif",
            "legacy_motif_hint",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmpdir:
                state = self._state(
                    tmpdir, species_facts={"scientific_name": "Homo sapiens"}
                )
                state["stage_outputs"]["t2t"].update(
                    {"status": "parameters_ready", field: "TTAGGG"}
                )
                with self.assertRaises(ValidationError):
                    enter_t2t(state)

    def test_threshold_snapshot_binds_two_long_read_assets_and_motif_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
                read_count=2,
            )
            resolved = self._resolve_provided(state)
            blocked = self._recommend(resolved)
            output = stage_output(blocked, "t2t")
            manifest = read_t2t_parameter_artifact(
                blocked, output["current_parameter_recommendation"]["manifest_ref"]
            )
            self.assertEqual(manifest["schema_version"], 3)
            self.assertIn("input_snapshot_ref", manifest["refs"])
            self.assertNotIn("input_profile_ref", manifest["refs"])
            snapshot = read_t2t_parameter_artifact(
                blocked, manifest["refs"]["input_snapshot_ref"]
            )
            self.assertEqual(snapshot["schema_version"], 3)
            self.assertEqual(snapshot["stage_context"]["long_read_asset_count"], 2)
            self.assertEqual(
                snapshot["stage_context"]["motif_source"], "provided_species_fact"
            )
            dependency_refs = snapshot["dependency_refs"]
            self.assertEqual(
                dependency_refs["motif_resolution_ref"],
                output["telomere_motif_resolution_ref"],
            )
            self.assertEqual(len(dependency_refs["stage_input_refs"]), 3)
            self.assertEqual(
                [ref["artifact_id"] for ref in dependency_refs["stage_input_refs"]],
                [
                    state["_test_assembly_ref"]["artifact_id"],
                    *[ref["artifact_id"] for ref in state["_test_read_refs"]],
                ],
            )

    def test_threshold_snapshot_uses_indexed_asset_registry_for_external_assets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
                read_count=2,
            )
            paths = SessionPaths.from_run_dir(state["data_dir"])
            long_read_inputs = state["stage_outputs"]["t2t"]["long_read_inputs"]
            registry_assets = [
                {
                    "asset_id": item["source_asset_id"],
                    "asset_type": "clean_long_reads",
                    "path": item["path"],
                    "platform": item["platform"],
                    "format": "fastq",
                }
                for item in long_read_inputs
            ]
            registry_source = paths.workflow_step_work_path(
                "step-000-inventory", "inventory", "asset_registry.json"
            )
            registry_source.parent.mkdir(parents=True, exist_ok=True)
            registry_source.write_text(
                json.dumps(
                    {
                        "session_id": "s1",
                        "assets": registry_assets,
                    }
                ),
                encoding="utf-8",
            )
            registry_artifact = ArtifactMeta.from_file(
                registry_source,
                stage="inventory",
                method_name="inventory.register_assets",
                artifact_type="asset_registry",
            )
            published_registry = publish_artifact_record(
                state["data_dir"],
                state["session_id"],
                registry_artifact,
            )
            state["asset_registry_ref"] = published_registry.ref.model_dump(mode="json")

            blocked = self._recommend(self._resolve_provided(state))
            output = stage_output(blocked, "t2t")
            manifest = read_t2t_parameter_artifact(
                blocked, output["current_parameter_recommendation"]["manifest_ref"]
            )
            snapshot = read_t2t_parameter_artifact(
                blocked, manifest["refs"]["input_snapshot_ref"]
            )

            self.assertEqual(len(snapshot["dependency_refs"]["stage_input_refs"]), 2)
            self.assertEqual(
                snapshot["dependency_refs"]["stage_input_refs"][1]["artifact_type"],
                "asset_registry",
            )

    def test_threshold_snapshot_reuses_motif_snapshot_species_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Original species",
                    "telomere_motif": "TTAGGG",
                },
            )
            resolved = self._resolve_provided(state)
            resolved["shared_parameters"] = {
                "species_facts": {
                    "scientific_name": "Changed species",
                    "telomere_motif": "CCCC",
                }
            }

            blocked = self._recommend(resolved)
            output = stage_output(blocked, "t2t")
            manifest = read_t2t_parameter_artifact(
                blocked, output["current_parameter_recommendation"]["manifest_ref"]
            )
            snapshot = read_t2t_parameter_artifact(
                blocked, manifest["refs"]["input_snapshot_ref"]
            )

            self.assertEqual(
                snapshot["facts"]["species"]["scientific_name"], "Original species"
            )
            self.assertEqual(
                snapshot["facts"]["species"]["supplied_telomere_motif"],
                "TTAGGG",
            )

    def test_zero_long_read_assets_bypasses_threshold_recommendation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
                read_count=0,
            )
            resolved = self._resolve_provided(state)
            resolved["stage_outputs"]["t2t"]["recommendation_mode"] = (
                "deterministic_baseline"
            )
            with patch(
                "app.llm.parameters.base.build_chat_model",
                side_effect=AssertionError("zero assets must bypass recommendation"),
            ):
                self.assertEqual(recommend_t2t_parameters(resolved), {})

    def test_threshold_rejects_motif_checkpoint_that_disagrees_with_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            resolved = self._resolve_provided(state)
            resolved["stage_outputs"]["t2t"]["recommendation_mode"] = (
                "deterministic_baseline"
            )
            resolved["stage_outputs"]["t2t"]["resolved_telomere_motif"] = "CCCC"
            with patch(
                "app.llm.parameters.base.build_chat_model",
                side_effect=AssertionError("changed motif checkpoint must fail first"),
            ):
                failed = self._merge(
                    resolved,
                    recommend_t2t_parameters(resolved),
                )
            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("does not match its evidence chain", output["reason"])

    def test_freeze_rejects_non_baseline_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            approved = self._recommend(self._resolve_provided(state))
            output = stage_output(approved, "t2t")
            attempt = output["parameter_attempts"][0]
            bundle = load_approved_parameter_chain_v3(
                approved,
                attempt=attempt,
                expected_stage="t2t",
                expected_method_id="t2t.alignment.minimap2",
            )
            nonempty_l2_chain = replace(
                bundle.chain,
                recommendation={
                    **bundle.chain.recommendation,
                    "recommendation": {"parameters": {"min_mapq": 35}},
                },
            )
            missing_policy_chain = replace(
                bundle.chain,
                manifest=bundle.chain.manifest.model_copy(
                    update={"summary": {"response_parsed": True}}
                ),
            )
            human_decision = {
                **bundle.approval_decision,
                "decision": {
                    **bundle.approval_decision["decision"],
                    "decided_by": "human",
                },
            }
            non_policy_decision = {
                **bundle.approval_decision,
                "decision": {
                    **bundle.approval_decision["decision"],
                    "automatic_resolution": {
                        **bundle.approval_decision["decision"]["automatic_resolution"],
                        "source": "foreground_countdown",
                    },
                },
            }
            cases = {
                "nonempty_l2": replace(bundle, chain=nonempty_l2_chain),
                "missing_policy": replace(bundle, chain=missing_policy_chain),
                "human_decided_by": replace(bundle, approval_decision=human_decision),
                "non_policy_automatic_resolution": replace(
                    bundle, approval_decision=non_policy_decision
                ),
                "different_input_binding": replace(
                    bundle,
                    chain=replace(
                        bundle.chain,
                        model_request={
                            **bundle.chain.model_request,
                            "input_snapshot_sha256": "0" * 64,
                        },
                    ),
                ),
                "different_basis_binding": replace(
                    bundle,
                    chain=replace(
                        bundle.chain,
                        model_request={
                            **bundle.chain.model_request,
                            "basis_sha256": "0" * 64,
                        },
                    ),
                ),
                "model_response": replace(
                    bundle,
                    chain=replace(
                        bundle.chain,
                        model_response={
                            **bundle.chain.model_response,
                            "model_invoked": True,
                        },
                    ),
                ),
            }
            for case, bad_bundle in cases.items():
                with (
                    self.subTest(case=case),
                    patch(
                        "app.stages.t2t.parameters.execution.load_approved_parameter_chain_v3",
                        return_value=bad_bundle,
                    ),
                ):
                    failed = self._merge(approved, freeze_t2t_parameters(approved))

                failed_output = stage_output(failed, "t2t")
                self.assertEqual(failed_output["status"], "failed")
                if case == "nonempty_l2":
                    self.assertIn(
                        "must not contain L2 mappings", failed_output["reason"]
                    )
                else:
                    self.assertIn(
                        "empty-L2 baseline policy approval", failed_output["reason"]
                    )

    def test_freeze_uses_persisted_catalog_and_persists_all_source_views(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            approved = self._recommend(self._resolve_provided(state))
            approved["stage_outputs"]["t2t"]["thresholds"] = {
                "min_motif_copies": 999,
                "terminal_window_bp": 999,
                "min_mapq": 999,
                "max_target_terminal_distance_bp": 999,
                "min_overlap_bp": 999,
                "min_supporting_reads": 999,
            }
            approved["stage_outputs"]["t2t"]["resolved_telomere_motif"] = "CCCC"
            approved["stage_outputs"]["t2t"]["resolved_telomere_motif_source"] = (
                "llm_species_resolution"
            )
            with patch(
                "app.stages.t2t.parameters.workflow.load_t2t_parameter_catalog",
                side_effect=AssertionError("freeze must use the persisted catalog"),
            ):
                ready = self._merge(approved, freeze_t2t_parameters(approved))
            output = stage_output(ready, "t2t")
            self.assertEqual(
                output["thresholds"],
                {
                    "min_motif_copies": 4,
                    "terminal_window_bp": 10_000,
                    "min_mapq": 20,
                    "max_target_terminal_distance_bp": 1_000,
                    "min_overlap_bp": 500,
                    "min_supporting_reads": 1,
                },
            )
            frozen_ref = output["parameter_attempts"][0]["frozen_parameters_ref"]
            frozen = read_t2t_parameter_artifact(ready, frozen_ref)
            for key in (
                "l0_executor",
                "l1_facts",
                "l2_approved",
                "effective_parameters",
                "effective_source_by_parameter",
                "applications",
            ):
                self.assertIn(key, frozen)
            self.assertEqual(frozen["l2_approved"], {})
            self.assertEqual(frozen["effective_thresholds"], output["thresholds"])
            self.assertEqual(frozen["effective_source_by_parameter"]["min_mapq"], "l0")
            self.assertEqual(
                frozen["effective_source_by_parameter"]["terminal_window_bp"], "l0"
            )
            self.assertEqual(output["resolved_telomere_motif"], "TTAGGG")
            self.assertEqual(
                output["resolved_telomere_motif_source"], "provided_species_fact"
            )

            ready_output = ready["stage_outputs"]["t2t"]
            ready_output["thresholds"] = {name: 999 for name in output["thresholds"]}
            ready_output["resolved_telomere_motif"] = "CCCC"
            ready_output["mode"] = "propose_only"
            ready_output["chromosome_list_ref"] = str(Path(tmpdir) / "changed.txt")
            ready_output["long_read_inputs"] = [
                {
                    "member_id": "changed",
                    "source_asset_id": "changed",
                    "path": str(Path(tmpdir) / "changed.fastq"),
                    "platform": "clr",
                }
            ]
            ready_output["execution_enabled"] = False
            ready_output["work_dir"] = str(
                SessionPaths.from_run_dir(ready["data_dir"]).workflow_step_work_path(
                    "step-001-t2t", "t2t", "frozen-input-test"
                )
            )
            Path(ready_output["work_dir"]).mkdir(parents=True, exist_ok=True)
            ready_output["input_assembly_ref"] = str(Path(tmpdir) / "assembly.fa")
            with patch(
                "app.stages.t2t.nodes._scan_fasta_ends",
                return_value=[],
            ) as scan:
                scan_update = scan_assembly_telomeres(ready)

            self.assertEqual(
                stage_output(self._merge(ready, scan_update), "t2t")["status"],
                "assembly_scanned",
            )
            frozen_inputs = load_t2t_frozen_execution_inputs(ready)
            original = state["stage_outputs"]["t2t"]
            self.assertEqual(frozen_inputs.mode, "auto_patch")
            self.assertEqual(
                frozen_inputs.input_assembly_ref, original["input_assembly_ref"]
            )
            self.assertEqual(
                [
                    item.model_dump(mode="json")
                    for item in frozen_inputs.long_read_inputs
                ],
                original["long_read_inputs"],
            )
            self.assertIsNone(frozen_inputs.chromosome_list_ref)
            self.assertEqual(frozen_inputs.work_dir, original["work_dir"])
            self.assertTrue(frozen_inputs.execution_enabled)
            self.assertEqual(
                scan.call_args.args[0], Path(original["input_assembly_ref"])
            )
            self.assertEqual(scan.call_args.args[1], "TTAGGG")
            self.assertEqual(scan.call_args.args[2]["min_mapq"], 20)
            self.assertEqual(scan.call_args.args[2]["min_motif_copies"], 4)

    def test_freeze_rejects_catalog_default_outside_threshold_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            record = load_t2t_parameter_catalog().model_dump(mode="json")
            record["parameters"]["min_mapq"]["layers"]["l0"]["value"] = 256
            catalog = MethodParameterCatalog.model_validate(record)
            with patch(
                "app.stages.t2t.parameters.workflow.load_t2t_parameter_catalog",
                return_value=catalog,
            ):
                approved = self._recommend(self._resolve_provided(state))
            failed = self._merge(approved, freeze_t2t_parameters(approved))
            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("min_mapq", output["reason"])
            self.assertIn("255", output["reason"])

    def test_freeze_rejects_catalog_missing_required_stage_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            catalog = load_t2t_parameter_catalog()
            incomplete_catalog = catalog.model_copy(
                update={
                    "parameters": {
                        name: spec
                        for name, spec in catalog.parameters.items()
                        if name != "min_mapq"
                    }
                }
            )
            with patch(
                "app.stages.t2t.parameters.workflow.load_t2t_parameter_catalog",
                return_value=incomplete_catalog,
            ):
                approved = self._recommend(self._resolve_provided(state))

            failed = self._merge(approved, freeze_t2t_parameters(approved))

            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("min_mapq", output["reason"])
            self.assertIn("Field required", output["reason"])


if __name__ == "__main__":
    unittest.main()
