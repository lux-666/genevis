"""Crash-window tests for immutable occurrence settlement replay."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.occurrence_settlement import settle_workflow_step_outcome
from app.schemas.artifacts import ArtifactMeta
from app.schemas.state import initial_state
from app.storage.artifacts import publish_artifact_record
from app.storage.ledger import Ledger
from tests.data_v2 import v2_run_dir


class OccurrenceSettlementReplayTests(unittest.TestCase):
    def test_delivery_replays_after_crash_before_stage_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(Path(tmp), "s1")
            Ledger(run_dir).create_session("s1", "control/input_manifest.yaml")
            work_dir = run_dir / "work" / "steps" / "step-001-assembly" / "assembly"
            candidate_path = work_dir / "candidate.json"
            primary_path = work_dir / "assembly.fasta"
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text("candidate\n", encoding="utf-8")
            primary_path.write_text(">contig\nACGT\n", encoding="utf-8")
            candidate = publish_artifact_record(
                run_dir,
                "s1",
                ArtifactMeta.from_file(
                    candidate_path,
                    stage="assembly",
                    method_name="assembly.candidate.flye",
                    artifact_type="assembly.candidate_bundle",
                ),
            )
            primary = publish_artifact_record(
                run_dir,
                "s1",
                ArtifactMeta.from_file(
                    primary_path,
                    stage="assembly",
                    method_name="assembly.candidate.flye",
                    artifact_type="assembly.draft_assembly",
                ),
            )
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/input_manifest.yaml",
                data_dir=str(run_dir),
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
                        "allow_local_execution": False,
                        "selected_method_names": ["assembly.candidate.flye"],
                    },
                ],
                stage_plan_ref="control/run_config.json",
            )
            state.update(
                {
                    "current_stage": "done",
                    "workflow_step_index": 2,
                    "entered_workflow_step_index": 1,
                    "workflow_step_outcome": {
                        "workflow_step_id": "step-001-assembly",
                        "stage": "assembly",
                        "outcome": "completed",
                        "deliveries": [
                            {
                                "resource_type": "assembly",
                                "delivery_mode": "materialized",
                                "candidate_bundle_ref": candidate.ref.model_dump(
                                    mode="json"
                                ),
                                "primary_output_ref": primary.ref.model_dump(
                                    mode="json"
                                ),
                            }
                        ],
                    },
                }
            )
            Ledger(run_dir).record_method_disposition(
                "s1",
                "assembly",
                "step-001-assembly",
                "assembly.candidate.flye",
                "completed",
            )

            original = Ledger.record_stage_completed
            crashed = False

            def crash_once(self, *args, **kwargs):
                nonlocal crashed
                if not crashed:
                    crashed = True
                    raise RuntimeError("simulated crash after delivery")
                return original(self, *args, **kwargs)

            with patch(
                "app.control.occurrence_settlement._validate_assembly_delivery_projection",
                return_value=(candidate.ref, primary.ref),
            ), patch.object(Ledger, "record_stage_completed", crash_once):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    settle_workflow_step_outcome(state, data_dir=run_dir)
                settle_workflow_step_outcome(state, data_dir=run_dir)

            events = Ledger(run_dir).read_events("s1")
            self.assertEqual(
                [event["event_type"] for event in events].count("resource_delivered"),
                1,
            )
            self.assertEqual(
                [event["event_type"] for event in events].count("stage_completed"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
