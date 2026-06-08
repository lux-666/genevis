"""Tests for annotation intake planning after assembly review."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.execution.artifacts import read_artifact_records
from app.review.annotation_intake import (
    ANNOTATION_INTAKE_PLAN,
    build_recommended_annotation_plan,
    classify_assembly_review_decision,
    maybe_collect_annotation_intake,
    persist_annotation_intake_plan,
    rewrite_requested_stages_after_assembly_review,
)
from app.review.base import ResumeReviewRequest


def _manifest(path: Path, session_id: str) -> Path:
    manifest_ref = path / "manifest.json"
    manifest_ref.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "assets": [
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(path / "reads.fastq")],
                        "platform": "ont",
                        "format": "fastq",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_ref


def _state(data_dir: Path, session_id: str, requested_stages: list[str] | None) -> dict:
    manifest_ref = _manifest(data_dir, session_id)
    return {
        "session_id": session_id,
        "thread_id": session_id,
        "data_dir": str(data_dir),
        "input_manifest_ref": str(manifest_ref),
        "supervision_state": "blocked_human",
        "current_stage": "assembly_review",
        "requested_stages": requested_stages,
        "active_interrupt": {
            "stage": "assembly_review",
            "gate_id": "assembly_review",
            "decisions_allowed": ["accept", "pass", "continue_low_confidence", "rerun", "stop_human_review"],
        },
        "stage_outputs": {
            "assembly_review": {"status": "blocked_human"},
            "assembly": {"status": "completed"},
        },
        "route_trace": ["inventory_stage", "read_qc_stage", "assembly_stage"],
        "review_notes": [],
    }


def _request(session_id: str, decision: str = "accept") -> ResumeReviewRequest:
    return ResumeReviewRequest(
        session_id=session_id,
        stage="assembly_review",
        gate_id="assembly_review",
        decision=decision,
        candidate_id="flye",
        final_assembly_ref="/tmp/draft.fasta",
        decided_by="human",
    )


def _diamond_provenance() -> list[dict[str, str]]:
    return [
        {
            "method_name": "function_annotation.candidate.diamond",
            "database_name": "UniProt",
            "database_version": "2026_02",
            "snapshot_date": "2026-05-16",
            "database_source_ref": "/db/uniprot/2026_02.dmnd",
            "descriptor_version": "diamond.yaml",
            "input_proteins_ref": "/data/proteins.fa",
        }
    ]


class AnnotationIntakeTests(unittest.TestCase):
    def test_decision_classifier_only_continue_downstream_triggers_intake(self):
        self.assertEqual(classify_assembly_review_decision("accept"), "continue_downstream")
        self.assertEqual(classify_assembly_review_decision("continue_low_confidence"), "continue_downstream")
        self.assertEqual(classify_assembly_review_decision("rerun"), "assembly_operation")
        self.assertEqual(classify_assembly_review_decision("stop_human_review"), "terminate")

    def test_rewrite_preserves_prefix_and_replaces_downstream_suffix(self):
        requested = ["read_qc", "assembly", "assembly_review", "repeat_annotation"]
        rewritten = rewrite_requested_stages_after_assembly_review(
            requested,
            ["hic_scaffolding", "gene_annotation"],
        )
        self.assertEqual(
            rewritten,
            ["read_qc", "assembly", "assembly_review", "hic_scaffolding", "gene_annotation"],
        )

    def test_recommendation_uses_manifest_when_plan_stops_at_assembly_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            state = _state(data_dir, "s1", ["read_qc", "assembly", "assembly_review"])

            plan = build_recommended_annotation_plan(state)

            self.assertEqual(
                plan.resolved_execution_order,
                ["repeat_annotation", "gene_annotation", "function_annotation"],
            )
            self.assertNotIn("assembly_review", plan.resolved_execution_order)

    def test_accept_as_is_persists_intake_artifact_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            session_id = "s2"
            Ledger(data_dir).create_session(session_id, "manifest.json")
            state = _state(data_dir, session_id, ["read_qc", "assembly", "assembly_review"])
            resume_update = {
                "supervision_state": "running_stage",
                "active_interrupt": None,
                "stage_outputs": {
                    **state["stage_outputs"],
                    "assembly_review": {
                        "status": "completed",
                        "final_assembly_ref": "/tmp/draft.fasta",
                    },
                },
            }

            result = maybe_collect_annotation_intake(
                state=state,
                resume_update=resume_update,
                request=_request(session_id),
                source="test",
                interactive=False,
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(
                result.final_requested_stages,
                ["read_qc", "assembly", "assembly_review", "repeat_annotation", "gene_annotation", "function_annotation"],
            )
            self.assertFalse(result.changed)
            self.assertEqual(result.update["requested_stages"], result.final_requested_stages)
            self.assertEqual(
                result.update["stage_outputs"]["assembly_review"]["annotation_intake_plan_ref"],
                result.artifact_ref,
            )

            payload = json.loads(Path(result.artifact_ref).read_text(encoding="utf-8"))
            self.assertEqual(payload["artifact_type"], ANNOTATION_INTAKE_PLAN)
            self.assertFalse(payload["changed"])
            self.assertEqual(payload["decision"]["decision"], "accept")

            artifact_types = [record["artifact_type"] for record in read_artifact_records(data_dir, session_id)]
            self.assertIn(ANNOTATION_INTAKE_PLAN, artifact_types)
            events = Ledger(data_dir).read_events(session_id)
            self.assertTrue(any(event["event_type"] == "annotation_intake_planned" for event in events))

    def test_override_can_add_downstream_stage_not_in_original_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            session_id = "s3"
            Ledger(data_dir).create_session(session_id, "manifest.json")
            state = _state(data_dir, session_id, ["read_qc", "assembly", "assembly_review", "repeat_annotation"])
            recommended = build_recommended_annotation_plan(state)
            final = recommended.model_copy(
                update={
                    "selected_stages": ["hic_scaffolding", "repeat_annotation"],
                    "resolved_execution_order": ["hic_scaffolding", "repeat_annotation"],
                    "selected_methods": {
                        "hic_scaffolding": "hic_scaffolding.candidate.yahs",
                        "repeat_annotation": ["repeat_annotation.masking.repeatmasker"],
                    },
                    "confirmed": True,
                }
            )

            result = persist_annotation_intake_plan(
                state=state,
                recommended_plan=recommended,
                final_plan=final,
                request=_request(session_id),
                source="test",
            )

            self.assertTrue(result.changed)
            self.assertEqual(
                result.final_requested_stages,
                ["read_qc", "assembly", "assembly_review", "hic_scaffolding", "repeat_annotation"],
            )
            self.assertEqual(
                result.update["stage_outputs"]["hic_scaffolding"]["primary_method"],
                "hic_scaffolding.candidate.yahs",
            )
            self.assertEqual(
                result.update["stage_outputs"]["repeat_annotation"]["primary_method"],
                "repeat_annotation.masking.repeatmasker",
            )

    def test_override_persists_function_database_provenance_to_checkpoint_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            session_id = "s6"
            Ledger(data_dir).create_session(session_id, "manifest.json")
            state = _state(data_dir, session_id, ["read_qc", "assembly", "assembly_review"])
            provenance = _diamond_provenance()
            recommended = build_recommended_annotation_plan(state)
            final = recommended.model_copy(
                update={
                    "selected_stages": ["function_annotation"],
                    "resolved_execution_order": ["function_annotation"],
                    "selected_methods": {"function_annotation": ["function_annotation.candidate.diamond"]},
                    "stage_parameters": {"function_annotation": {"database_provenance": provenance}},
                    "confirmed": True,
                }
            )

            result = persist_annotation_intake_plan(
                state=state,
                recommended_plan=recommended,
                final_plan=final,
                request=_request(session_id),
                source="test",
            )

            function_out = result.update["stage_outputs"]["function_annotation"]
            self.assertEqual(function_out["database_provenance"], provenance)
            self.assertEqual(function_out["execution_mode"], "local")

    def test_override_clears_omitted_downstream_suffix_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            session_id = "s5"
            Ledger(data_dir).create_session(session_id, "manifest.json")
            state = _state(
                data_dir,
                session_id,
                ["read_qc", "assembly", "assembly_review", "repeat_annotation", "gene_annotation", "function_annotation"],
            )
            state["skip_reasons"] = {
                "repeat_annotation": "old skip",
                "gene_annotation": "old skip",
                "assembly": "upstream preserved",
            }
            state["stage_outputs"].update(
                {
                    "repeat_annotation": {"status": "old", "primary_method": "old.repeat"},
                    "gene_annotation": {"status": "old", "primary_method": "old.gene"},
                    "function_annotation": {"status": "old", "primary_method": "old.function"},
                }
            )
            recommended = build_recommended_annotation_plan(state)
            final = recommended.model_copy(
                update={
                    "selected_stages": ["hic_scaffolding"],
                    "resolved_execution_order": ["hic_scaffolding"],
                    "selected_methods": {"hic_scaffolding": "hic_scaffolding.candidate.yahs"},
                    "confirmed": True,
                }
            )

            result = persist_annotation_intake_plan(
                state=state,
                recommended_plan=recommended,
                final_plan=final,
                request=_request(session_id),
                source="test",
            )

            self.assertEqual(
                result.final_requested_stages,
                ["read_qc", "assembly", "assembly_review", "hic_scaffolding"],
            )
            self.assertIn("assembly", result.update["stage_outputs"])
            self.assertIn("assembly_review", result.update["stage_outputs"])
            self.assertIn("hic_scaffolding", result.update["stage_outputs"])
            for stale_stage in ("repeat_annotation", "gene_annotation", "function_annotation"):
                self.assertNotIn(stale_stage, result.update["stage_outputs"])
                self.assertNotIn(stale_stage, result.update["skip_reasons"])
            self.assertEqual(result.update["skip_reasons"]["assembly"], "upstream preserved")

    def test_non_continue_decision_skips_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            session_id = "s4"
            state = _state(data_dir, session_id, ["read_qc", "assembly", "assembly_review"])

            result = maybe_collect_annotation_intake(
                state=state,
                resume_update={},
                request=_request(session_id, decision="rerun"),
                source="test",
                interactive=False,
            )

            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
