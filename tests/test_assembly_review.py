"""Tests for the assembly review stage.

Assembly review is the main human gate.  These tests verify that:
- Candidate disagreements do not auto-advance without policy.
- Rejected candidates remain traceable.
- Existing assembly inputs route through deterministically.
- Missing assemblies fail terminally.
- Human gates set blocked_human and active_interrupt.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.execution.evidence import load_frozen_attempt
from app.integrity import sha256_file
from app.review.persistence import load_review_request
from app.schemas.artifacts import (
    ASSEMBLY_CANDIDATE_BUNDLE,
    ASSEMBLY_DRAFT_ASSEMBLY,
    ASSEMBLY_REVIEW_PACKET,
    HIC_SCAFFOLDED_ASSEMBLY,
    T2T_PATCHED_ASSEMBLY,
    ArtifactMeta,
    AssemblyCandidateBundle,
    ReviewDecisionArtifact,
)
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.resource_ref import ResourceRef
from app.schemas.state import initial_state
from app.stages.assembly_review.builder import build_stage
from app.stages.assembly_review.method_application.busco import (
    build_busco_graph,
    freeze_busco_attempts,
)
from app.stages.assembly_review.method_application.quast import (
    build_quast_graph,
    freeze_quast_attempts,
    quast_method_outcome,
)
from app.stages.assembly_review.nodes import (
    _assembly_review_accept_note,
    _phase10_review_metrics,
    resolve_candidates,
)
from app.storage.artifacts import publish_artifact_record
from app.storage.assembly_candidates import (
    publish_derived_assembly_candidate,
    publish_registered_assembly_candidate,
)
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.busco_fixture import ensure_busco_lineage, isolated_frozen_busco_resolver
from tests.data_v2 import v2_run_dir


def _request_record(result: dict) -> dict:
    return load_review_request(
        result["data_dir"], result["active_interrupt"]["request_ref"]
    )


def _request_payload(result: dict) -> dict:
    return _request_record(result)["payload"]


def _review_occurrence_state(methods: list[str]) -> dict:
    return {
        "workflow_steps": [
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
            {
                "step_id": "step-001-assembly",
                "stage": "assembly",
                "allow_local_execution": True,
            },
            {
                "step_id": "step-002-assembly_review",
                "stage": "assembly_review",
                "allow_local_execution": True,
                "selected_method_names": methods,
                "review_target": {
                    "resource_type": "assembly",
                    "producer_step_id": "step-001-assembly",
                },
            },
        ],
        "workflow_step_index": 2,
        "current_stage": "assembly_review",
    }


class AssemblyReviewDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_assembly_review_methods(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        expected = {
            "assembly_review.eval.quast": "quast",
            "assembly_review.eval.busco": "busco",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "assembly_review")
            self.assertIn(method_name, registry.by_stage["assembly_review"])


class AssemblyReviewStageTests(unittest.TestCase):
    def test_phase10_metrics_use_canonical_quast_contig_count(self):
        metrics = _phase10_review_metrics(
            {
                "candidates": [{"candidate_id": "candidate-1"}],
                "signals": [
                    {
                        "candidate_id": "candidate-1",
                        "signal_name": "quast",
                        "status": "available",
                        "metadata": {
                            "metrics": {
                                "contig_count": 731,
                                "n50": 250_000,
                            }
                        },
                    }
                ],
            },
            {},
        )

        self.assertEqual(metrics["contig_count"], 731)

    def test_review_accept_note_reflects_frozen_next_stage_or_completion(self):
        state = self._build_state(entry_mode="raw_inputs")

        self.assertEqual(
            _assembly_review_accept_note(state),
            "Accept continues with the selected assembly to the frozen next stage: "
            "hic_scaffolding.",
        )

        state["workflow_steps"] = state["workflow_steps"][:3]
        self.assertEqual(
            _assembly_review_accept_note(state),
            "Accept completes the frozen workflow with the selected assembly.",
        )

    def test_outer_graph_routes_to_metric_subgraphs(self):
        outer_nodes = set(build_stage().get_graph().nodes)
        quast_nodes = set(build_quast_graph().get_graph().nodes)
        busco_nodes = set(build_busco_graph().get_graph().nodes)

        self.assertIn("quast", outer_nodes)
        self.assertIn("busco", outer_nodes)
        self.assertIn("related_species_sanity_check", outer_nodes)
        self.assertNotIn("freeze_quast", outer_nodes)
        self.assertNotIn("run_busco", outer_nodes)
        for nodes in [quast_nodes, busco_nodes]:
            self.assertIn("freeze", nodes)
            self.assertIn("run", nodes)
            self.assertIn("collect", nodes)

    def test_metric_method_modules_do_not_depend_on_parent_stage_modules(self):
        for module_path in [
            Path("app/stages/assembly_review/method_application/quast.py"),
            Path("app/stages/assembly_review/method_application/busco.py"),
        ]:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            imports = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ]
            self.assertNotIn("app.stages.assembly_review.nodes", imports)
            self.assertNotIn("app.stages.assembly_review.routing", imports)

    def test_metric_freeze_callbacks_only_prepare_evaluating_state(self):
        state = {
            "session_id": "s1",
            "stage_outputs": {
                "assembly_review": {
                    "status": "evaluated",
                    "execution_mode": "local",
                }
            },
        }

        self.assertEqual(freeze_quast_attempts(state), {})
        self.assertEqual(freeze_busco_attempts(state), {})

    def test_quast_outcome_requires_every_candidate_and_survives_warnings(self):
        state = {
            **_review_occurrence_state(["assembly_review.eval.quast"]),
            "stage_outputs": {
                "assembly_review": {
                    "status": "completed_with_warnings",
                    "review_plan": {"planned_signals": ["quast"]},
                    "candidates": [
                        {
                            "candidate_id": "flye_01",
                            "method_name": "assembly.candidate.flye",
                            "source": "test",
                            "status": "executed",
                            "bundle_ref": {
                                "schema_version": 2,
                                "artifact_id": "bundle-flye",
                                "path_ref": "artifacts/flye.bundle.json",
                                "sha256": "0" * 64,
                                "size_bytes": 0,
                                "artifact_type": ASSEMBLY_CANDIDATE_BUNDLE,
                            },
                            "primary_output_ref": {
                                "schema_version": 2,
                                "artifact_id": "assembly-flye",
                                "path_ref": "artifacts/flye.fasta",
                                "sha256": "1" * 64,
                                "size_bytes": 0,
                                "artifact_type": ASSEMBLY_DRAFT_ASSEMBLY,
                            },
                        },
                        {
                            "candidate_id": "hifiasm_01",
                            "method_name": "assembly.candidate.hifiasm",
                            "source": "test",
                            "status": "executed",
                            "bundle_ref": {
                                "schema_version": 2,
                                "artifact_id": "bundle-hifiasm",
                                "path_ref": "artifacts/hifiasm.bundle.json",
                                "sha256": "2" * 64,
                                "size_bytes": 0,
                                "artifact_type": ASSEMBLY_CANDIDATE_BUNDLE,
                            },
                            "primary_output_ref": {
                                "schema_version": 2,
                                "artifact_id": "assembly-hifiasm",
                                "path_ref": "artifacts/hifiasm.fasta",
                                "sha256": "3" * 64,
                                "size_bytes": 0,
                                "artifact_type": ASSEMBLY_DRAFT_ASSEMBLY,
                            },
                        },
                    ],
                    "signals": [
                        {
                            "signal_name": "quast",
                            "candidate_id": "flye_01",
                            "status": "available",
                        },
                        {
                            "signal_name": "quast",
                            "candidate_id": "hifiasm_01",
                            "status": "available",
                        },
                    ],
                }
            },
        }
        outcome = quast_method_outcome(state)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.disposition, "completed")

        # A subsequent stage-level failure cannot revise a fully settled QUAST
        # result.  The outcome is derived from the evaluator's durable signals.
        state["stage_outputs"]["assembly_review"]["status"] = "failed"
        settled_before_later_failure = quast_method_outcome(state)
        self.assertIsNotNone(settled_before_later_failure)
        self.assertEqual(settled_before_later_failure.disposition, "completed")

        # A failed preflight has no execution cursor and therefore remains
        # not_started, even if the enclosing review output is failed.
        state["stage_outputs"]["assembly_review"]["signals"] = [
            {
                "signal_name": "quast",
                "candidate_id": candidate_id,
                "status": "missing",
                "notes": [
                    "quast_environment_preflight_failed",
                    "execution_not_started",
                ],
            }
            for candidate_id in ["flye_01", "hifiasm_01"]
        ]
        state["stage_outputs"]["assembly_review"]["execution_attempts"] = []
        preflight = quast_method_outcome(state)
        self.assertIsNotNone(preflight)
        self.assertEqual(preflight.disposition, "not_started")
        self.assertEqual(preflight.reason, "quast_environment_preflight_failed")

        # Only the matching evaluator's durable execution cursor permits a
        # failed disposition.
        state["stage_outputs"]["assembly_review"]["signals"][0]["status"] = "failed"
        state["stage_outputs"]["assembly_review"]["signals"][0]["notes"] = [
            "quast_execution_failed"
        ]
        state["stage_outputs"]["assembly_review"]["execution_attempts"] = [
            {
                "attempt_id": "s1:assembly_review:quast:flye_01",
                "method_id": "assembly_review.eval.quast",
                "status": "execution_failed",
            }
        ]
        execution = quast_method_outcome(state)
        self.assertIsNotNone(execution)
        self.assertEqual(execution.disposition, "failed")
        self.assertEqual(execution.reason, "quast_candidate_evaluation_failed")

    def test_review_plan_cannot_select_an_evaluator_outside_the_roster(self):
        state = {
            **_review_occurrence_state([]),
            "stage_outputs": {
                "assembly_review": {
                    "review_plan": {"planned_signals": ["quast"]},
                }
            },
        }
        self.assertIsNone(quast_method_outcome(state))
        state["workflow_steps"][2]["selected_method_names"] = [
            "assembly_review.eval.quast"
        ]
        state["stage_outputs"]["assembly_review"]["review_plan"] = {}
        self.assertIsNotNone(quast_method_outcome(state))

    def test_unselected_evaluators_do_not_freeze_or_probe_environments(self):
        state = {
            **_review_occurrence_state([]),
            "stage_outputs": {
                "assembly_review": {
                    "execution_mode": "local",
                    "review_plan": {
                        "status": "evaluating",
                        "planned_signals": ["quast", "busco"],
                    },
                }
            },
        }
        for module, freeze in (
            ("quast", freeze_quast_attempts),
            ("busco", freeze_busco_attempts),
        ):
            with patch(
                f"app.stages.assembly_review.method_application.{module}.review_methods_error",
                side_effect=AssertionError("unselected evaluator probed tools"),
            ):
                self.assertEqual(freeze(state), {})

    def test_missing_frozen_review_roster_is_rejected(self):
        state = {
            **_review_occurrence_state([]),
            "stage_outputs": {"assembly_review": {}},
        }
        state["workflow_steps"][2].pop("selected_method_names")
        with self.assertRaisesRegex(ValueError, "selected_method_names is required"):
            quast_method_outcome(state)

    def test_repeated_review_uses_only_the_active_occurrence_roster(self):
        state = {
            **_review_occurrence_state(["assembly_review.eval.quast"]),
            "stage_outputs": {"assembly_review": {}},
        }
        state["workflow_steps"].append(
            {
                **state["workflow_steps"][2],
                "step_id": "step-003-assembly_review",
                "selected_method_names": ["assembly_review.eval.busco"],
                "review_target": {
                    "resource_type": "assembly",
                    "producer_step_id": "step-002-assembly_review",
                },
            }
        )
        self.assertIsNotNone(quast_method_outcome(state))
        state["workflow_step_index"] = 3
        self.assertIsNone(quast_method_outcome(state))

    def test_resolves_t2t_and_hic_derived_current_candidates_without_fallback(self):
        producer_cases = (
            ("t2t", T2T_PATCHED_ASSEMBLY, "t2t.contract"),
            (
                "hic_scaffolding",
                HIC_SCAFFOLDED_ASSEMBLY,
                "hic_scaffolding.contract",
            ),
        )
        producer_method_names = {
            "t2t": "t2t.alignment.minimap2",
            "hic_scaffolding": "hic_scaffolding.candidate.autohic",
        }
        for producer_stage, primary_type, method_name in producer_cases:
            with (
                self.subTest(producer_stage=producer_stage),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                session_id = "s1"
                producer_step_id = f"step-001-{producer_stage}"
                data_dir = v2_run_dir(tmpdir, session_id)
                state = initial_state(
                    session_id=session_id,
                    input_manifest_ref="control/manifest.yaml",
                    data_dir=str(data_dir),
                    workflow_steps=[
                        {
                            "step_id": "step-000-inventory",
                            "stage": "inventory",
                            "allow_local_execution": False,
                            "selected_method_names": [],
                        },
                        {
                            "step_id": producer_step_id,
                            "stage": producer_stage,
                            "allow_local_execution": True,
                            "selected_method_names": [
                                producer_method_names[producer_stage]
                            ],
                        },
                        {
                            "step_id": "step-002-assembly_review",
                            "stage": "assembly_review",
                            "allow_local_execution": True,
                            "selected_method_names": [
                                "assembly_review.eval.quast",
                                "assembly_review.eval.busco",
                            ],
                            "review_target": {
                                "resource_type": "assembly",
                                "producer_step_id": producer_step_id,
                            },
                        },
                    ],
                )
                state["workflow_step_index"] = 2
                state["entered_workflow_step_index"] = 2
                state["current_stage"] = "assembly_review"
                state["stage_outputs"] = {"assembly_review": {}}

                paths = SessionPaths.from_run_dir(data_dir)
                registry_source = paths.workflow_step_work_path(
                    "step-000-inventory", "inventory", "asset_registry.json"
                )
                registry_source.parent.mkdir(parents=True, exist_ok=True)
                registry_source.write_text(
                    json.dumps({"session_id": session_id, "assets": []}),
                    encoding="utf-8",
                )
                registry_ref = publish_artifact_record(
                    data_dir,
                    session_id,
                    ArtifactMeta.from_file(
                        registry_source,
                        stage="inventory",
                        method_name="inventory.register_assets",
                        artifact_type="asset_registry",
                    ),
                ).ref
                source_assembly = Path(tmpdir) / "source.fasta"
                source_assembly.write_text(">source\nACGT\n", encoding="utf-8")
                source_candidate = publish_registered_assembly_candidate(
                    data_dir,
                    session_id,
                    workflow_step_id="step-000-inventory",
                    asset_id="source-assembly",
                    source_path=source_assembly,
                    expected_sha256=sha256_file(source_assembly),
                    expected_size_bytes=source_assembly.stat().st_size,
                    asset_registry_ref=registry_ref,
                )

                derived_source = paths.workflow_step_work_path(
                    producer_step_id,
                    producer_stage,
                    "derived_assembly.fasta",
                )
                derived_source.parent.mkdir(parents=True, exist_ok=True)
                derived_source.write_text(">derived\nACGTAC\n", encoding="utf-8")
                primary_ref = publish_artifact_record(
                    data_dir,
                    session_id,
                    ArtifactMeta.from_file(
                        derived_source,
                        stage=producer_stage,
                        method_name=method_name,
                        artifact_type=primary_type,
                    ),
                ).ref
                derived_candidate = publish_derived_assembly_candidate(
                    data_dir,
                    session_id,
                    workflow_step_id=producer_step_id,
                    source_candidate=source_candidate,
                    primary_output_ref=primary_ref,
                    stage=producer_stage,
                    method_name=method_name,
                    action_identity=f"decision:{producer_stage}",
                )
                state["resource_refs"]["assembly"] = ResourceRef(
                    resource_type="assembly",
                    paths=[str(derived_candidate.primary_output_path)],
                    source_stage=producer_stage,
                    workflow_step_id=producer_step_id,
                    metadata={
                        "candidate_bundle_ref": derived_candidate.ref.model_dump(
                            mode="json"
                        ),
                        "primary_output_ref": (
                            derived_candidate.primary_output_ref.model_dump(mode="json")
                        ),
                        "input_candidate_bundle_ref": source_candidate.ref.model_dump(
                            mode="json"
                        ),
                        "input_primary_output_ref": (
                            source_candidate.primary_output_ref.model_dump(mode="json")
                        ),
                    },
                ).to_dict()
                ledger = Ledger(data_dir)
                ledger.create_session(session_id, "control/manifest.yaml")
                ledger.record_stage_completed(
                    session_id,
                    "inventory",
                    workflow_step_id="step-000-inventory",
                )
                ledger.record_resource_delivered(
                    session_id,
                    stage=producer_stage,
                    workflow_step_id=producer_step_id,
                    resource_type="assembly",
                    delivery_mode="materialized",
                    candidate_bundle_ref=derived_candidate.ref.model_dump(mode="json"),
                    primary_output_ref=derived_candidate.primary_output_ref.model_dump(
                        mode="json"
                    ),
                )
                ledger.record_stage_completed(
                    session_id,
                    producer_stage,
                    workflow_step_id=producer_step_id,
                )

                update = resolve_candidates(state)
                review = update["stage_outputs"]["assembly_review"]

                self.assertEqual(review["status"], "resolved")
                self.assertEqual(review["candidate_count"], 1)
                self.assertEqual(len(review["candidates"]), 1)
                candidate = review["candidates"][0]
                self.assertEqual(candidate["source"], producer_stage)
                self.assertEqual(
                    candidate["bundle_ref"],
                    derived_candidate.ref.model_dump(mode="json"),
                )
                self.assertEqual(
                    candidate["primary_output_ref"],
                    derived_candidate.primary_output_ref.model_dump(mode="json"),
                )
                self.assertEqual(
                    Path(state["resource_refs"]["assembly"]["paths"][0]),
                    derived_candidate.primary_output_path,
                )
                self.assertNotIn(producer_stage, state["stage_outputs"])

    def test_rejects_current_assembly_from_another_producer_occurrence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGT\n", encoding="utf-8")
            state = self._build_state(data_dir=tmpdir)
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="s1:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            self._set_candidate_refs(state, [str(candidate_ref)])
            current = state["resource_refs"]["assembly"]
            state["resource_refs"]["assembly"] = ResourceRef(
                resource_type="assembly",
                paths=list(current["paths"]),
                source_stage="inventory",
                workflow_step_id="step-000-inventory",
                metadata=dict(current["metadata"]),
            ).to_dict()

            update = resolve_candidates(state)

            review = update["stage_outputs"]["assembly_review"]
            self.assertEqual(review["status"], "failed")
            self.assertIn("producer step does not match", review["reason"])
            self.assertIn("step-001-assembly", review["reason"])
            self.assertEqual(review["candidate_count"], 0)

    def test_metric_graphs_resume_ran_checkpoint_at_collect(self):
        for module_name, build_graph, ran_status in [
            (
                "app.stages.assembly_review.method_application.quast",
                build_quast_graph,
                "quast_ran",
            ),
            (
                "app.stages.assembly_review.method_application.busco",
                build_busco_graph,
                "busco_ran",
            ),
        ]:
            with self.subTest(module=module_name):
                state = {
                    # This route-only fixture settles no evaluator disposition.
                    **_review_occurrence_state([]),
                    "session_id": "s1",
                    "stage_outputs": {"assembly_review": {"status": ran_status}},
                }
                with (
                    patch(
                        f"{module_name}.freeze_"
                        f"{module_name.rsplit('.', 1)[-1]}_attempts",
                        side_effect=AssertionError("ran checkpoint must bypass freeze"),
                    ) as freeze,
                    patch(
                        f"{module_name}.collect_"
                        f"{module_name.rsplit('.', 1)[-1]}_artifacts",
                        return_value={},
                    ) as collect,
                ):
                    build_graph().invoke(state)

                freeze.assert_not_called()
                collect.assert_called_once()

    def _publish_registry_ref(
        self, data_dir: str, session_id: str, registry_path: str | Path
    ) -> dict:
        paths = SessionPaths.from_run_dir(data_dir)
        source = paths.workflow_step_work_path(
            "step-000-inventory", "inventory", "asset_registry.json"
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            Path(str(registry_path)).read_text(encoding="utf-8"), encoding="utf-8"
        )
        artifact = ArtifactMeta.from_file(
            source,
            stage="inventory",
            method_name="inventory.register_assets",
            artifact_type="asset_registry",
        )
        published = publish_artifact_record(data_dir, session_id, artifact)
        return published.ref.model_dump(mode="json")

    def _build_state(
        self,
        *,
        session_id: str = "test-session",
        entry_mode: str | None = None,
        candidate_bundles: list[dict] | None = None,
        registry_ref: str | None = None,
        data_dir: str | None = None,
    ) -> dict:
        stage_outputs = {
            "inventory": {
                "status": "completed",
                "entry_mode": entry_mode,
            },
        }
        if candidate_bundles:
            stage_outputs["assembly"] = {
                "status": "completed",
                "candidate_bundles": candidate_bundles,
            }
        data_root = (
            Path(data_dir)
            if data_dir is not None
            else Path(tempfile.mkdtemp(prefix="gene-agent-assembly-review-"))
        )
        if data_dir is None:
            self.addCleanup(shutil.rmtree, data_root, True)
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(v2_run_dir(data_root, session_id)),
            workflow_steps=[
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                    "selected_method_names": [],
                },
                {
                    "step_id": "step-001-assembly",
                    "stage": "assembly",
                    "allow_local_execution": True,
                },
                {
                    "step_id": "step-002-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": True,
                    "selected_method_names": [
                        "assembly_review.eval.quast",
                        "assembly_review.eval.busco",
                    ],
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-001-assembly",
                    },
                },
                {
                    "step_id": "step-003-hic_scaffolding",
                    "stage": "hic_scaffolding",
                    "allow_local_execution": True,
                    "selected_method_names": [
                        "hic_scaffolding.candidate.autohic",
                    ],
                },
            ],
        )
        state["workflow_step_index"] = 2
        state["entered_workflow_step_index"] = 2
        state["current_stage"] = "assembly_review"
        stage_outputs["assembly_review"] = {
            "related_species_reference_dir": str(self._test_reference_dir(state))
        }
        state["stage_outputs"] = stage_outputs
        reference_patch = patch(
            "app.stages.assembly_review.statistics._related_species_reference_dir",
            side_effect=lambda _out, *, state: self._test_reference_dir(state),
        )
        reference_patch.start()
        self.addCleanup(reference_patch.stop)
        busco_patch = patch(
            "app.stages.assembly_review.method_application.busco."
            "load_frozen_dependency_resolver",
            side_effect=lambda _dir: isolated_frozen_busco_resolver(
                state["stage_outputs"]["assembly_review"]["busco_parameters"]
            ),
        )
        busco_patch.start()
        self.addCleanup(busco_patch.stop)
        if candidate_bundles:
            candidate_refs = [
                self._write_candidate_bundle(
                    str(data_root),
                    candidate_id=str(bundle.get("candidate_id", f"candidate-{index}")),
                    method_name=str(
                        bundle.get("method_name", "assembly.candidate.flye")
                    ),
                    draft_assembly_fasta=(bundle.get("expected_outputs") or {}).get(
                        "draft_assembly_fasta"
                    ),
                )
                for index, bundle in enumerate(candidate_bundles)
            ]
            self._set_candidate_refs(state, [str(ref) for ref in candidate_refs])
        if registry_ref:
            state["asset_registry_ref"] = self._publish_registry_ref(
                state["data_dir"], session_id, registry_ref
            )
        else:
            empty_registry = SessionPaths.from_run_dir(
                state["data_dir"]
            ).workflow_step_work_path(
                "step-000-inventory", "inventory", "empty_asset_registry.json"
            )
            empty_registry.parent.mkdir(parents=True, exist_ok=True)
            empty_registry.write_text(
                json.dumps({"session_id": session_id, "assets": []}),
                encoding="utf-8",
            )
            state["asset_registry_ref"] = publish_artifact_record(
                state["data_dir"],
                session_id,
                ArtifactMeta.from_file(
                    empty_registry,
                    stage="inventory",
                    method_name="inventory.register_assets",
                    artifact_type="asset_registry",
                ),
            ).ref.model_dump(mode="json")
        if entry_mode == "existing_assembly" and registry_ref:
            assets = json.loads(Path(registry_ref).read_text(encoding="utf-8")).get(
                "assets", []
            )
            if assets:
                bundle = self._write_candidate_bundle(
                    str(data_root),
                    candidate_id="existing_assembly",
                    method_name="inventory.registered_assembly",
                    draft_assembly_fasta=str(assets[0]["path"]),
                )
                self._set_candidate_refs(state, [str(bundle)])
        return state

    @staticmethod
    def _test_reference_dir(state: dict) -> Path:
        """Provide an empty valid snapshot for isolated stage seams."""
        root = Path(state["data_dir"]) / "isolated_ncbi_metazoa"
        root.mkdir(parents=True, exist_ok=True)
        files = {
            "Metazoa.ncbi.json": "{}\n",
            "Metazoa.ncbi.lineage": "Organism Taxonomic ID\n",
            "Metazoa.ncbi.lineage2": "Organism Taxonomic ID\n",
            "Metazoa.ncbi.tsv": "Organism Name\tOrganism Taxonomic ID\n",
        }
        for name, contents in files.items():
            (root / name).write_text(contents, encoding="utf-8")
        return root

    def _write_candidate_bundle(
        self,
        tmpdir: str,
        *,
        candidate_id: str,
        method_name: str,
        draft_assembly_fasta: str | None = None,
    ) -> Path:
        path = Path(tmpdir) / f"{candidate_id}.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": "test-session",
                    "candidate_id": candidate_id,
                    "method_name": method_name,
                    "status": "planned",
                    "artifact_type": "draft_assembly.candidate",
                    "input_ref": str(Path(tmpdir) / "long_reads.fofn"),
                    "input_type": "long_reads.fofn",
                    "expected_outputs": {
                        "draft_assembly_fasta": draft_assembly_fasta
                        or str(Path(tmpdir) / f"{candidate_id}.fasta")
                    },
                    "logs": {"primary_log": str(Path(tmpdir) / f"{candidate_id}.log")},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _set_candidate_refs(state: dict, candidate_refs: list[str]) -> None:
        """Publish legacy fixture candidates through the executed-bundle contract."""
        data_dir = state["data_dir"]
        session_id = state["session_id"]
        paths = SessionPaths.from_run_dir(data_dir)
        published_candidates = []
        for candidate_ref in candidate_refs:
            payload = json.loads(Path(candidate_ref).read_text(encoding="utf-8"))
            draft = (payload.get("expected_outputs") or {}).get("draft_assembly_fasta")
            if not draft:
                raise ValueError("fixture candidate is missing draft_assembly_fasta")
            draft_path = Path(str(draft))
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            if not draft_path.exists():
                draft_path.write_text(">fixture\nACGT\n", encoding="utf-8")
            candidate_id = str(payload["candidate_id"])
            method_name = str(payload["method_name"])
            planned = AssemblyCandidateBundle(
                session_id=session_id,
                candidate_id=candidate_id,
                method_name=method_name,
                status="planned",
                input_type="long_reads.fofn",
                input_member_ids={"primary": ["fixture-long-reads"]},
                input_member_platforms={"primary": ["ont"]},
                parameter_attempt_id=0,
                expected_outputs=["primary_assembly"],
            )
            planned_source = paths.workflow_step_work_path(
                "step-001-assembly",
                "assembly",
                "fixture_candidates",
                f"{candidate_id}.planned.json",
            )
            planned_source.parent.mkdir(parents=True, exist_ok=True)
            planned_source.write_text(planned.model_dump_json(), encoding="utf-8")
            planned_ref = publish_artifact_record(
                data_dir,
                session_id,
                ArtifactMeta.from_file(
                    planned_source,
                    stage="assembly",
                    method_name=method_name,
                    artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
                ),
            ).ref
            primary_source = paths.workflow_step_work_path(
                "step-001-assembly",
                "assembly",
                "fixture_candidates",
                f"{candidate_id}.fasta",
            )
            primary_source.write_bytes(draft_path.read_bytes())
            primary_ref = publish_artifact_record(
                data_dir,
                session_id,
                ArtifactMeta.from_file(
                    primary_source,
                    stage="assembly",
                    method_name=method_name,
                    artifact_type=ASSEMBLY_DRAFT_ASSEMBLY,
                ),
            ).ref
            executed = planned.model_copy(
                update={
                    "status": "executed",
                    "plan_ref": planned_ref,
                    "primary_output": primary_ref,
                }
            )
            executed_source = paths.workflow_step_work_path(
                "step-001-assembly",
                "assembly",
                "fixture_candidates",
                f"{candidate_id}.executed.json",
            )
            executed_source.write_text(executed.model_dump_json(), encoding="utf-8")
            executed_ref = publish_artifact_record(
                data_dir,
                session_id,
                ArtifactMeta.from_file(
                    executed_source,
                    stage="assembly",
                    method_name=method_name,
                    artifact_type=ASSEMBLY_CANDIDATE_BUNDLE,
                ),
            ).ref
            published_candidates.append((executed_ref, primary_ref))

        if not published_candidates:
            return
        current_ref, current_primary = published_candidates[0]
        metadata = {
            "candidate_bundle_ref": current_ref.model_dump(mode="json"),
            "primary_output_ref": current_primary.model_dump(mode="json"),
            "rejected_candidates": dict(
                (state.get("stage_outputs") or {})
                .get("assembly", {})
                .get("rejected_candidates", {})
            ),
        }
        if len(published_candidates) > 1:
            raw_ref, raw_primary = published_candidates[1]
            metadata["raw_candidate_bundle_ref"] = raw_ref.model_dump(mode="json")
            metadata["raw_primary_output_ref"] = raw_primary.model_dump(mode="json")
        state["resource_refs"]["assembly"] = ResourceRef(
            resource_type="assembly",
            paths=[str(paths.resolve_ref(current_primary.path_ref))],
            source_stage="assembly",
            workflow_step_id="step-001-assembly",
            metadata=metadata,
        ).to_dict()
        ledger = Ledger(data_dir)
        ledger.create_session(session_id, "control/manifest.yaml")
        ledger.record_stage_completed(
            session_id,
            "inventory",
            workflow_step_id="step-000-inventory",
        )
        ledger.record_resource_delivered(
            session_id,
            stage="assembly",
            workflow_step_id="step-001-assembly",
            resource_type="assembly",
            delivery_mode="materialized",
            candidate_bundle_ref=current_ref.model_dump(mode="json"),
            primary_output_ref=current_primary.model_dump(mode="json"),
        )
        ledger.record_stage_completed(
            session_id,
            "assembly",
            workflow_step_id="step-001-assembly",
        )

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "test-session", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _fake_quast_bin(self, tmpdir: str, *, fail: bool = False) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        quast = bin_dir / "quast.py"
        if fail:
            quast.write_text(
                "#!/bin/sh\necho fake quast failed >&2\nexit 9\n",
                encoding="utf-8",
            )
        else:
            quast.write_text(
                "#!/bin/sh\n"
                'outdir=""\n'
                "while [ $# -gt 0 ]; do\n"
                '  if [ "$1" = "-o" ]; then\n'
                "    shift\n"
                '    outdir="$1"\n'
                "  fi\n"
                "  shift\n"
                "done\n"
                'mkdir -p "$outdir"\n'
                "printf 'Assembly\tassembly\n# contigs\t1\n"
                "Largest contig\t8\nTotal length\t8\nN50\t8\n"
                'L50\t1\nGC (%%)\t50.00\n\' > "$outdir/report.tsv"\n'
                "printf 'fake quast report\n' > \"$outdir/report.txt\"\n"
                "printf 'fake quast stdout\n'\n",
                encoding="utf-8",
            )
        quast.chmod(0o755)
        return bin_dir

    def _fake_busco_bin(self, tmpdir: str, *, fail: bool = False) -> Path:
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir(exist_ok=True)
        busco = bin_dir / "busco"
        if fail:
            busco.write_text(
                "#!/bin/sh\necho fake busco failed >&2\nexit 7\n",
                encoding="utf-8",
            )
        else:
            busco.write_text(
                "#!/bin/sh\n"
                'out=""\n'
                'lineage=""\n'
                "while [ $# -gt 0 ]; do\n"
                '  case "$1" in\n'
                '    -o) shift; out="$1" ;;\n'
                '    -l) shift; lineage="$1" ;;\n'
                "  esac\n"
                "  shift\n"
                "done\n"
                'lineage_name=$(basename "$lineage")\n'
                'mkdir -p "$out"\n'
                'summary="$out/short_summary.specific.$lineage_name.$out.txt"\n'
                "printf 'C:95.0%%[S:93.0%%,D:2.0%%],"
                "F:3.0%%,M:2.0%%,n:100\n"
                "95\tComplete BUSCOs (C)\n"
                "93\tComplete and single-copy BUSCOs (S)\n"
                "2\tComplete and duplicated BUSCOs (D)\n"
                "3\tFragmented BUSCOs (F)\n"
                "2\tMissing BUSCOs (M)\n"
                '100\tTotal BUSCO groups searched\n\' > "$summary"\n'
                "printf 'fake busco stdout\n'\n",
                encoding="utf-8",
            )
        busco.chmod(0o755)
        return bin_dir

    def test_existing_assembly_blocks_for_human_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_fasta = Path(tmpdir) / "existing.fa"
            assembly_fasta.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": (
                            "test-session:assembly_fasta_registered:existing.fa"
                        ),
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_fasta),
                    }
                ],
            )
            state = self._build_state(
                entry_mode="existing_assembly",
                registry_ref=str(registry),
                data_dir=tmpdir,
            )
            state["current_stage"] = "assembly_review"
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNotNone(result.get("active_interrupt"))
            self.assertEqual(result["active_interrupt"]["stage"], "assembly_review")
            request_payload = _request_payload(result)
            self.assertIn("accept", request_payload["decisions_allowed"])
            # Eligibility remains a review fact for the assembly LLM resolver;
            # it is not a removed strict/auto policy outcome.
            self.assertIn("auto_decidable", request_payload)
            self.assertNotIn("rerun", request_payload["decisions_allowed"])
            self.assertIn("stop_human_review", request_payload["decisions_allowed"])
            self.assertNotIn("pass", request_payload["decisions_allowed"])
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["status"], "blocked_human")
            self.assertEqual(
                ar_out["candidates"][0]["candidate_id"], "existing_assembly"
            )
            self.assertEqual(
                ar_out["candidates"][0]["primary_output_ref"]["artifact_type"],
                ASSEMBLY_DRAFT_ASSEMBLY,
            )
            request_ref = SessionPaths.from_run_dir(result["data_dir"]).resolve_ref(
                result["active_interrupt"]["request_ref"]["path_ref"]
            )
            self.assertTrue(request_ref.exists())
            request_record = json.loads(request_ref.read_text(encoding="utf-8"))
            self.assertEqual(request_record["stage"], "assembly_review")
            self.assertEqual(
                request_record["payload"]["candidates"][0]["candidate_id"],
                "existing_assembly",
            )

    def test_review_decision_artifact_allows_post_review_actions(self):
        for decision in ["pass", "polish"]:
            artifact = ReviewDecisionArtifact(
                session_id="s1", stage="assembly_review", decision=decision
            )
            self.assertEqual(artifact.decision, decision)

    def test_no_candidates_fails_terminally(self):
        state = self._build_state(entry_mode="raw_inputs")
        graph = build_stage()
        result = graph.invoke(state)
        self.assertEqual(result["supervision_state"], "failed_terminal")
        self.assertIn(
            "central assembly ResourceRef is missing",
            result.get("last_error", ""),
        )
        dispositions = [
            event
            for event in Ledger(result["data_dir"]).read_events(result["session_id"])
            if event["event_type"] == "method_disposition_recorded"
        ]
        self.assertEqual(
            {event["method_name"]: event["disposition"] for event in dispositions},
            {
                "assembly_review.eval.quast": "not_started",
                "assembly_review.eval.busco": "not_started",
            },
        )

    def test_single_assembly_candidate_blocks_human(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state["current_stage"] = "assembly_review"
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNotNone(result.get("active_interrupt"))
            self.assertEqual(result["active_interrupt"]["stage"], "assembly_review")
            self.assertIn("blocked_at", result["active_interrupt"])
            self.assertEqual(
                _request_payload(result)["decisions_allowed"],
                ["accept", "stop_human_review"],
            )
            # current_stage stays at assembly_review while blocked
            self.assertEqual(result["current_stage"], "assembly_review")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(
                ar_out["candidates"][0]["bundle_ref"]["artifact_type"],
                ASSEMBLY_CANDIDATE_BUNDLE,
            )
            self.assertEqual(
                ar_out["candidates"][0]["primary_output_ref"]["artifact_type"],
                ASSEMBLY_DRAFT_ASSEMBLY,
            )
            self.assertTrue(Path(ar_out["eval_bundle_ref"]).exists())

    def test_default_review_planning_does_not_run_quast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            quast_signals = [
                signal
                for signal in result["stage_outputs"]["assembly_review"]["signals"]
                if signal["signal_name"] == "quast"
            ]
            self.assertEqual(quast_signals[0]["status"], "pending")
            events_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            event_types = [
                json.loads(line)["event_type"]
                for line in events_path.read_text().splitlines()
            ]
            self.assertIn("review_requested", event_types)
            self.assertNotIn("attempt_started", event_types)
            events = Ledger(result["data_dir"]).read_events(result["session_id"])
            dispositions = [
                event
                for event in events
                if event["event_type"] == "method_disposition_recorded"
            ]
            self.assertEqual(len(dispositions), 2)
            self.assertEqual(
                {event["method_name"]: event["disposition"] for event in dispositions},
                {
                    "assembly_review.eval.quast": "skipped",
                    "assembly_review.eval.busco": "skipped",
                },
            )
            self.assertTrue(all(event["reason"] for event in dispositions))
            review_sequence = next(
                event["sequence"]
                for event in events
                if event["event_type"] == "review_requested"
            )
            self.assertTrue(
                all(event["sequence"] < review_sequence for event in dispositions)
            )

    def test_default_review_report_shows_missing_or_pending_signals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])

            result = build_stage().invoke(state)

            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertTrue(Path(ar_out["assembly_review_report_ref"]).exists())
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("quast (pending)", report_text)
            self.assertIn("busco (pending)", report_text)
            self.assertIn("No automatic biological thresholds are applied", report_text)
            report_data = json.loads(
                Path(ar_out["assembly_review_packet_ref"]).read_text(encoding="utf-8")
            )
            self.assertNotIn("recommended_candidate_id", report_data)
            self.assertNotIn("pass_fail", json.dumps(report_data))

    def test_local_quast_success_updates_signals_and_review_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir)
            lineage = ensure_busco_lineage(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["current_stage"] = "assembly_review"
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "related_species_reference_dir": str(self._test_reference_dir(state)),
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
                "busco_parameters": {
                    "lineage_path": str(lineage.resolve()),
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "test_fixture",
                    "mode": "genome",
                    "threads": 1,
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            quast_signal = next(
                signal
                for signal in ar_out["signals"]
                if signal["signal_name"] == "quast"
            )
            self.assertEqual(quast_signal["status"], "available")
            self.assertTrue(quast_signal["metric_refs"])
            self.assertEqual(quast_signal["metadata"]["metrics"]["n50"], 8)
            self.assertEqual(quast_signal["metadata"]["metrics"]["total_length"], 8)
            request_payload = _request_payload(result)
            request_quast = next(
                signal
                for signal in request_payload["signals"]
                if signal["signal_name"] == "quast"
            )
            self.assertEqual(request_quast["status"], "available")
            self.assertEqual(request_quast["candidate_id"], "flye_01")
            self.assertTrue(
                all(signal.get("candidate_id") for signal in request_payload["signals"])
            )
            self.assertRegex(
                ar_out["eval_bundle_ref"],
                r"assembly_eval_bundle\.evaluating\.[0-9a-f]{16}\.json$",
            )
            self.assertEqual(
                len(
                    list(
                        (
                            SessionPaths.from_run_dir(
                                result["data_dir"]
                            ).workflow_step_artifacts_dir(
                                "step-002-assembly_review", "assembly_review"
                            )
                        ).glob("assembly_eval_bundle.planned.*.json")
                    )
                ),
                1,
            )
            self.assertTrue(Path(ar_out["assembly_review_report_ref"]).exists())
            self.assertTrue(Path(ar_out["assembly_review_packet_ref"]).exists())
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("flye_01", report_text)
            self.assertIn("N50", report_text)
            self.assertIn("Total length", report_text)
            self.assertIn("Largest contig", report_text)
            self.assertIn("busco", report_text)
            self.assertIn("quality_metrics_are_advisory", report_text)
            report_data = json.loads(
                Path(ar_out["assembly_review_packet_ref"]).read_text(encoding="utf-8")
            )
            self.assertEqual(report_data["candidates"][0]["metrics"]["quast"]["n50"], 8)
            self.assertEqual(report_data["candidates"][0]["signals"]["missing"], [])
            self.assertIn(
                "busco",
                report_data["candidates"][0]["signals"]["available"],
            )
            paths = SessionPaths.from_run_dir(result["data_dir"])
            self.assertEqual(
                request_payload["report_refs"]["assembly_review_report"],
                paths.to_ref(ar_out["assembly_review_report_ref"]),
            )

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl")
                .read_text()
                .splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("assembly_review.quast_report_tsv", artifact_types)
            self.assertIn("assembly_review.quast_report_txt", artifact_types)
            self.assertNotIn("assembly_review.report", artifact_types)
            self.assertIn(ASSEMBLY_REVIEW_PACKET, artifact_types)
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl")
                .read_text()
                .splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_started", event_types)
            self.assertIn("attempt_succeeded", event_types)
            self.assertIn("artifact_registered", event_types)
            self.assertIn("review_requested", event_types)

    def test_local_quast_success_writes_quast_summary_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir)
            lineage = ensure_busco_lineage(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "related_species_reference_dir": str(self._test_reference_dir(state)),
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
                "busco_parameters": {
                    "lineage_path": str(lineage.resolve()),
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "test_fixture",
                    "mode": "genome",
                    "threads": 1,
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            quast_signal = next(
                signal
                for signal in ar_out["signals"]
                if signal["signal_name"] == "quast"
            )
            summary_ref = quast_signal["metadata"].get("quast_summary_ref")
            self.assertTrue(
                summary_ref,
                "QUAST success must expose quast_summary_ref on signal metadata",
            )
            summary_path = Path(summary_ref)
            self.assertTrue(summary_path.exists())
            summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary_payload["candidate_id"], "flye_01")
            self.assertEqual(
                summary_payload["method_name"], "assembly_review.eval.quast"
            )
            self.assertEqual(summary_payload["metrics"]["n50"], 8)
            self.assertEqual(summary_payload["metrics"]["total_length"], 8)
            self.assertIn(
                "quality_metrics_are_advisory",
                summary_payload["notes"],
            )
            # The summary path must also appear on the signal's metric_refs
            # so review consumers see it alongside the raw report.
            self.assertIn(str(summary_path), quast_signal["metric_refs"])

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl")
                .read_text()
                .splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("assembly_review.quast_summary", artifact_types)
            self.assertIn("assembly_review.quast_report_tsv", artifact_types)

    def test_local_busco_success_updates_signals_and_review_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            lineage = ensure_busco_lineage(tmpdir)
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["current_stage"] = "assembly_review"
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "related_species_reference_dir": str(self._test_reference_dir(state)),
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
                "busco_parameters": {
                    "lineage_path": str(lineage.resolve()),
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "operator_offline_cache",
                    "mode": "genome",
                    "threads": 1,
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            busco_signal = next(
                signal
                for signal in ar_out["signals"]
                if signal["signal_name"] == "busco"
            )
            self.assertEqual(busco_signal["status"], "available")
            self.assertTrue(busco_signal["metric_refs"])
            self.assertEqual(
                busco_signal["metadata"]["metrics"]["complete_percent"], 95.0
            )
            self.assertEqual(
                busco_signal["metadata"]["metrics"]["total_busco_groups_searched"], 100
            )
            self.assertEqual(
                busco_signal["metadata"]["provenance"]["lineage_name"],
                "embryophyta_odb10",
            )
            request_payload = _request_payload(result)
            request_busco = next(
                signal
                for signal in request_payload["signals"]
                if signal["signal_name"] == "busco"
            )
            self.assertEqual(request_busco["status"], "available")
            self.assertEqual(request_busco["candidate_id"], "flye_01")
            report_data = json.loads(
                Path(ar_out["assembly_review_packet_ref"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                report_data["candidates"][0]["metrics"]["busco"]["complete_percent"],
                95.0,
            )
            report_text = Path(ar_out["assembly_review_report_ref"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("95.0", report_text)
            self.assertNotIn("fake busco stdout", report_text)

            summary_ref = busco_signal["metadata"].get("busco_summary_ref")
            self.assertTrue(summary_ref)
            summary_payload = json.loads(Path(summary_ref).read_text(encoding="utf-8"))
            self.assertEqual(
                summary_payload["lineage"]["lineage_source"], "operator_offline_cache"
            )
            self.assertIn(str(summary_ref), busco_signal["metric_refs"])

            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl")
                .read_text()
                .splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("assembly_review.busco_short_summary", artifact_types)
            self.assertIn("assembly_review.busco_summary", artifact_types)

    def test_local_busco_missing_lineage_is_not_started_before_human_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "related_species_reference_dir": str(self._test_reference_dir(state)),
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            events = Ledger(result["data_dir"]).read_events(result["session_id"])
            dispositions = [
                event
                for event in events
                if event["event_type"] == "method_disposition_recorded"
            ]
            by_method = {event["method_name"]: event for event in dispositions}
            self.assertEqual(
                by_method["assembly_review.eval.quast"]["disposition"], "completed"
            )
            self.assertEqual(
                by_method["assembly_review.eval.busco"]["disposition"],
                "not_started",
            )
            self.assertEqual(
                by_method["assembly_review.eval.busco"]["reason"],
                "busco_not_configured",
            )
            self.assertEqual(len(dispositions), 2)

    def test_local_busco_failure_marks_signal_failed_and_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            lineage = ensure_busco_lineage(tmpdir)
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            self._fake_busco_bin(tmpdir, fail=True)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "related_species_reference_dir": str(self._test_reference_dir(state)),
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "quast_parameters": {"threads": 1},
                "busco_parameters": {
                    "lineage_path": str(lineage.resolve()),
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "operator_offline_cache",
                    "mode": "genome",
                    "threads": 1,
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            busco_signal = next(
                signal
                for signal in result["stage_outputs"]["assembly_review"]["signals"]
                if signal["signal_name"] == "busco"
            )
            self.assertEqual(busco_signal["status"], "failed")
            self.assertIn("candidate_not_rejected_from_metrics", busco_signal["notes"])
            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl")
                .read_text()
                .splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)

    def test_local_quast_failure_marks_signal_failed_and_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            fake_bin = self._fake_quast_bin(tmpdir, fail=True)
            self._fake_busco_bin(tmpdir)
            lineage = ensure_busco_lineage(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "related_species_reference_dir": str(self._test_reference_dir(state)),
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
                "busco_parameters": {
                    "lineage_path": str(lineage.resolve()),
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "test_fixture",
                    "mode": "genome",
                    "threads": 1,
                },
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            quast_signal = next(
                signal
                for signal in result["stage_outputs"]["assembly_review"]["signals"]
                if signal["signal_name"] == "quast"
            )
            self.assertEqual(quast_signal["status"], "failed")
            self.assertIn("candidate_not_rejected_from_metrics", quast_signal["notes"])
            ledger_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "ledger" / "events.jsonl")
                .read_text()
                .splitlines()
            ]
            event_types = [event["event_type"] for event in ledger_events]
            self.assertIn("attempt_failed", event_types)
            self.assertIn("review_requested", event_types)
            artifact_events = [
                json.loads(line)
                for line in (Path(result["data_dir"]) / "artifacts" / "index.jsonl")
                .read_text()
                .splitlines()
            ]
            artifact_types = {event["artifact_type"] for event in artifact_events}
            self.assertIn("command.stdout", artifact_types)
            self.assertIn("command.stderr", artifact_types)
            self.assertEqual(
                sum(
                    event["artifact_type"] == "command.stdout"
                    for event in artifact_events
                ),
                2,
            )
            self.assertEqual(
                sum(
                    event["artifact_type"] == "command.stderr"
                    for event in artifact_events
                ),
                2,
            )

    def test_local_quast_missing_draft_marks_signal_missing_without_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(Path(tmpdir) / "missing.fasta"),
            )
            fake_bin = self._fake_quast_bin(tmpdir)
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            primary_path = SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(
                state["resource_refs"]["assembly"]["metadata"]["primary_output_ref"][
                    "path_ref"
                ]
            )
            primary_path.unlink()
            state["stage_outputs"]["assembly_review"] = {
                "execution_mode": "local",
                "related_species_reference_dir": str(self._test_reference_dir(state)),
                "env": {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("candidate handoff is invalid", result["last_error"])

    def test_multiple_candidates_block_human(self):
        state = self._build_state(
            entry_mode="raw_inputs",
            candidate_bundles=[
                {
                    "candidate_id": "flye_01",
                    "method_name": "flye",
                    "status": "planned",
                    "candidate_ref": "ref/flye",
                },
                {
                    "candidate_id": "hifiasm_01",
                    "method_name": "hifiasm",
                    "status": "planned",
                    "candidate_ref": "ref/hifiasm",
                },
            ],
        )
        graph = build_stage()
        result = graph.invoke(state)
        self.assertEqual(result["supervision_state"], "blocked_human")
        ar_out = result["stage_outputs"]["assembly_review"]
        self.assertEqual(ar_out["candidate_count"], 2)

    def test_rejected_candidates_remain_traceable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_state(
                entry_mode="raw_inputs",
                candidate_bundles=[
                    {
                        "candidate_id": "flye_01",
                        "method_name": "flye",
                        "status": "planned",
                        "candidate_ref": "ref/flye",
                    },
                    {
                        "candidate_id": "canu_01",
                        "method_name": "canu",
                        "status": "planned",
                        "candidate_ref": "ref/canu",
                    },
                ],
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            candidate_ids = [c["candidate_id"] for c in ar_out["candidates"]]
            self.assertIn("flye_01", candidate_ids)
            self.assertIn("canu_01", candidate_ids)

    def test_does_not_auto_advance_from_single_metric(self):
        """A single candidate must not be auto-accepted without human policy."""
        state = self._build_state(
            entry_mode="raw_inputs",
            candidate_bundles=[
                {
                    "candidate_id": "flye_01",
                    "method_name": "flye",
                    "status": "planned",
                    "candidate_ref": "ref/flye",
                },
            ],
        )
        state["current_stage"] = "assembly_review"
        graph = build_stage()
        result = graph.invoke(state)
        # Must NOT advance to next stage automatically
        self.assertEqual(result["current_stage"], "assembly_review")
        self.assertEqual(result["supervision_state"], "blocked_human")

    def test_planned_signals_include_quast_and_busco(self):
        state = self._build_state(
            entry_mode="raw_inputs",
            candidate_bundles=[
                {
                    "candidate_id": "flye_01",
                    "method_name": "flye",
                    "status": "planned",
                },
            ],
        )
        graph = build_stage()
        result = graph.invoke(state)
        ar_out = result["stage_outputs"]["assembly_review"]
        planned = ar_out.get("planned_signals", [])
        self.assertIn("quast", planned)
        self.assertIn("busco", planned)

    def test_planned_signals_include_merqury_when_short_reads_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
            )
            short_reads = Path(tmpdir) / "short.fq"
            long_reads = Path(tmpdir) / "long.fq"
            short_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "test-session:clean_short_dna_reads:short.fq",
                        "asset_type": "clean_short_dna_reads",
                        "path": str(short_reads),
                    },
                    {
                        "asset_id": "test-session:clean_long_reads:long.fq",
                        "asset_type": "clean_long_reads",
                        "path": str(long_reads),
                    },
                ],
            )
            state = self._build_state(
                entry_mode="raw_inputs", registry_ref=str(registry), data_dir=tmpdir
            )
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            graph = build_stage()
            result = graph.invoke(state)
            ar_out = result["stage_outputs"]["assembly_review"]
            planned = ar_out.get("planned_signals", [])
            self.assertEqual(planned, ["quast", "busco"])
            self.assertNotIn("merqury", planned)
            self.assertNotIn("mapping_coverage", planned)
            self.assertNotIn("purge_dups_check", planned)
            eval_payload = json.loads(
                Path(ar_out["eval_bundle_ref"]).read_text(encoding="utf-8")
            )
            self.assertIn("clean_short_dna_reads", eval_payload["available_inputs"])

    def test_existing_assembly_writes_review_requested_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_fasta = Path(tmpdir) / "existing.fa"
            assembly_fasta.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": (
                            "test-session:assembly_fasta_registered:existing.fa"
                        ),
                        "asset_type": "assembly_fasta_registered",
                        "path": str(assembly_fasta),
                    }
                ],
            )
            state = self._build_state(
                entry_mode="existing_assembly",
                registry_ref=str(registry),
                data_dir=tmpdir,
            )
            state["current_stage"] = "assembly_review"
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "blocked_human")
            events_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            requested_events = [
                e for e in events if e["event_type"] == "review_requested"
            ]
            decision_events = [
                e for e in events if e["event_type"] == "review_decision"
            ]
            self.assertEqual(len(requested_events), 1)
            self.assertEqual(requested_events[0]["stage"], "assembly_review")
            self.assertEqual(
                _request_payload(result)["candidates"][0]["candidate_id"],
                "existing_assembly",
            )
            self.assertEqual(decision_events, [])

    def test_human_gate_writes_review_requested_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_state(
                entry_mode="raw_inputs",
                candidate_bundles=[
                    {
                        "candidate_id": "flye_01",
                        "method_name": "flye",
                        "status": "planned",
                    },
                ],
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            events_path = Path(result["data_dir"]) / "ledger" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            request_events = [
                e for e in events if e["event_type"] == "review_requested"
            ]
            self.assertEqual(len(request_events), 1)
            self.assertEqual(request_events[0]["stage"], "assembly_review")
            self.assertTrue(
                any(
                    ref.get("role") == "review_request_artifact_ref"
                    for ref in request_events[0]["refs"]
                )
            )
            request_record = _request_record(result)
            self.assertEqual(
                request_record["review_request_id"],
                result["active_interrupt"]["review_request_id"],
            )

    def test_assembly_stage_executed_candidate_ref_points_to_real_fasta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            draft = Path(tmpdir) / "flye_draft.fasta"
            draft.write_text(">contig1\nACGT\n", encoding="utf-8")
            flye_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="test-session:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(flye_ref)],
                "draft_assembly_ref": str(draft),
                "executed_candidate_refs": [str(flye_ref)],
            }
            self._set_candidate_refs(state, [str(flye_ref)])

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            candidate = result["stage_outputs"]["assembly_review"]["candidates"][0]
            self.assertEqual(
                candidate["candidate_id"], "test-session:assembly.candidate.flye"
            )
            self.assertEqual(
                candidate["primary_output_ref"]["artifact_type"],
                ASSEMBLY_DRAFT_ASSEMBLY,
            )
            self.assertTrue(
                SessionPaths.from_run_dir(result["data_dir"])
                .resolve_ref(candidate["primary_output_ref"]["path_ref"])
                .exists()
            )

    def test_consumes_assembly_stage_selected_candidate_refs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            flye_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="test-session:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
            )
            hifiasm_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="test-session:assembly.candidate.hifiasm",
                method_name="assembly.candidate.hifiasm",
            )
            state = self._build_state(entry_mode="raw_inputs", data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(flye_ref), str(hifiasm_ref)],
                "rejected_candidates": {
                    "assembly.candidate.canu": "genome_size_required"
                },
            }
            self._set_candidate_refs(state, [str(flye_ref), str(hifiasm_ref)])

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["candidate_count"], 2)
            candidate_ids = {
                candidate["candidate_id"] for candidate in ar_out["candidates"]
            }
            self.assertEqual(
                candidate_ids,
                {
                    "test-session:assembly.candidate.flye",
                    "test-session:assembly.candidate.hifiasm",
                },
            )
            self.assertEqual(
                ar_out["rejected_candidates"],
                {"assembly.candidate.canu": "genome_size_required"},
            )

    def test_relative_data_dir_resolves_to_absolute_paths_in_quast_script(self):
        # Regression: freeze_quast_attempts embeds data_dir-derived paths into
        # the generated shell script. When data_dir is relative the runner's
        # cwd (nested under data_dir) would make those paths resolve to a
        # non-existent nested location. The script must embed absolute paths.
        import os
        from unittest.mock import patch

        from app.execution.env_resolver import EnvSpec
        from app.stages.assembly_review.method_application.quast import (
            freeze_quast_attempts,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            draft = tmp / "draft.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="s1:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                run_dir = v2_run_dir(tmp / "out", "s1")
                state = self._build_state(
                    session_id="s1",
                    entry_mode="raw_inputs",
                    data_dir=str(tmp / "out"),
                )
                state["data_dir"] = "out/runs/s1"
                state["stage_outputs"]["assembly"] = {
                    "status": "draft_ready",
                    "selected_candidate_refs": [str(candidate_ref)],
                }
                self._set_candidate_refs(state, [str(candidate_ref)])
                from app.stages.assembly_review.nodes import (
                    plan_review_metrics,
                    resolve_candidates,
                )

                state.update(resolve_candidates(state))
                state.update(plan_review_metrics(state))
                state["stage_outputs"]["assembly_review"] = {
                    **state["stage_outputs"]["assembly_review"],
                    "execution_mode": "local",
                    "env": {"PATH": "/nonexistent"},
                    "quast_parameters": {"threads": 1},
                }
                with (
                    patch(
                        "app.stages.assembly_review.method_application.quast.review_methods_error",
                        return_value=None,
                    ),
                    patch(
                        "app.stages.assembly_review.method_application.quast.resolve_method_env",
                        return_value=(EnvSpec(conda_env="env_geno_exec"), [], None),
                    ),
                ):
                    result = freeze_quast_attempts(state)
                attempts = (
                    result.get("stage_outputs", {})
                    .get("assembly_review", {})
                    .get("execution_attempts", [])
                )
                self.assertTrue(attempts, "expected at least one frozen attempt")
                attempt = load_frozen_attempt(
                    {**state, **result},
                    attempts[0]["frozen_attempt_ref"],
                    expected_stage="assembly_review",
                    expected_method_name="assembly_review.eval.quast",
                )
                script_path = attempt.script_path
                self.assertTrue(
                    script_path.exists(), f"script not found: {script_path}"
                )
                script_text = script_path.read_text(encoding="utf-8")
                self.assertNotIn(" -r ", script_text)
                self.assertEqual(attempt.metadata["reference_mode"], "reference_free")
                expected_out_dir = (
                    SessionPaths.from_run_dir(run_dir).workflow_step_work_dir(
                        "step-002-assembly_review", "assembly_review"
                    )
                    / "quast"
                    / "s1_assembly.candidate.flye"
                    / "quast_out"
                )
                self.assertIn(str(expected_out_dir), script_text)
                self.assertNotIn(" out/runs/s1/artifacts/", script_text)
            finally:
                os.chdir(old_cwd)

    def test_quast_uses_manifest_reference_and_freezes_asset_dependency(self):
        from app.execution.env_resolver import EnvSpec
        from app.stages.assembly_review.nodes import plan_review_metrics

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            draft = tmp / "draft.fasta"
            reference = tmp / "reference.fasta"
            draft.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
            reference.write_text(">chr1\nACGTACGT\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": (
                            "test-session:reference_genome_fasta:reference.fasta"
                        ),
                        "asset_type": "reference_genome_fasta",
                        "path": str(reference),
                        "format": "fasta",
                        "checksum": sha256_file(reference),
                        "checksum_status": "ready",
                        "size_bytes": reference.stat().st_size,
                        "metadata": {"compression": "none", "layout": "single"},
                    }
                ],
            )
            candidate_ref = self._write_candidate_bundle(
                tmpdir,
                candidate_id="flye_01",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(draft),
            )
            state = self._build_state(
                entry_mode="raw_inputs",
                registry_ref=str(registry),
                data_dir=tmpdir,
            )
            state["stage_outputs"]["assembly"] = {
                "status": "draft_ready",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            self._set_candidate_refs(state, [str(candidate_ref)])
            state.update(resolve_candidates(state))
            state.update(plan_review_metrics(state))
            state["stage_outputs"]["assembly_review"] = {
                **state["stage_outputs"]["assembly_review"],
                "execution_mode": "local",
                "quast_parameters": {"threads": 2},
            }
            with (
                patch(
                    "app.stages.assembly_review.method_application.quast.review_methods_error",
                    return_value=None,
                ),
                patch(
                    "app.stages.assembly_review.method_application.quast.resolve_method_env",
                    return_value=(EnvSpec(conda_env="env_geno_exec"), [], None),
                ),
            ):
                result = freeze_quast_attempts(state)

            attempts = result["stage_outputs"]["assembly_review"]["execution_attempts"]
            attempt = load_frozen_attempt(
                {**state, **result},
                attempts[0]["frozen_attempt_ref"],
                expected_stage="assembly_review",
                expected_method_name="assembly_review.eval.quast",
            )
            script_text = attempt.script_path.read_text(encoding="utf-8")
            self.assertIn(f"-r {reference.resolve()}", script_text)
            self.assertEqual(attempt.metadata["reference_mode"], "reference_based")
            self.assertEqual(
                attempt.metadata["reference_genome_asset_id"],
                "test-session:reference_genome_fasta:reference.fasta",
            )
            asset_dependencies = [
                dependency
                for dependency in attempt.input_dependencies
                if dependency.kind == "asset"
            ]
            self.assertEqual(len(asset_dependencies), 1)
            self.assertEqual(asset_dependencies[0].path, str(reference.resolve()))


class AssemblyReviewSchemaTests(unittest.TestCase):
    def test_assembly_review_plan_has_no_threshold_defaults(self):
        from app.schemas.plan import AssemblyReviewPlan

        plan = AssemblyReviewPlan(status="planned")
        self.assertEqual(plan.decision, "pending")
        self.assertEqual(plan.planned_signals, [])
        # Thresholds are intentionally not part of the plan schema;
        # they require human approval before use.

    def test_review_decision_artifact_allows_current_commit_decisions(self):
        from app.schemas.artifacts import ReviewDecisionArtifact

        for decision in [
            "accept",
            "pass",
            "purge",
            "polish",
            "continue_low_confidence",
            "stop_human_review",
        ]:
            with self.subTest(decision=decision):
                artifact = ReviewDecisionArtifact(
                    session_id="s1",
                    stage="assembly_review",
                    decision=decision,
                )
                self.assertEqual(artifact.decision, decision)

    def test_review_payload_uses_current_assembly_review_decisions(self):
        from app.review.base import ReviewPayload

        payload = ReviewPayload(
            stage="assembly_review",
            reason="test",
            decisions_allowed=[
                "pass",
                "accept",
                "purge",
                "polish",
                "stop_human_review",
            ],
        )
        self.assertIn("pass", payload.decisions_allowed)
        self.assertIn("accept", payload.decisions_allowed)
        self.assertIn("purge", payload.decisions_allowed)
        self.assertIn("polish", payload.decisions_allowed)
        self.assertNotIn("rerun", payload.decisions_allowed)
        self.assertIn("stop_human_review", payload.decisions_allowed)


class AssemblyReviewCutoverTests(unittest.TestCase):
    """Regression coverage for the current Assembly Review boundary."""

    def _build_blocked_state(
        self,
        tmpdir: str,
        *,
        extra_candidate_count: int = 0,
        persist_request: bool = True,
    ) -> dict:
        """Build a Flye-candidate state blocked at the assembly-review gate."""
        stage_tests = AssemblyReviewStageTests()
        self.addCleanup(stage_tests.doCleanups)
        session_id = "test-rerun-session"
        assembly_fasta = Path(tmpdir) / "draft.fasta"
        assembly_fasta.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
        candidate_refs = [
            stage_tests._write_candidate_bundle(
                tmpdir,
                candidate_id=f"{session_id}:assembly.candidate.flye",
                method_name="assembly.candidate.flye",
                draft_assembly_fasta=str(assembly_fasta),
            )
        ]
        for index in range(extra_candidate_count):
            candidate_refs.append(
                stage_tests._write_candidate_bundle(
                    tmpdir,
                    candidate_id=(
                        f"{session_id}:assembly.candidate.flye:alternative_{index + 1}"
                    ),
                    method_name="assembly.candidate.flye",
                    draft_assembly_fasta=str(assembly_fasta),
                )
            )
        state = stage_tests._build_state(
            session_id=session_id, entry_mode="raw_inputs", data_dir=tmpdir
        )
        state["stage_outputs"]["assembly"] = {"status": "completed"}
        stage_tests._set_candidate_refs(state, [str(ref) for ref in candidate_refs])
        from app.stages.assembly_review.nodes import (
            evaluate_review_rules,
            human_review_gate,
            plan_review_metrics,
            resolve_candidates,
        )

        state.update(resolve_candidates(state))
        state.update(plan_review_metrics(state))
        state.update(evaluate_review_rules(state))
        if not persist_request:
            return state
        return {**state, **human_review_gate(state)}

    @staticmethod
    def _review_binding(state: dict) -> dict:
        return {
            "gate_id": state["active_interrupt"]["gate_id"],
            "review_request_id": state["active_interrupt"]["review_request_id"],
            "review_request_hash": state["active_interrupt"]["request_hash"],
        }

    def test_json_artifacts_record_registered_refs_after_publication(self):
        from app.schemas.artifacts import (
            ASSEMBLY_CANDIDATE_COMPARISON,
            ASSEMBLY_REVIEW_ACTION,
            ASSEMBLY_REVIEW_EVAL_BUNDLE,
            ASSEMBLY_REVIEW_PACKET,
            ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
        )
        from app.stages.assembly_review.method_application.support import (
            write_json_artifact,
        )
        from app.storage.artifacts import artifact_ref_for_path
        from app.storage.ledger import Ledger

        artifact_types = (
            ASSEMBLY_REVIEW_EVAL_BUNDLE,
            ASSEMBLY_REVIEW_RELATED_SPECIES_EVIDENCE_PACK,
            ASSEMBLY_CANDIDATE_COMPARISON,
            ASSEMBLY_REVIEW_PACKET,
            ASSEMBLY_REVIEW_ACTION,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir)
            for index, artifact_type in enumerate(artifact_types):
                path = write_json_artifact(
                    state,
                    f"registered-{index}.json",
                    {"artifact_type": artifact_type},
                    artifact_type=artifact_type,
                )
                ref = artifact_ref_for_path(
                    state["data_dir"],
                    state["session_id"],
                    path,
                    expected_type=artifact_type,
                )
                registered = [
                    event
                    for event in Ledger(state["data_dir"]).read_event_rows(
                        state["session_id"]
                    )
                    if event["event_type"] == "artifact_registered"
                    and event["subject_id"] == ref.artifact_id
                ]

                self.assertEqual(len(registered), 1)
                self.assertEqual(
                    registered[0]["refs"],
                    [
                        {
                            "role": "artifact",
                            "artifact_id": ref.artifact_id,
                            "path_ref": ref.path_ref,
                            "sha256": ref.sha256,
                            "size_bytes": ref.size_bytes,
                            "artifact_type": ref.artifact_type,
                        }
                    ],
                )

    def test_unsupported_review_decision_fails_closed(self):
        from app.control.errors import ResumeValidationError
        from app.review.application import persist_review_intent
        from app.review.base import ResumeReviewRequest
        from app.stages.assembly_review.nodes import (
            prepare_assembly_review_application,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir)
            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="invalid",
                candidate_id=state["stage_outputs"]["assembly_review"]["candidates"][0][
                    "candidate_id"
                ],
                **self._review_binding(state),
            )

            with self.assertRaisesRegex(
                ResumeValidationError,
                "unsupported assembly_review decision: invalid",
            ):
                intent = persist_review_intent(state, request, source="review")
                prepare_assembly_review_application(state, intent.intent)

    def test_validates_rejected_candidate_set_and_rejects_unknown_ids(self):
        from app.control.errors import ResumeValidationError
        from app.review.approval import load_verified_interrupt_request
        from app.review.base import ResumeReviewRequest
        from app.stages.assembly_review.nodes import (
            validate_assembly_review_intent_request,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir, extra_candidate_count=1)
            candidate_ids = [
                candidate["candidate_id"]
                for candidate in state["stage_outputs"]["assembly_review"]["candidates"]
            ]
            selected, rejected = candidate_ids
            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="accept",
                candidate_id=selected,
                rejected_candidates=[rejected, rejected],
                notes=["keep this order"],
                **self._review_binding(state),
            )

            _ref, request_record = load_verified_interrupt_request(
                state, state["active_interrupt"]
            )
            canonical = validate_assembly_review_intent_request(
                state, request, request_record
            )

            self.assertEqual(canonical.rejected_candidates, [rejected])
            self.assertEqual(canonical.notes, ["keep this order"])
            with self.assertRaisesRegex(
                ResumeValidationError, "unsupported assembly_review decision"
            ):
                validate_assembly_review_intent_request(
                    state,
                    request.model_copy(update={"decision": "invalid"}),
                    request_record,
                )
            with self.assertRaisesRegex(ResumeValidationError, "unknown.*rejected"):
                validate_assembly_review_intent_request(
                    state,
                    request.model_copy(update={"rejected_candidates": ["unknown"]}),
                    request_record,
                )
            stopped = validate_assembly_review_intent_request(
                state,
                request.model_copy(
                    update={
                        "decision": "stop_human_review",
                        "candidate_id": "unknown",
                        "candidate_ids": ["unknown"],
                        "rejected_candidates": ["unknown"],
                        "method_name": "assembly.candidate.flye",
                        "metadata": {
                            "action_output_ref": "work/action.fasta",
                            "evidence_refs": ["work/evidence.txt"],
                            "human_approved_action": True,
                            "provenance_ref": "work/provenance.json",
                        },
                    }
                ),
                request_record,
            )
            self.assertIsNone(stopped.candidate_id)
            self.assertEqual(stopped.candidate_ids, [])
            self.assertEqual(stopped.rejected_candidates, [])
            self.assertIsNone(stopped.method_name)
            self.assertEqual(stopped.metadata, {})

    def test_transform_intent_rejects_external_and_missing_file_refs(self):
        from app.control.errors import ResumeValidationError
        from app.review.approval import load_verified_interrupt_request
        from app.review.base import ResumeReviewRequest
        from app.stages.assembly_review.nodes import (
            validate_assembly_review_intent_request,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir)
            candidate_id = state["stage_outputs"]["assembly_review"]["candidates"][0][
                "candidate_id"
            ]
            _ref, request_record = load_verified_interrupt_request(
                state, state["active_interrupt"]
            )
            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="purge",
                candidate_id=candidate_id,
                method_name="assembly.candidate.flye",
                metadata={
                    "action_output_ref": "../outside.fasta",
                    "human_approved_action": True,
                    "evidence_notes": ["human-verified purge"],
                },
                **self._review_binding(state),
            )
            with self.assertRaisesRegex(ResumeValidationError, "run-relative"):
                validate_assembly_review_intent_request(state, request, request_record)

            missing = request.model_copy(
                update={
                    "metadata": {
                        **request.metadata,
                        "action_output_ref": "work/missing-action.fasta",
                    }
                }
            )
            with self.assertRaisesRegex(ResumeValidationError, "materialized file"):
                validate_assembly_review_intent_request(state, missing, request_record)

    def test_prepared_effect_uses_typed_refs_and_decision_metadata(self):
        from pydantic import ValidationError

        from app.review.application import RunRelativeFileRef, persist_review_intent
        from app.review.base import ResumeReviewRequest
        from app.stages.assembly_review.nodes import (
            prepare_assembly_review_application,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir)
            paths = SessionPaths.from_run_dir(state["data_dir"])
            rationale_path = paths.workflow_step_work_path(
                "step-002-assembly_review", "assembly_review", "rationale.json"
            )
            rationale_path.parent.mkdir(parents=True, exist_ok=True)
            rationale_path.write_text('{"reason":"manual review"}', encoding="utf-8")
            rationale_ref = publish_artifact_record(
                state["data_dir"],
                state["session_id"],
                ArtifactMeta.from_file(
                    rationale_path,
                    stage="assembly_review",
                    method_name="assembly_review.contract",
                    artifact_type="review.rationale",
                ),
            ).ref
            candidate_id = state["stage_outputs"]["assembly_review"]["candidates"][0][
                "candidate_id"
            ]
            request = ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly_review",
                decision="accept",
                candidate_id=candidate_id,
                rationale_artifact_ref=rationale_ref,
                **self._review_binding(state),
            )
            intent = persist_review_intent(
                state, request, source="assembly-review-test"
            )
            prepared = prepare_assembly_review_application(state, intent.intent)
            effect = prepared.effect

            self.assertIsInstance(effect.pre_action_assembly_ref, RunRelativeFileRef)
            self.assertIsInstance(effect.final_assembly_ref, RunRelativeFileRef)
            self.assertIsInstance(effect.output_assembly_ref, RunRelativeFileRef)
            self.assertEqual(effect.rationale_artifact_ref, rationale_ref)
            self.assertFalse(effect.output_assembly_ref.path_ref.startswith("/"))
            self.assertTrue(
                paths.resolve_ref(effect.output_assembly_ref.path_ref).is_file()
            )

            decision_path = paths.resolve_ref(effect.decision_ref.path_ref)
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            self.assertEqual(
                decision["decision"]["metadata"]["rationale_artifact_ref"],
                rationale_ref.model_dump(mode="json"),
            )
            self.assertEqual(
                decision["decision"]["metadata"]["output_primary_ref"],
                effect.primary_output_ref.model_dump(mode="json"),
            )

            with self.assertRaises(ValidationError):
                ResumeReviewRequest(
                    session_id=state["session_id"],
                    stage="assembly_review",
                    decision="accept",
                    candidate_id=candidate_id,
                    rationale_artifact_ref="not-an-artifact-ref",
                    **self._review_binding(state),
                )

    def test_candidate_comparison_replay_reuses_existing_artifact(self):
        """Reuse comparison output when candidates and signals are unchanged."""
        from app.stages.assembly_review.nodes import generate_candidate_comparison
        from app.storage.artifacts import read_artifact_records
        from app.storage.ledger import Ledger

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir, extra_candidate_count=1)
            state["stage_outputs"]["assembly_review"] = {
                **state["stage_outputs"]["assembly_review"],
                "signals": [],
            }

            first_update = generate_candidate_comparison(state)
            first_ref = first_update["stage_outputs"]["assembly_review"][
                "candidate_comparison_ref"
            ]
            merged = {**state, "stage_outputs": first_update["stage_outputs"]}
            artifacts_before = read_artifact_records(
                state["data_dir"], state["session_id"]
            )
            events_before = Ledger(state["data_dir"]).read_events(state["session_id"])
            comparison_events = [
                event
                for event in events_before
                if event["event_type"] == "candidate_comparison_generated"
            ]
            self.assertEqual(len(comparison_events), 1)
            self.assertEqual(
                comparison_events[0]["workflow_step_id"],
                "step-002-assembly_review",
            )
            mtime_before = Path(first_ref).stat().st_mtime_ns

            replay_update = generate_candidate_comparison(merged)

            self.assertEqual(replay_update, {})
            self.assertEqual(Path(first_ref).stat().st_mtime_ns, mtime_before)
            self.assertEqual(
                read_artifact_records(state["data_dir"], state["session_id"]),
                artifacts_before,
            )
            self.assertEqual(
                Ledger(state["data_dir"]).read_events(state["session_id"]),
                events_before,
            )

    def test_review_report_replay_does_not_duplicate_artifact_records(self):
        from app.stages.assembly_review.nodes import human_review_gate
        from app.storage.artifacts import read_artifact_records

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_blocked_state(tmpdir, persist_request=False)
            state["stage_outputs"]["assembly_review"] = {
                **state["stage_outputs"]["assembly_review"],
                "status": "evaluated",
                "signals": [],
                "review_plan": {
                    "planned_signals": ["quast"],
                    "available_signals": [],
                    "missing_signals": ["quast"],
                },
            }

            first = human_review_gate(state)
            merged = {**state, **first}
            artifacts_before = [
                record
                for record in read_artifact_records(
                    state["data_dir"], state["session_id"]
                )
                if record["artifact_type"]
                in {"assembly_review.report", ASSEMBLY_REVIEW_PACKET}
            ]
            report_ref = first["stage_outputs"]["assembly_review"][
                "assembly_review_report_ref"
            ]
            report_before = Path(report_ref).read_text(encoding="utf-8")

            second = human_review_gate(merged)

            artifacts_after = [
                record
                for record in read_artifact_records(
                    state["data_dir"], state["session_id"]
                )
                if record["artifact_type"]
                in {"assembly_review.report", ASSEMBLY_REVIEW_PACKET}
            ]
            self.assertEqual(
                Path(report_ref).read_text(encoding="utf-8"), report_before
            )
            self.assertEqual(artifacts_after, artifacts_before)
            second_result = {**merged, **second}
            self.assertEqual(
                _request_payload(second_result)["report_refs"][
                    "assembly_review_report"
                ],
                SessionPaths.from_run_dir(state["data_dir"]).to_ref(report_ref),
            )


if __name__ == "__main__":
    unittest.main()
