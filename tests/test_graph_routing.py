"""Tests for parent graph routing boundaries."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from app.graph.builder import build_graph
from app.graph.routing import route_controller
from app.schemas.artifacts import ArtifactMeta
from app.schemas.state import initial_state
from app.storage.artifacts import publish_artifact_record
from app.storage.ledger import Ledger
from app.workflow import (
    STAGE_ORDER,
    next_stage_name,
    skipped_stages_until_next,
    unrequested_stages_from_current,
)
from tests.data_v2 import v2_run_dir


def _publish_empty_asset_registry(run_dir, session_id: str) -> dict:
    source = run_dir / "work" / "inventory" / "asset_registry.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps({"schema_version": 1, "session_id": session_id, "assets": []}),
        encoding="utf-8",
    )
    artifact = ArtifactMeta.from_file(
        source,
        stage="inventory",
        method_name="inventory.register_assets",
        artifact_type="asset_registry",
    )
    return publish_artifact_record(run_dir, session_id, artifact).ref.model_dump(
        mode="json"
    )


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
            state["asset_registry_ref"] = _publish_empty_asset_registry(run_dir, "s1")
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

    def test_empty_requested_stages_end_after_inventory(self):
        self.assertEqual(next_stage_name("inventory", []), "done")
        self.assertEqual(next_stage_name("inventory", None), "read_qc")
        self.assertEqual(
            skipped_stages_until_next("inventory", []),
            STAGE_ORDER[1:],
        )
        self.assertEqual(
            unrequested_stages_from_current("read_qc", []),
            STAGE_ORDER[1:],
        )
        self.assertEqual(
            unrequested_stages_from_current("read_qc", None),
            [],
        )

        inventory_update = route_controller(
            {
                "supervision_state": "idle",
                "current_stage": "inventory",
                "requested_stages": [],
            }
        )
        self.assertNotIn("current_stage", inventory_update)

        update = route_controller(
            {
                "supervision_state": "idle",
                "current_stage": "read_qc",
                "requested_stages": [],
            }
        )

        self.assertEqual(update["current_stage"], "done")

    def test_empty_requested_stages_complete_after_inventory_with_durable_evidence(
        self,
    ):
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
            ledger = Ledger(run_dir)
            ledger.create_session("empty-plan", "control/manifest.yaml")
            state = initial_state(
                session_id="empty-plan",
                input_manifest_ref="control/manifest.yaml",
                data_dir=str(run_dir),
                requested_stages=[],
            )

            result = build_graph(
                backend="memory",
                data_dir=str(run_dir),
            ).invoke(
                state,
                config={"configurable": {"thread_id": "empty-plan"}},
            )

            self.assertEqual(result["current_stage"], "done")
            self.assertEqual(result["supervision_state"], "completed")
            self.assertEqual(
                result["stage_outputs"]["inventory"]["status"],
                "completed",
            )
            self.assertNotIn("read_qc", result["stage_outputs"])

            rows = ledger.read_event_rows("empty-plan")
            skipped = [row for row in rows if row["event_type"] == "stage_skipped"]
            self.assertEqual([row["stage"] for row in skipped], STAGE_ORDER[1:])
            self.assertTrue(
                all(row["summary"]["reason"] == "not_requested" for row in skipped)
            )
            self.assertFalse(
                any(
                    row["event_type"] == "stage_entered"
                    and row["stage"] == "read_qc"
                    for row in rows
                )
            )
            terminals = [
                row for row in rows if row["event_type"] == "session_terminal"
            ]
            self.assertEqual(len(terminals), 1)
            self.assertEqual(terminals[0]["summary"]["outcome"], "completed")
            self.assertEqual(terminals[0]["stage"], "done")

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
            STAGE_ORDER[
                STAGE_ORDER.index("assembly_review") + 1 : STAGE_ORDER.index(
                    "repeat_annotation"
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
