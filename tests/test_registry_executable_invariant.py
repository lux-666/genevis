"""Invariant: every registered method has a real lifecycle or planner route."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch

from app.control.workflow_plan import workflow_step_id
from app.entrypoints.ask_planner import (
    READ_QC_FILTER_SKIP,
    READ_QC_STATS_METHOD,
    build_planner_method_catalog,
    build_run_config_payload,
)
from app.execution.lifecycle import MethodExecutionLifecycle
from app.schemas.ask_plan import AskPlanState, PlanStageOccurrence
from app.schemas.descriptors import DescriptorRegistry, FrozenAttemptSpec
from app.schemas.state import initial_state
from app.stages.assembly import builder as assembly_builder
from app.stages.assembly.method_application import (
    flye,
    hifiasm,
    hifiasm_ont,
    nextdenovo,
)
from app.stages.assembly_dedup import builder as assembly_dedup_builder
from app.stages.assembly_dedup import nodes as assembly_dedup_nodes
from app.stages.assembly_review import builder as assembly_review_builder
from app.stages.assembly_review.method_application import busco, quast
from app.stages.genome_survey import builder as genome_survey_builder
from app.stages.genome_survey.method_application import genomescope2, jellyfish
from app.stages.hic_scaffolding import builder as hic_scaffolding_builder
from app.stages.hic_scaffolding.method_application import autohic
from app.stages.read_qc import builder as read_qc_builder
from app.stages.read_qc.constants import LENGTH_FILTER_SKIP
from app.stages.read_qc.method_application import seqkit_length_filter, seqkit_stats
from app.stages.read_qc.nodes import commit_read_qc, plan_length_filter
from app.stages.t2t import builder as t2t_builder
from app.stages.t2t import nodes as t2t_nodes
from app.storage.ledger import Ledger


@dataclass(frozen=True)
class LocalBinding:
    """Actual adapter objects proving one registry method's execution route."""

    adapter: ModuleType
    lifecycle: MethodExecutionLifecycle
    method_builder: object
    stage_builder: object
    outer_node: str


LOCAL_BINDINGS = (
    LocalBinding(
        flye,
        flye.LIFECYCLE,
        flye.build_flye_graph,
        assembly_builder.build_stage,
        "flye",
    ),
    LocalBinding(
        nextdenovo,
        nextdenovo.LIFECYCLE,
        nextdenovo.build_nextdenovo_graph,
        assembly_builder.build_stage,
        "nextdenovo",
    ),
    LocalBinding(
        hifiasm,
        hifiasm.LIFECYCLE,
        hifiasm.build_hifiasm_graph,
        assembly_builder.build_stage,
        "hifiasm",
    ),
    LocalBinding(
        hifiasm_ont,
        hifiasm_ont.LIFECYCLE,
        hifiasm_ont.build_hifiasm_ont_graph,
        assembly_builder.build_stage,
        "hifiasm_ont",
    ),
    LocalBinding(
        assembly_dedup_nodes,
        assembly_dedup_nodes.PURGE_DUPS_LIFECYCLE,
        assembly_dedup_nodes.build_purge_dups_graph,
        assembly_dedup_builder.build_stage,
        "purge_dups",
    ),
    LocalBinding(
        quast,
        quast.LIFECYCLE,
        quast.build_quast_graph,
        assembly_review_builder.build_stage,
        "quast",
    ),
    LocalBinding(
        busco,
        busco.LIFECYCLE,
        busco.build_busco_graph,
        assembly_review_builder.build_stage,
        "busco",
    ),
    LocalBinding(
        jellyfish,
        jellyfish.LIFECYCLE,
        jellyfish.build_jellyfish_graph,
        genome_survey_builder.build_stage,
        "jellyfish",
    ),
    LocalBinding(
        genomescope2,
        genomescope2.LIFECYCLE,
        genomescope2.build_genomescope2_graph,
        genome_survey_builder.build_stage,
        "genomescope2",
    ),
    LocalBinding(
        autohic,
        autohic.LIFECYCLE,
        autohic.build_autohic_graph,
        hic_scaffolding_builder.build_stage,
        "autohic",
    ),
    LocalBinding(
        t2t_nodes,
        t2t_nodes.T2T_ALIGNMENT_LIFECYCLE,
        t2t_nodes.build_t2t_alignment_graph,
        t2t_builder.build_stage,
        "alignment",
    ),
    LocalBinding(
        seqkit_stats,
        seqkit_stats.LIFECYCLE,
        seqkit_stats.build_seqkit_stats_graph,
        read_qc_builder.build_stage,
        "seqkit_stats",
    ),
    LocalBinding(
        seqkit_length_filter,
        seqkit_length_filter.LIFECYCLE,
        seqkit_length_filter.build_seqkit_length_filter_graph,
        read_qc_builder.build_stage,
        "seqkit_length_filter",
    ),
)


class RegistryExecutableInvariantTests(unittest.TestCase):
    def test_local_lifecycles_are_a_complete_disjoint_registry_partition(self):
        descriptor_registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        registry_local = {
            method_name
            for method_name, descriptor in descriptor_registry.methods.items()
            if descriptor.planner.execution_kind == "local"
        }
        registry_planner_only = set(descriptor_registry.methods) - registry_local
        lifecycle_methods = {
            binding.lifecycle.method_name for binding in LOCAL_BINDINGS
        }
        planner_catalog = build_planner_method_catalog(descriptor_registry)
        planner_only_methods = {
            method_name
            for method_name, descriptor in planner_catalog.by_method.items()
            if descriptor["execution_kind"] == "planner_only"
        }

        self.assertEqual(lifecycle_methods, registry_local)
        self.assertEqual(planner_only_methods, registry_planner_only)
        self.assertFalse(lifecycle_methods & planner_only_methods)
        self.assertEqual(
            lifecycle_methods | planner_only_methods, set(descriptor_registry.methods)
        )

    def test_every_local_binding_builds_shared_lifecycle_and_outer_route(self):
        for binding in LOCAL_BINDINGS:
            with self.subTest(method_name=binding.lifecycle.method_name):
                with patch.object(
                    binding.adapter,
                    "build_method_graph",
                    wraps=binding.adapter.build_method_graph,
                ) as build_method_graph:
                    method_graph = binding.method_builder()

                build_method_graph.assert_called_once()
                self.assertIs(build_method_graph.call_args.args[0], binding.lifecycle)
                self.assertIsNotNone(binding.lifecycle.outcome_policy)
                self.assertTrue(
                    {"freeze", "run", "collect"}.issubset(
                        method_graph.get_graph().nodes
                    )
                )
                self.assertIn(
                    binding.outer_node, binding.stage_builder().get_graph().nodes
                )

    def test_each_frozen_local_method_graph_runs_its_exact_lifecycle(self):
        for binding in LOCAL_BINDINGS:
            with self.subTest(method_name=binding.lifecycle.method_name):
                workflow_steps = [
                    {
                        "step_id": workflow_step_id(0, "inventory"),
                        "stage": "inventory",
                        "allow_local_execution": False,
                        "selected_method_names": [],
                    }
                ]
                if binding.lifecycle.stage == "assembly_review":
                    workflow_steps.append(
                        {
                            "step_id": workflow_step_id(1, "assembly"),
                            "stage": "assembly",
                            "allow_local_execution": False,
                            "selected_method_names": [],
                        }
                    )
                target_step_index = len(workflow_steps)
                target_step = {
                    "step_id": workflow_step_id(
                        target_step_index, binding.lifecycle.stage
                    ),
                    "stage": binding.lifecycle.stage,
                    "allow_local_execution": True,
                    "selected_method_names": [binding.lifecycle.method_name],
                }
                if binding.lifecycle.stage == "assembly_review":
                    target_step["review_target"] = {
                        "resource_type": "assembly",
                        "producer_step_id": workflow_step_id(1, "assembly"),
                    }
                workflow_steps.append(target_step)
                state = initial_state(
                    session_id="lifecycle-invariant",
                    input_manifest_ref="inputs/manifest.yaml",
                    workflow_steps=workflow_steps,
                )
                state["workflow_step_index"] = target_step_index
                state["entered_workflow_step_index"] = target_step_index
                state["current_stage"] = binding.lifecycle.stage
                state["stage_outputs"] = {
                    binding.lifecycle.stage: {"status": binding.lifecycle.frozen_status}
                }
                with (
                    patch(
                        "app.execution.lifecycle.run_method_attempts",
                        return_value={
                            "stage_outputs": {
                                binding.lifecycle.stage: {"status": "failed"}
                            }
                        },
                    ) as run_method_attempts,
                    patch(
                        "app.execution.method_outcomes.settle_method_outcomes",
                        return_value={},
                    ) as settle_method_outcomes,
                ):
                    result = binding.method_builder().invoke(state)

                run_method_attempts.assert_called_once()
                settle_method_outcomes.assert_called_once()
                self.assertIs(run_method_attempts.call_args.args[1], binding.lifecycle)
                self.assertEqual(
                    result["stage_outputs"][binding.lifecycle.stage]["status"],
                    "failed",
                )

    def test_planner_only_filter_selection_skips_process_and_preserves_raw_paths(self):
        planner_catalog = build_planner_method_catalog(
            DescriptorRegistry.load(Path("descriptors/registry.yaml"))
        )
        planner_only = next(
            descriptor
            for descriptor in planner_catalog.by_method.values()
            if descriptor["execution_kind"] == "planner_only"
        )
        canonical_method = planner_only["method_name"]
        self.assertEqual(canonical_method, READ_QC_FILTER_SKIP)

        plan = AskPlanState(
            session_id="planner-only-invariant",
            data_dir="data",
            entry_request={"command": "run", "manifest": "inputs/manifest.yaml"},
            manifest_ref="inputs/manifest.yaml",
            selected_stage_occurrences=[
                PlanStageOccurrence(stage="read_qc", allow_local_execution=True)
            ],
            selected_methods={"read_qc": [READ_QC_STATS_METHOD, canonical_method]},
            confirmed=True,
        )
        filter_config = build_run_config_payload(plan)["read_qc"]["length_filter"]
        self.assertEqual(filter_config["method"], LENGTH_FILTER_SKIP)

        raw_paths = ["/inputs/reads_1.fastq.gz", "/inputs/reads_2.fastq.gz"]
        state = initial_state(
            session_id="planner-only-invariant",
            input_manifest_ref="inputs/manifest.yaml",
            workflow_steps=[
                {
                    "step_id": workflow_step_id(0, "inventory"),
                    "stage": "inventory",
                    "allow_local_execution": False,
                    "selected_method_names": [],
                },
                {
                    "step_id": workflow_step_id(1, "read_qc"),
                    "stage": "read_qc",
                    "allow_local_execution": True,
                    "selected_method_names": [
                        READ_QC_STATS_METHOD,
                        canonical_method,
                    ],
                },
            ],
        )
        state["workflow_step_index"] = 1
        state["entered_workflow_step_index"] = 1
        state["current_stage"] = "read_qc"
        state["stage_outputs"] = {
            "read_qc": {
                "status": "length_filter_pending",
                "length_filter_request": filter_config,
                "raw_long_reads_paths": raw_paths,
            }
        }
        planned = plan_length_filter(state)
        state["stage_outputs"] = planned["stage_outputs"]

        with (
            patch.object(
                seqkit_length_filter,
                "build_frozen_attempt",
                wraps=seqkit_length_filter.build_frozen_attempt,
            ) as build_frozen_attempt,
            patch(
                "app.execution.lifecycle.run_method_attempts",
                wraps=None,
            ) as run_method_attempts,
        ):
            result = seqkit_length_filter.build_seqkit_length_filter_graph().invoke(
                state
            )

        self.assertEqual(
            result["stage_outputs"]["read_qc"]["length_filter_method"],
            LENGTH_FILTER_SKIP,
        )
        self.assertEqual(
            result["stage_outputs"]["read_qc"]["raw_long_reads_paths"], raw_paths
        )
        build_frozen_attempt.assert_not_called()
        run_method_attempts.assert_not_called()

        committed = commit_read_qc(result)
        self.assertEqual(
            committed["stage_outputs"]["read_qc"]["raw_long_reads_paths"], raw_paths
        )
        self.assertEqual(committed["resource_refs"]["long_reads"]["paths"], raw_paths)

    def test_planner_only_filter_events_are_scoped_to_the_filter_method(self):
        with TemporaryDirectory() as tmpdir:
            session_id = "planner-only-events"
            data_dir = Path(tmpdir) / "runs" / session_id
            ledger = Ledger(data_dir)
            ledger.create_session(session_id, "inputs/manifest.yaml")
            stats_attempt = FrozenAttemptSpec(
                attempt_id=f"{session_id}:stats",
                session_id=session_id,
                stage="read_qc",
                method_name=READ_QC_STATS_METHOD,
                script_path=(data_dir / "run.sh").resolve(),
                script_sha256="0" * 64,
                working_dir=data_dir.resolve(),
                expected_outputs=[],
                input_dependencies=[],
                metadata={"workflow_step_id": workflow_step_id(1, "read_qc")},
            )
            ledger.record_attempt_started(session_id, stats_attempt)
            ledger.record_attempt_succeeded(session_id, stats_attempt)

            state = initial_state(
                session_id=session_id,
                input_manifest_ref="inputs/manifest.yaml",
                data_dir=str(data_dir),
                workflow_steps=[
                    {
                        "step_id": workflow_step_id(0, "inventory"),
                        "stage": "inventory",
                        "allow_local_execution": False,
                        "selected_method_names": [],
                    },
                    {
                        "step_id": workflow_step_id(1, "read_qc"),
                        "stage": "read_qc",
                        "allow_local_execution": True,
                        "selected_method_names": [
                            READ_QC_STATS_METHOD,
                            READ_QC_FILTER_SKIP,
                        ],
                    },
                ],
            )
            state["workflow_step_index"] = 1
            state["entered_workflow_step_index"] = 1
            state["current_stage"] = "read_qc"
            state["stage_outputs"] = {
                "read_qc": {
                    "status": "length_filter_pending",
                    "length_filter_request": {"method": LENGTH_FILTER_SKIP},
                    "raw_long_reads_paths": ["/inputs/reads.fastq.gz"],
                }
            }
            planned = plan_length_filter(state)
            state["stage_outputs"] = planned["stage_outputs"]
            with (
                patch.object(
                    seqkit_length_filter,
                    "build_frozen_attempt",
                    wraps=seqkit_length_filter.build_frozen_attempt,
                ) as build_frozen_attempt,
                patch("app.execution.lifecycle.run_method_attempts") as run_attempts,
            ):
                routed = seqkit_length_filter.build_seqkit_length_filter_graph().invoke(
                    state
                )
            committed = commit_read_qc(routed)

            self.assertEqual(
                committed["stage_outputs"]["read_qc"]["length_filter_method"],
                LENGTH_FILTER_SKIP,
            )
            build_frozen_attempt.assert_not_called()
            run_attempts.assert_not_called()

            events = ledger.read_events(session_id)
            filter_method_names = {
                READ_QC_FILTER_SKIP,
                seqkit_length_filter.LIFECYCLE.method_name,
            }
            filter_events = [
                event
                for event in events
                if event.get("method_name") in filter_method_names
                and event["event_type"] in {"attempt_started", "attempt_succeeded"}
            ]
            stats_events = [
                event
                for event in events
                if event.get("method_name") == READ_QC_STATS_METHOD
                and event["event_type"] in {"attempt_started", "attempt_succeeded"}
            ]

        self.assertEqual(filter_events, [])
        self.assertEqual(
            {event["event_type"] for event in stats_events},
            {"attempt_started", "attempt_succeeded"},
        )
