"""Tests for parent graph routing boundaries."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from app.control.ledger import Ledger
from app.graph.builder import build_graph
from app.graph.routing import (
    next_stage_name,
    route_controller,
    skipped_stages_until_next,
)
from app.schemas.state import initial_state
from tests.data_v2 import v2_run_dir


class GraphRoutingTests(unittest.TestCase):
    def test_requested_stages_skip_downstream_after_assembly_goal(self):
        update = route_controller(
            {
                "supervision_state": "idle",
                "current_stage": "assembly_review",
                "requested_stages": ["read_qc", "assembly"],
            }
        )

        self.assertEqual(update["current_stage"], "done")

    def test_parent_bypasses_unrequested_hic_without_entering_subgraph(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = v2_run_dir(tmp, "s1")
            ledger = Ledger(run_dir)
            ledger.create_session("s1", "control/manifest.yaml")
            state = initial_state(
                session_id="s1",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(run_dir),
                requested_stages=["t2t"],
            )
            state["current_stage"] = "hic_scaffolding"
            state["supervision_state"] = "idle"

            with patch("app.reports.review_report.write_operator_report"):
                result = build_graph(
                    backend="memory",
                    data_dir=str(run_dir),
                ).invoke(
                    state,
                    config={"configurable": {"thread_id": "s1"}},
                )

            self.assertNotIn("hic_scaffolding", result.get("stage_outputs") or {})
            skip_rows = [
                row
                for row in ledger.read_event_rows("s1")
                if row.get("event_type") == "stage_skipped"
                and row.get("stage") == "hic_scaffolding"
            ]
            self.assertEqual(len(skip_rows), 1)
            self.assertEqual(skip_rows[0]["summary"]["reason"], "not_requested")

    def test_none_requested_stages_preserves_full_pipeline_semantics(self):
        update = route_controller(
            {
                "supervision_state": "idle",
                "current_stage": "hic_scaffolding",
                "requested_stages": None,
            }
        )

        self.assertNotIn("current_stage", update)

    def test_resume_boundary_selects_next_requested_stage(self):
        self.assertEqual(
            next_stage_name(
                "assembly_review",
                [
                    "assembly_review",
                    "repeat_annotation",
                    "gene_annotation",
                    "function_annotation",
                ],
            ),
            "repeat_annotation",
        )
        self.assertEqual(
            skipped_stages_until_next(
                "assembly_review",
                [
                    "assembly_review",
                    "repeat_annotation",
                    "gene_annotation",
                    "function_annotation",
                ],
            ),
            ["hic_scaffolding", "t2t"],
        )


if __name__ == "__main__":
    unittest.main()
