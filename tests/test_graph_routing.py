"""Tests for routing from the frozen occurrence plan."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.projections import ProgressProjectionInput
from app.control.workflow_plan import fail_workflow_step, workflow_steps_from_run_config
from app.graph.builder import build_graph
from app.graph.routing import route_controller, route_from_current_stage
from app.schemas.run_config import RunConfig
from app.schemas.state import initial_state
from app.storage.frozen_inputs import (
    FrozenInputBundle,
    freeze_session_inputs,
    validate_frozen_inputs,
)
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.busco_fixture import assembly_review_config
from tests.data_v2 import v2_run_dir


def _freeze_verified_inputs(
    run_dir: Path,
    manifest: Path,
    run_config: RunConfig,
) -> tuple[SessionPaths, FrozenInputBundle]:
    paths = SessionPaths.from_run_dir(run_dir)
    envs = paths.control_dir / "test_envs.yaml"
    envs.write_text(
        "default:\n  fallback_env: test-env\npackages: {}\n",
        encoding="utf-8",
    )
    freeze_session_inputs(
        paths,
        manifest_draft=manifest,
        run_config_draft=run_config,
        config_path=envs,
        probe_environments=False,
    )
    return paths, validate_frozen_inputs(paths)


class GraphRoutingTests(unittest.TestCase):
    def test_router_dispatches_and_seeds_only_the_current_occurrence(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "t2t", "allow_local_execution": False},
                    {"stage": "assembly_review", "allow_local_execution": True},
                ],
                "assembly_review": assembly_review_config(
                    "/data/busco/embryophyta_odb10"
                ),
            }
        )
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            workflow_steps=workflow_steps_from_run_config(config),
            stage_seeds={"inventory": {"seed": "inventory"}},
        )

        update = route_controller(state)

        self.assertEqual(update["entered_workflow_step_index"], 0)
        self.assertEqual(update["stage_outputs"]["inventory"], {"seed": "inventory"})
        routed = {**state, **update}
        self.assertEqual(route_from_current_stage(routed), "inventory_stage")

    def test_inventory_only_plan_completes_without_synthesized_stage_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "empty-plan")
            reads = run_dir / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = run_dir / "control" / "manifest.yaml"
            manifest.write_text(
                json.dumps(
                    {
                        "session_id": "empty-plan",
                        "assets": [
                            {
                                "asset_type": "raw_long_reads",
                                "paths": [str(reads)],
                                "platform": "ont",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = RunConfig.model_validate({"requested_stages": []})
            paths, verified_inputs = _freeze_verified_inputs(
                run_dir,
                manifest,
                config,
            )
            input_manifest_ref = paths.to_ref(paths.input_manifest_path)
            ledger = Ledger(run_dir)
            ledger.create_session("empty-plan", input_manifest_ref)
            state = initial_state(
                session_id="empty-plan",
                input_manifest_ref=input_manifest_ref,
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(
                    verified_inputs.run_config
                ),
            )

            result = build_graph(
                backend="memory",
                data_dir=str(run_dir),
                verified_inputs=verified_inputs,
            ).invoke(
                state,
                config={"configurable": {"thread_id": "empty-plan"}},
            )

            self.assertEqual(result["current_stage"], "done")
            self.assertEqual(result["supervision_state"], "completed")
            self.assertEqual(result["workflow_step_index"], 1)
            self.assertEqual(
                result["route_trace"],
                ["step-000-inventory:inventory_stage"],
            )
            rows = ledger.read_event_rows("empty-plan")
            self.assertFalse(any(row["event_type"] == "stage_skipped" for row in rows))
            self.assertFalse(
                any(row["event_type"] == "session_terminal" for row in rows)
            )

    def test_advanced_skipped_occurrence_has_only_one_terminal_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "survey-skip-terminal")
            rnaseq = run_dir / "rna.fastq"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = run_dir / "control" / "manifest.yaml"
            manifest.write_text(
                json.dumps(
                    {
                        "session_id": "survey-skip-terminal",
                        "genome_size_estimate": 2_000_000,
                        "assets": [
                            {
                                "asset_type": "raw_rnaseq_reads",
                                "paths": [str(rnaseq)],
                                "platform": "illumina",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = RunConfig.model_validate(
                {
                    "requested_stages": [
                        {
                            "stage": "genome_survey",
                            "allow_local_execution": False,
                        }
                    ]
                }
            )
            paths, verified_inputs = _freeze_verified_inputs(
                run_dir,
                manifest,
                config,
            )
            input_manifest_ref = paths.to_ref(paths.input_manifest_path)
            ledger = Ledger(run_dir)
            ledger.create_session("survey-skip-terminal", input_manifest_ref)
            state = initial_state(
                session_id="survey-skip-terminal",
                input_manifest_ref=input_manifest_ref,
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(
                    verified_inputs.run_config
                ),
            )

            result = build_graph(
                backend="memory",
                data_dir=str(run_dir),
                verified_inputs=verified_inputs,
            ).invoke(
                state,
                config={"configurable": {"thread_id": "survey-skip-terminal"}},
            )

            self.assertEqual(result["supervision_state"], "completed")
            terminals = [
                row
                for row in ledger.read_event_rows("survey-skip-terminal")
                if row.get("workflow_step_id") == "step-001-genome_survey"
                and row["event_type"]
                in {"stage_failed", "stage_skipped", "stage_completed"}
            ]
            self.assertEqual(
                [row["event_type"] for row in terminals],
                ["stage_skipped"],
            )
            self.assertEqual(
                terminals[0]["summary"]["reason"],
                "no_usable_survey_reads",
            )

    def test_failed_occurrence_takes_precedence_over_same_update_skip_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "failed-over-skip")
            reads = run_dir / "assembly.fa"
            reads.write_text(">ctg\nACGT\n", encoding="utf-8")
            manifest = run_dir / "control" / "manifest.yaml"
            manifest.write_text(
                json.dumps(
                    {
                        "session_id": "failed-over-skip",
                        "assets": [
                            {
                                "asset_type": "assembly_fasta_registered",
                                "paths": [str(reads)],
                                "format": "fasta",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "gapcloser", "allow_local_execution": False}
                    ]
                }
            )
            paths, verified_inputs = _freeze_verified_inputs(
                run_dir,
                manifest,
                config,
            )
            input_manifest_ref = paths.to_ref(paths.input_manifest_path)
            ledger = Ledger(run_dir)
            ledger.create_session("failed-over-skip", input_manifest_ref)
            state = initial_state(
                session_id="failed-over-skip",
                input_manifest_ref=input_manifest_ref,
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(
                    verified_inputs.run_config
                ),
            )

            def fail_with_skip(_state):
                from app.execution.method_outcomes import (
                    MethodOutcome,
                    settle_method_outcomes,
                )

                settle_method_outcomes(
                    _state,
                    stage="gapcloser",
                    outcomes=MethodOutcome(
                        "gapcloser.tgs_gapcloser",
                        "not_started",
                        reason="synthetic stage failure",
                    ),
                )
                return {
                    **fail_workflow_step(
                        _state,
                        "gapcloser",
                        "synthetic stage failure",
                    ),
                    "skip_reasons": {"step-001-gapcloser": "stale synthetic skip"},
                }

            with patch(
                "app.graph.builder.build_gapcloser_stage",
                return_value=fail_with_skip,
            ):
                result = build_graph(
                    backend="memory",
                    data_dir=str(run_dir),
                    verified_inputs=verified_inputs,
                ).invoke(
                    state,
                    config={"configurable": {"thread_id": "failed-over-skip"}},
                )

            self.assertEqual(result["supervision_state"], "failed_terminal")
            terminals = [
                row
                for row in ledger.read_event_rows("failed-over-skip")
                if row.get("workflow_step_id") == "step-001-gapcloser"
                and row["event_type"]
                in {"stage_failed", "stage_skipped", "stage_completed"}
            ]
            self.assertEqual(
                [row["event_type"] for row in terminals],
                ["stage_failed"],
            )

    def test_attached_progress_observes_ledger_after_each_controller_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "attached-progress")
            reads = run_dir / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = run_dir / "control" / "manifest.yaml"
            manifest.write_text(
                json.dumps(
                    {
                        "session_id": "attached-progress",
                        "assets": [
                            {
                                "asset_type": "raw_long_reads",
                                "paths": [str(reads)],
                                "platform": "ont",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = RunConfig.model_validate({"requested_stages": []})
            paths, verified_inputs = _freeze_verified_inputs(
                run_dir,
                manifest,
                config,
            )
            input_manifest_ref = paths.to_ref(paths.input_manifest_path)
            ledger = Ledger(run_dir)
            ledger.create_session("attached-progress", input_manifest_ref)
            state = initial_state(
                session_id="attached-progress",
                input_manifest_ref=input_manifest_ref,
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(
                    verified_inputs.run_config
                ),
            )
            observations: list[tuple[int, str, list[str]]] = []

            def observe_progress(progress: ProgressProjectionInput) -> None:
                observations.append(
                    (
                        progress.workflow_step_index,
                        progress.supervision_state,
                        [
                            row["event_type"]
                            for row in ledger.read_event_rows("attached-progress")
                        ],
                    )
                )

            result = build_graph(
                backend="memory",
                data_dir=str(run_dir),
                verified_inputs=verified_inputs,
                on_controller_transition=observe_progress,
            ).invoke(
                state,
                config={"configurable": {"thread_id": "attached-progress"}},
            )

            self.assertEqual(result["supervision_state"], "completed")
            self.assertEqual(len(observations), 2)
            self.assertEqual(observations[0][0:2], (0, "idle"))
            self.assertIn("stage_entered", observations[0][2])
            self.assertNotIn("stage_completed", observations[0][2])
            self.assertEqual(observations[1][0:2], (1, "completed"))
            self.assertIn("stage_completed", observations[1][2])
            self.assertNotIn("session_terminal", observations[1][2])

    def test_graph_runs_two_occurrences_of_the_same_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "repeat-read-qc")
            reads = run_dir / "reads.fastq"
            reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = run_dir / "control" / "manifest.yaml"
            manifest.write_text(
                json.dumps(
                    {
                        "session_id": "repeat-read-qc",
                        "assets": [
                            {
                                "asset_type": "raw_long_reads",
                                "paths": [str(reads)],
                                "platform": "ont",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "read_qc", "allow_local_execution": False},
                        {"stage": "read_qc", "allow_local_execution": False},
                    ]
                }
            )
            paths, verified_inputs = _freeze_verified_inputs(
                run_dir,
                manifest,
                config,
            )
            input_manifest_ref = paths.to_ref(paths.input_manifest_path)
            ledger = Ledger(run_dir)
            ledger.create_session("repeat-read-qc", input_manifest_ref)
            state = initial_state(
                session_id="repeat-read-qc",
                input_manifest_ref=input_manifest_ref,
                data_dir=str(run_dir),
                workflow_steps=workflow_steps_from_run_config(
                    verified_inputs.run_config
                ),
            )

            recorded_completed_steps: list[str] = []
            original_record_stage_completed = Ledger.record_stage_completed

            def record_stage_completed_once(
                ledger: Ledger,
                session_id: str,
                stage: str,
                *,
                workflow_step_id: str,
                **kwargs: object,
            ) -> None:
                recorded_completed_steps.append(workflow_step_id)
                original_record_stage_completed(
                    ledger,
                    session_id,
                    stage,
                    workflow_step_id=workflow_step_id,
                    **kwargs,
                )

            with patch.object(
                Ledger,
                "record_stage_completed",
                new=record_stage_completed_once,
            ):
                result = build_graph(
                    backend="memory",
                    data_dir=str(run_dir),
                    verified_inputs=verified_inputs,
                ).invoke(
                    state,
                    config={"configurable": {"thread_id": "repeat-read-qc"}},
                )

            self.assertEqual(result["supervision_state"], "completed")
            self.assertEqual(
                result["route_trace"],
                [
                    "step-000-inventory:inventory_stage",
                    "step-001-read_qc:read_qc_stage",
                    "step-002-read_qc:read_qc_stage",
                ],
            )
            completed = [
                row
                for row in ledger.read_event_rows("repeat-read-qc")
                if row["event_type"] == "stage_completed" and row["stage"] == "read_qc"
            ]
            self.assertEqual(
                [row["workflow_step_id"] for row in completed],
                ["step-001-read_qc", "step-002-read_qc"],
            )
            self.assertEqual(
                recorded_completed_steps,
                [
                    "step-000-inventory",
                    "step-001-read_qc",
                    "step-002-read_qc",
                ],
            )


if __name__ == "__main__":
    unittest.main()
