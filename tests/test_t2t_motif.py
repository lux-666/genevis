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

from app.control.lifecycle import ResumeValidationError
from app.llm.parameters.t2t.motif import T2TMotifRequest
from app.llm.parameters.t2t.request import T2TParameterRequest
from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request, persist_review_request
from app.schemas.artifacts import (
    T2T_MOTIF_INPUT_SNAPSHOT,
    T2T_MOTIF_RESOLUTION,
    T2T_MOTIF_RESOLUTION_MANIFEST,
    ArtifactMeta,
)
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
    write_artifact as write_t2t_parameter_artifact,
)
from app.stages.t2t.parameters.workflow import (
    apply_t2t_parameter_approval_decision,
    freeze_t2t_parameters,
    recommend_t2t_parameters,
    resolve_t2t_telomere_motif,
)
from app.storage.artifacts import publish_artifact_record
from app.storage.parameter_evidence import load_parameter_recommendation_chain_v3
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
        )
        read_inputs: list[dict[str, object]] = []
        read_refs: list[dict] = []
        paths = SessionPaths.from_run_dir(state["data_dir"])
        assembly_source = paths.work_path("inventory", "assembly.fa")
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
            source = paths.work_path("inventory", f"ont_{index:04d}.fastq")
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
                "chromosome_list_ref": None,
                "long_read_refs": [str(item["path"]) for item in read_inputs],
                "long_read_inputs": read_inputs,
                "input_assembly_ref": str(published_assembly.path),
                "work_dir": str(paths.work_path("t2t", "t2t_finishing")),
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

    def test_model_requests_require_typed_canonical_inputs(self):
        with self.assertRaises(ValueError):
            T2TMotifRequest.model_validate(
                {"facts": {"scientific_name": "Homo sapiens"}}
            )
        with self.assertRaises(ValueError):
            T2TParameterRequest.model_validate(
                {
                    "schema_version": 3,
                    "task": "recommend_initial",
                    "facts": CanonicalSpeciesFacts(scientific_name="Homo sapiens"),
                    "t2t_context": {
                        "stage": "t2t",
                        "resolved_telomere_motif": "TTAGGG",
                        "motif_source": "provided_species_fact",
                        "long_read_asset_count": "1",
                    },
                    "adjustable_parameters": {},
                }
            )

    def _resolve_provided(self, state: dict) -> dict:
        with patch(
            "app.llm.parameters.t2t.motif.build_chat_model",
            side_effect=AssertionError("provided motif must not invoke a model"),
        ):
            update = resolve_t2t_telomere_motif(state)
        return self._merge(state, update)

    def _recommend(self, state: dict, parameters: dict | None = None) -> dict:
        with patch("app.llm.parameters.base.build_chat_model") as build_model:
            build_model.return_value.invoke.return_value = self._response(
                {
                    "parameters": parameters or {},
                    "summary": "Focused test recommendation.",
                }
            )
            update = recommend_t2t_parameters(state)
        return self._merge(state, update)

    def _blocked_parameter_approval(self, tmpdir: str) -> dict:
        state = self._state(
            tmpdir,
            species_facts={
                "scientific_name": "Homo sapiens",
                "telomere_motif": "TTAGGG",
            },
        )
        return self._recommend(self._resolve_provided(state), {"min_mapq": 35})

    @staticmethod
    def _parameter_approval_request(
        blocked: dict,
        *,
        decision: str = "approve_parameters",
    ) -> ResumeReviewRequest:
        interrupt = blocked["stage_outputs"]["t2t"]["active_interrupt"]
        metadata_keys = (
            "attempt_id",
            "task",
            "previous_attempt_id",
            "recommendation_hash",
        )
        return ResumeReviewRequest(
            session_id=blocked["session_id"],
            stage="t2t",
            gate_id="parameter_approval",
            decision=decision,
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            metadata={key: interrupt[key] for key in metadata_keys},
        )

    @staticmethod
    def _replace_parameter_approval_request(
        blocked: dict,
        *,
        payload_updates: dict,
    ) -> None:
        output = blocked["stage_outputs"]["t2t"]
        interrupt = output["active_interrupt"]
        record = load_review_request(
            blocked["data_dir"],
            interrupt["request_ref"],
            session_id=blocked["session_id"],
        )
        payload = {**record["payload"], **payload_updates}
        persisted = persist_review_request(
            blocked,
            stage="t2t",
            gate_id="parameter_approval",
            reason="t2t_parameter_approval_required",
            payload=payload,
            method_name="t2t.parameter_approval",
            artifact_type="t2t.parameter_approval.request",
            bindings=record["bindings"],
        )
        replacement = persisted.compact_interrupt(
            stage="t2t",
            gate_id="parameter_approval",
            reason="t2t_parameter_approval_required",
            bindings=record["bindings"],
        )
        output["active_interrupt"] = replacement
        cursor = dict(output["current_parameter_recommendation"])
        cursor["review_request_id"] = persisted.review_request_id
        cursor["review_request_ref"] = persisted.request_ref.model_dump(mode="json")
        output["current_parameter_recommendation"] = cursor

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
            blocked = self._recommend(resolved, {"min_mapq": 35})
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
            registry_source = paths.work_path("inventory", "asset_registry.json")
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
            resolved["stage_outputs"]["t2t"]["resolved_telomere_motif"] = "CCCC"
            with patch(
                "app.llm.parameters.base.build_chat_model",
                side_effect=AssertionError("changed motif checkpoint must fail first"),
            ):
                failed = self._merge(resolved, recommend_t2t_parameters(resolved))
            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("does not match its evidence chain", output["reason"])

    def test_parameter_approval_validates_request_and_offered_decision_before_write(
        self,
    ):
        cases = (
            "request_id",
            "request_hash",
            "metadata",
            "unsupported_decision",
            "unoffered_rejection",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                blocked = self._blocked_parameter_approval(tmpdir)
                request = self._parameter_approval_request(blocked)
                if case == "request_id":
                    request = request.model_copy(
                        update={"review_request_id": "review_stale"}
                    )
                elif case == "request_hash":
                    request = request.model_copy(
                        update={"review_request_hash": "0" * 64}
                    )
                elif case == "metadata":
                    request = request.model_copy(
                        update={
                            "metadata": {
                                **request.metadata,
                                "recommendation_hash": "0" * 64,
                            }
                        }
                    )
                elif case == "unsupported_decision":
                    request = request.model_copy(update={"decision": "defer"})
                else:
                    self._replace_parameter_approval_request(
                        blocked,
                        payload_updates={"decisions_allowed": ["approve_parameters"]},
                    )
                    request = self._parameter_approval_request(
                        blocked, decision="reject_parameters"
                    )

                with (
                    patch(
                        "app.review.approval.persist_review_decision"
                    ) as persist_decision,
                    self.assertRaises(ResumeValidationError),
                ):
                    apply_t2t_parameter_approval_decision(blocked, request)
                persist_decision.assert_not_called()

    def test_parameter_approval_validates_cursor_and_payload_before_write(self):
        cases = (
            "cursor_status",
            "cursor_review_request",
            "cursor_review_ref",
            "cursor_recommendation_id",
            "cursor_task",
            "cursor_method",
            "payload_manifest_hash",
            "payload_manifest_ref",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                blocked = self._blocked_parameter_approval(tmpdir)
                output = blocked["stage_outputs"]["t2t"]
                cursor = dict(output["current_parameter_recommendation"])
                if case == "cursor_status":
                    cursor["status"] = "approved"
                elif case == "cursor_review_request":
                    cursor["review_request_id"] = "review_stale"
                elif case == "cursor_review_ref":
                    cursor["review_request_ref"] = cursor["manifest_ref"]
                elif case == "cursor_recommendation_id":
                    cursor["recommendation_id"] = 1
                elif case == "cursor_task":
                    cursor["task"] = "retry_after_execution_failure"
                elif case == "cursor_method":
                    cursor["method_id"] = "t2t.changed_method"
                elif case == "payload_manifest_hash":
                    self._replace_parameter_approval_request(
                        blocked,
                        payload_updates={"recommendation_manifest_hash": "0" * 64},
                    )
                else:
                    record = load_review_request(
                        blocked["data_dir"],
                        output["active_interrupt"]["request_ref"],
                        session_id=blocked["session_id"],
                    )
                    other_ref = record["payload"]["recommendation_ref"]
                    self._replace_parameter_approval_request(
                        blocked,
                        payload_updates={
                            "recommendation_manifest_ref": other_ref,
                            "recommendation_manifest_hash": other_ref["sha256"],
                        },
                    )
                if case.startswith("cursor_"):
                    output["current_parameter_recommendation"] = cursor
                request = self._parameter_approval_request(blocked)

                with (
                    patch(
                        "app.review.approval.persist_review_decision"
                    ) as persist_decision,
                    self.assertRaises(ResumeValidationError),
                ):
                    apply_t2t_parameter_approval_decision(blocked, request)
                persist_decision.assert_not_called()

    def test_parameter_approval_validates_manifest_identity_before_write(self):
        cases = {
            "status": {"status": "response_invalid"},
            "recommendation_id": {"recommendation_id": 1},
            "task": {"task": "retry_after_execution_failure"},
            "method": {"method_id": "t2t.changed_method"},
            "predecessor_id": {"previous_attempt_id": 0},
            "predecessor_seal": {"previous_attempt_seal_hash": "0" * 64},
            "predecessor_ref": None,
        }
        for case, manifest_updates in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                blocked = self._blocked_parameter_approval(tmpdir)
                cursor = blocked["stage_outputs"]["t2t"][
                    "current_parameter_recommendation"
                ]
                chain = load_parameter_recommendation_chain_v3(
                    blocked,
                    manifest_ref=cursor["manifest_ref"],
                    expected_stage="t2t",
                    expected_method_id=cursor["method_id"],
                )
                if manifest_updates is None:
                    manifest_updates = {
                        "previous_attempt_manifest_ref": (
                            chain.manifest.refs.recommendation_ref
                        )
                    }
                changed_chain = replace(
                    chain,
                    manifest=chain.manifest.model_copy(update=manifest_updates),
                )
                request = self._parameter_approval_request(blocked)

                with (
                    patch(
                        "app.stages.t2t.parameters.workflow."
                        "load_parameter_recommendation_chain_v3",
                        return_value=changed_chain,
                    ),
                    patch(
                        "app.review.approval.persist_review_decision"
                    ) as persist_decision,
                    self.assertRaises(ResumeValidationError),
                ):
                    apply_t2t_parameter_approval_decision(blocked, request)
                persist_decision.assert_not_called()

    def test_parameter_approval_requires_initial_empty_attempt_head_before_write(self):
        cases = ("existing_attempt", "current_head", "proposed_head")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                blocked = self._blocked_parameter_approval(tmpdir)
                output = blocked["stage_outputs"]["t2t"]
                if case == "existing_attempt":
                    output["parameter_attempts"] = [{"attempt_id": 0}]
                elif case == "current_head":
                    output["current_parameter_attempt_id"] = 1
                else:
                    output["proposed_parameter_attempt_id"] = 1
                request = self._parameter_approval_request(blocked)

                with (
                    patch(
                        "app.review.approval.persist_review_decision"
                    ) as persist_decision,
                    self.assertRaises(ResumeValidationError),
                ):
                    apply_t2t_parameter_approval_decision(blocked, request)
                persist_decision.assert_not_called()

    def test_parameter_approval_derives_attempt_identity_from_validated_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked = self._blocked_parameter_approval(tmpdir)
            cursor = blocked["stage_outputs"]["t2t"]["current_parameter_recommendation"]
            chain = load_parameter_recommendation_chain_v3(
                blocked,
                manifest_ref=cursor["manifest_ref"],
                expected_stage="t2t",
                expected_method_id=cursor["method_id"],
            )

            approved = apply_t2t_parameter_approval_decision(
                blocked,
                self._parameter_approval_request(blocked),
            )

            attempt = approved["stage_outputs"]["t2t"]["parameter_attempts"][0]
            manifest = chain.manifest
            self.assertEqual(attempt["attempt_id"], manifest.recommendation_id)
            self.assertEqual(attempt["method_id"], manifest.method_id)
            self.assertEqual(attempt["task"], manifest.task)
            self.assertEqual(
                attempt["previous_attempt_id"], manifest.previous_attempt_id
            )
            self.assertEqual(
                attempt["previous_attempt_seal_hash"],
                manifest.previous_attempt_seal_hash,
            )
            self.assertEqual(
                attempt["recommendation_contract"],
                manifest.recommendation_contract,
            )
            self.assertEqual(
                attempt["recommendation_manifest_ref"],
                chain.manifest_ref.model_dump(mode="json"),
            )

            approved_state = self._merge(blocked, approved)
            with self.assertRaises(ResumeValidationError):
                apply_t2t_parameter_approval_decision(
                    approved_state,
                    self._parameter_approval_request(blocked),
                )

    def test_parameter_rejection_is_fully_bound_and_does_not_create_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked = self._blocked_parameter_approval(tmpdir)

            rejected = apply_t2t_parameter_approval_decision(
                blocked,
                self._parameter_approval_request(blocked, decision="reject_parameters"),
            )

            output = rejected["stage_outputs"]["t2t"]
            self.assertEqual(rejected["supervision_state"], "failed_terminal")
            self.assertEqual(
                output["current_parameter_recommendation"]["status"],
                "rejected",
            )
            self.assertEqual(output["parameter_attempts"], [])
            self.assertIsNone(output["proposed_parameter_attempt_id"])

        with tempfile.TemporaryDirectory() as tmpdir:
            blocked = self._blocked_parameter_approval(tmpdir)
            self._replace_parameter_approval_request(
                blocked,
                payload_updates={"recommendation_manifest_hash": "0" * 64},
            )
            request = self._parameter_approval_request(
                blocked, decision="reject_parameters"
            )
            with (
                patch(
                    "app.review.approval.persist_review_decision"
                ) as persist_decision,
                self.assertRaises(ResumeValidationError),
            ):
                apply_t2t_parameter_approval_decision(blocked, request)
            persist_decision.assert_not_called()

    def test_freeze_uses_persisted_catalog_and_persists_all_source_views(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                species_facts={
                    "scientific_name": "Homo sapiens",
                    "telomere_motif": "TTAGGG",
                },
            )
            blocked = self._recommend(self._resolve_provided(state), {"min_mapq": 35})
            interrupt = stage_output(blocked, "t2t")["active_interrupt"]
            metadata_keys = (
                "attempt_id",
                "task",
                "previous_attempt_id",
                "recommendation_hash",
            )
            approval = apply_t2t_parameter_approval_decision(
                blocked,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="t2t",
                    gate_id="parameter_approval",
                    decision="approve_parameters",
                    review_request_id=interrupt["review_request_id"],
                    review_request_hash=interrupt["request_hash"],
                    metadata={key: interrupt[key] for key in metadata_keys},
                ),
            )
            approved = self._merge(blocked, approval)
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
                    "min_mapq": 35,
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
            self.assertEqual(frozen["l2_approved"], {"min_mapq": 35})
            self.assertEqual(frozen["effective_thresholds"], output["thresholds"])
            self.assertEqual(frozen["effective_source_by_parameter"]["min_mapq"], "l2")
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
                SessionPaths.from_run_dir(ready["data_dir"]).work_path(
                    "t2t", "frozen-input-test"
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
            self.assertEqual(scan.call_args.args[2]["min_mapq"], 35)
            self.assertEqual(scan.call_args.args[2]["min_motif_copies"], 4)

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
                blocked = self._recommend(self._resolve_provided(state))
            interrupt = stage_output(blocked, "t2t")["active_interrupt"]
            metadata_keys = (
                "attempt_id",
                "task",
                "previous_attempt_id",
                "recommendation_hash",
            )
            approval = apply_t2t_parameter_approval_decision(
                blocked,
                ResumeReviewRequest(
                    session_id="s1",
                    stage="t2t",
                    gate_id="parameter_approval",
                    decision="approve_parameters",
                    review_request_id=interrupt["review_request_id"],
                    review_request_hash=interrupt["request_hash"],
                    metadata={key: interrupt[key] for key in metadata_keys},
                ),
            )
            approved = self._merge(blocked, approval)

            failed = self._merge(approved, freeze_t2t_parameters(approved))

            output = stage_output(failed, "t2t")
            self.assertEqual(output["status"], "failed")
            self.assertIn("min_mapq", output["reason"])
            self.assertIn("Field required", output["reason"])


if __name__ == "__main__":
    unittest.main()
