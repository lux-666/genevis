"""Tests for the assembly review stage.

Assembly review is the main human gate.  These tests verify that:
- Candidate disagreements do not auto-advance without policy.
- Rejected candidates remain traceable.
- Existing assembly inputs route through deterministically.
- Missing assemblies fail terminally.
- Human gates set blocked_human and active_interrupt.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.stages.assembly_review.builder import build_stage
from app.schemas.state import initial_state


class AssemblyReviewStageTests(unittest.TestCase):
    def _build_state(
        self,
        *,
        session_id: str = "test-session",
        entry_mode: str | None = None,
        candidate_bundles: list[dict] | None = None,
        registry_ref: str | None = None,
        data_dir: str = "/tmp",
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
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="inputs/manifest.yaml",
            data_dir=data_dir,
        )
        state["stage_outputs"] = stage_outputs
        if registry_ref:
            state["asset_registry_ref"] = registry_ref
        return state

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

    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "test-session", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def test_existing_assembly_auto_advances_deterministically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly_fasta = Path(tmpdir) / "existing.fa"
            assembly_fasta.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "test-session:assembly_fasta_registered:existing.fa",
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
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "hic_scaffolding")
            self.assertEqual(result["supervision_state"], "running_stage")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["status"], "completed")
            self.assertEqual(ar_out["decision"], "accept")
            self.assertEqual(ar_out["selected_candidate_id"], "existing_assembly")
            self.assertEqual(ar_out["final_assembly_ref"], str(assembly_fasta))
            decision_ref = Path(ar_out["review_decision_artifact_ref"])
            self.assertTrue(decision_ref.exists())
            decision_payload = json.loads(decision_ref.read_text(encoding="utf-8"))
            self.assertEqual(decision_payload["decision"], "accept")
            self.assertEqual(decision_payload["final_assembly_ref"], str(assembly_fasta))

    def test_no_candidates_fails_terminally(self):
        state = self._build_state(entry_mode="raw_inputs")
        graph = build_stage()
        result = graph.invoke(state)
        self.assertEqual(result["supervision_state"], "failed_terminal")
        self.assertIn("no_assembly_candidates_found", result.get("last_error", ""))

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
            state["current_stage"] = "assembly_review"
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNotNone(result.get("active_interrupt"))
            self.assertEqual(result["active_interrupt"]["stage"], "assembly_review")
            self.assertIn("blocked_at", result["active_interrupt"])
            # current_stage stays at assembly_review while blocked
            self.assertEqual(result["current_stage"], "assembly_review")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["candidates"][0]["bundle_ref"], str(candidate_ref))
            self.assertEqual(ar_out["candidates"][0]["final_assembly_ref"], str(Path(tmpdir) / "flye_01.fasta"))
            self.assertTrue(Path(ar_out["eval_bundle_ref"]).exists())

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
            state = self._build_state(entry_mode="raw_inputs", registry_ref=str(registry), data_dir=tmpdir)
            state["stage_outputs"]["assembly"] = {
                "status": "planned",
                "selected_candidate_refs": [str(candidate_ref)],
            }
            graph = build_stage()
            result = graph.invoke(state)
            ar_out = result["stage_outputs"]["assembly_review"]
            planned = ar_out.get("planned_signals", [])
            self.assertIn("merqury", planned)
            self.assertIn("mapping_coverage", planned)
            self.assertIn("purge_dups_check", planned)
            eval_payload = json.loads(Path(ar_out["eval_bundle_ref"]).read_text(encoding="utf-8"))
            self.assertIn("clean_short_dna_reads", eval_payload["available_inputs"])

    def test_existing_assembly_writes_review_decision_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._build_state(
                entry_mode="existing_assembly",
                registry_ref=f"{tmpdir}/registry.json",
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            events_path = Path(tmpdir) / "ledger" / "test-session" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            decision_events = [e for e in events if e["event_type"] == "review_decision"]
            self.assertEqual(len(decision_events), 1)
            self.assertEqual(decision_events[0]["decision"], "accept")
            self.assertEqual(decision_events[0]["candidate_id"], "existing_assembly")

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
            events_path = Path(tmpdir) / "ledger" / "test-session" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            request_events = [e for e in events if e["event_type"] == "review_requested"]
            self.assertEqual(len(request_events), 1)
            self.assertEqual(request_events[0]["stage"], "assembly_review")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertTrue(Path(ar_out["review_request_artifact_ref"]).exists())

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
                "rejected_candidates": {"assembly.candidate.canu": "genome_size_required"},
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "blocked_human")
            ar_out = result["stage_outputs"]["assembly_review"]
            self.assertEqual(ar_out["candidate_count"], 2)
            candidate_ids = {candidate["candidate_id"] for candidate in ar_out["candidates"]}
            self.assertEqual(
                candidate_ids,
                {"test-session:assembly.candidate.flye", "test-session:assembly.candidate.hifiasm"},
            )
            self.assertEqual(
                ar_out["rejected_candidates"],
                {"assembly.candidate.canu": "genome_size_required"},
            )


class AssemblyReviewSchemaTests(unittest.TestCase):
    def test_assembly_review_plan_has_no_threshold_defaults(self):
        from app.schemas.plan import AssemblyReviewPlan

        plan = AssemblyReviewPlan(status="planned")
        self.assertEqual(plan.decision, "pending")
        self.assertEqual(plan.planned_signals, [])
        # Thresholds are intentionally not part of the plan schema;
        # they require human approval before use.

    def test_review_decision_artifact_allows_all_decision_types(self):
        from app.schemas.artifacts import ReviewDecisionArtifact

        for decision in [
            "accept",
            "rerun",
            "purge",
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

    def test_review_payload_includes_all_allowed_decisions(self):
        from app.review.base import ReviewPayload

        payload = ReviewPayload(
            stage="assembly_review",
            reason="test",
            decisions_allowed=["accept", "rerun", "stop_human_review"],
        )
        self.assertIn("accept", payload.decisions_allowed)
        self.assertIn("rerun", payload.decisions_allowed)
        self.assertIn("stop_human_review", payload.decisions_allowed)


if __name__ == "__main__":
    unittest.main()
