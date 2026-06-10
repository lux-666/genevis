"""Tests for Phase 8 Slice 1 contracts and InputProfiler nodes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import resume_review_state
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate
from app.schemas.artifacts import (
    AssemblyInputProfile,
    AssemblyQCSummary,
    AssemblyReviewTaskItem,
    AssemblyReviewTasks,
    AssemblyRouteDecision,
    AssemblyT2TReadiness,
)
from app.schemas.state import initial_state
from app.stages.assembly.knowledge import load_skill_context
from app.stages.assembly.nodes import (
    apply_route_decision,
    infer_input_profile,
    resolve_extended_inputs,
    select_route,
)


class Phase8SchemaTests(unittest.TestCase):
    def test_new_assembly_artifact_schemas_validate(self):
        profile = AssemblyInputProfile(
            session_id="s1",
            target_level="draft",
            recommended_route="hifi",
            recommendation_confidence=0.9,
            recommendation_reasoning="HiFi reads available",
            uncertainty_notes=["coverage not estimated"],
            profile_source="llm_inferred",
        )
        decision = AssemblyRouteDecision(
            session_id="s1",
            selected_route="hifi",
            decided_by="llm_auto",
            rationale="HiFi reads available",
        )
        qc = AssemblyQCSummary(
            session_id="s1",
            candidate_id="c1",
            method_name="assembly.candidate.hifiasm",
            qc_passed=True,
        )
        readiness = AssemblyT2TReadiness(
            session_id="s1",
            candidate_id="c1",
            t2t_verdict="near_t2t",
            llm_confidence=0.8,
            llm_reasoning="near complete",
            uncertainty_notes=["Hi-C absent"],
        )
        tasks = AssemblyReviewTasks(
            session_id="s1",
            candidate_id="c1",
            t2t_status_estimate="near_t2t",
            confidence=0.8,
            tasks=[
                AssemblyReviewTaskItem(
                    priority="high",
                    category="telomere",
                    finding="missing telomere signal",
                    action="inspect contig ends",
                )
            ],
        )

        self.assertEqual(profile.uncertainty_notes, ["coverage not estimated"])
        self.assertEqual(decision.selected_route, "hifi")
        self.assertTrue(qc.qc_passed)
        self.assertEqual(readiness.uncertainty_notes, ["Hi-C absent"])
        self.assertEqual(tasks.tasks[0].category, "telomere")


class KnowledgePackTests(unittest.TestCase):
    def test_load_skill_context_full(self):
        ctx = load_skill_context("route_selector")

        self.assertIn("hifi", ctx["allowed_actions"])
        self.assertIn("Route Selection Skill", ctx["skill_text"])
        self.assertTrue(ctx["cases"])

    def test_load_skill_context_missing(self):
        ctx = load_skill_context("does_not_exist")

        self.assertEqual(ctx, {"rules": {}, "skill_text": None, "cases": [], "allowed_actions": []})

    def test_load_skill_context_cwd_independent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                ctx = load_skill_context("qc_interpretation")
            finally:
                os.chdir(old_cwd)

        self.assertIn("safety_boundaries", ctx["rules"])
        self.assertIn("all_candidates_fragmented", ctx["rules"]["safety_boundaries"]["always_block_human"])

    def test_load_skill_context_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "assembly"
            pack = root / "partial_pack"
            pack.mkdir(parents=True)
            (pack / "rules.yaml").write_text(
                "allowed_actions:\n  - choose\n",
                encoding="utf-8",
            )
            with patch("app.stages.assembly.knowledge._PACK_ROOT", root):
                ctx = load_skill_context("partial_pack")

        self.assertEqual(ctx["allowed_actions"], ["choose"])
        self.assertIsNone(ctx["skill_text"])
        self.assertEqual(ctx["cases"], [])


class InputProfilerTests(unittest.TestCase):
    def _state(self, tmpdir: str, *, assets: list[dict] | None = None) -> dict:
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "assembly"
        registry = Path(tmpdir) / "registry.json"
        registry.write_text(json.dumps({"session_id": "s1", "assets": assets or []}), encoding="utf-8")
        state["asset_registry_ref"] = str(registry)
        return state

    def test_resolve_extended_inputs_ont_only_preserves_input_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "ont.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(
                tmpdir,
                assets=[
                    {
                        "asset_id": "ont",
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                        "platform": "cyclone",
                    }
                ],
            )

            result = resolve_extended_inputs(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["status"], "resolved_extended")
            self.assertEqual(assembly["ont_reads_ref"], str(reads))
            self.assertEqual(assembly["primary_long_reads_ref"], str(reads))
            self.assertEqual(assembly["input_ref"], str(reads))
            self.assertEqual(assembly["input_type"], "clean_long_reads")

    def test_resolve_extended_inputs_hifi_priority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ont_reads = Path(tmpdir) / "ont.fastq"
            hifi_reads = Path(tmpdir) / "hifi.fastq"
            ont_reads.write_text("@o\nACGT\n+\n!!!!\n", encoding="utf-8")
            hifi_reads.write_text("@h\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(
                tmpdir,
                assets=[
                    {"asset_id": "ont", "asset_type": "clean_long_reads", "path": str(ont_reads), "platform": "ont"},
                    {"asset_id": "hifi", "asset_type": "pacbio_hifi", "path": str(hifi_reads)},
                ],
            )

            result = resolve_extended_inputs(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertTrue(assembly["has_hifi"])
            self.assertEqual(assembly["ont_reads_ref"], str(ont_reads))
            self.assertEqual(assembly["hifi_reads_ref"], str(hifi_reads))
            self.assertEqual(assembly["primary_long_reads_ref"], str(hifi_reads))
            self.assertEqual(assembly["input_ref"], str(hifi_reads))

    def test_resolve_extended_inputs_no_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)

            result = resolve_extended_inputs(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["status"], "failed")
            self.assertIn("assembly requires long reads", assembly["reason"])

    def test_infer_input_profile_explicit_route_skips_llm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(tmpdir, assets=[{"asset_id": "ont", "asset_type": "clean_long_reads", "path": str(reads)}])
            state["stage_outputs"] = {"assembly": {"assembly_route": "hifi", "hifi_reads_ref": str(reads)}}
            resolved = {**state, **resolve_extended_inputs(state)}

            with patch("app.stages.assembly.nodes.build_chat_model", side_effect=AssertionError("LLM should not run")):
                result = infer_input_profile(resolved)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["input_profile"]["profile_source"], "run_config_explicit")
            self.assertEqual(assembly["route_decision"]["selected_route"], "hifi")
            self.assertTrue(Path(assembly["input_profile_ref"]).exists())

    def test_infer_input_profile_llm_prompt_receives_skill_text(self):
        class FakeModel:
            def __init__(self):
                self.messages = None

            def invoke(self, messages):
                self.messages = messages
                return json.dumps(
                    {
                        "recommended_route": "cyclone",
                        "confidence": 0.72,
                        "reasoning": "Cyclone-like reads",
                        "uncertainty_notes": ["coverage absent"],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(tmpdir, assets=[{"asset_id": "ont", "asset_type": "clean_long_reads", "path": str(reads)}])
            resolved = {**state, **resolve_extended_inputs(state)}
            model = FakeModel()

            with patch("app.stages.assembly.nodes.build_chat_model", return_value=model):
                result = infer_input_profile(resolved)

            self.assertIsNotNone(model.messages)
            self.assertIn("Route Selection Skill", model.messages[0]["content"])
            self.assertEqual(result["stage_outputs"]["assembly"]["input_profile"]["recommended_route"], "cyclone")
            self.assertNotIn("route_selector_prompt", result["stage_outputs"]["assembly"])
            self.assertNotIn("route_selector_source", result["stage_outputs"]["assembly"])

    def test_infer_input_profile_llm_failure_returns_insufficient_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(tmpdir, assets=[{"asset_id": "ont", "asset_type": "clean_long_reads", "path": str(reads)}])
            resolved = {**state, **resolve_extended_inputs(state)}

            with patch("app.stages.assembly.nodes.build_chat_model", side_effect=RuntimeError("llm down")):
                result = infer_input_profile(resolved)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["input_profile"]["recommended_route"], "insufficient_data")
            self.assertEqual(assembly["input_profile"]["recommendation_confidence"], 0.0)
            self.assertNotIn("route_selector_prompt", assembly)
            self.assertNotIn("route_selector_source", assembly)

    def test_select_route_defaults_to_strict_and_blocks_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "profile_ready",
                    "input_profile": {
                        "recommended_route": "cyclone",
                        "recommendation_confidence": 0.95,
                        "recommendation_reasoning": "strong recommendation",
                        "uncertainty_notes": [],
                    },
                }
            }

            result = select_route(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["status"], "blocked_human")
            self.assertNotIn("route_decision", assembly)
            self.assertEqual(assembly["active_interrupt"]["gate_id"], "route_selection")

    def test_select_route_auto_policy_can_adopt_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "profile_ready",
                    "review_policy": {"mode": "auto"},
                    "input_profile": {
                        "recommended_route": "cyclone",
                        "recommendation_confidence": 0.95,
                        "recommendation_reasoning": "strong recommendation",
                        "uncertainty_notes": [],
                    },
                }
            }

            result = select_route(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["status"], "route_selected")
            self.assertEqual(assembly["route_decision"]["selected_route"], "cyclone")

    def test_select_route_insufficient_data_blocks_without_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "profile_ready",
                    "input_profile": {
                        "recommended_route": "insufficient_data",
                        "recommendation_confidence": 0.0,
                        "recommendation_reasoning": "missing reads",
                        "uncertainty_notes": ["no reads"],
                    },
                }
            }

            result = select_route(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["status"], "blocked_human")
            self.assertNotIn("route_decision", assembly)
            self.assertEqual(assembly["active_interrupt"]["gate_id"], "route_selection")

    def test_apply_route_decision_maps_gate_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["supervision_state"] = "blocked_human"
            state["active_interrupt"] = {"stage": "assembly", "gate_id": "route_selection"}
            state["stage_outputs"] = {"assembly": {"status": "blocked_human"}}
            request = ResumeReviewRequest(
                session_id="s1",
                stage="assembly",
                gate_id="route_selection",
                decision="select_ont",
            )

            result = apply_route_decision(state, request)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "route_selected")
            self.assertEqual(assembly["selected_route"], "ont")
            self.assertEqual(assembly["route_decision"]["selected_route"], "ont")
            self.assertTrue(Path(assembly["route_decision_ref"]).exists())

    def test_route_selection_gate_registered_and_resumable(self):
        gate = get_review_gate("assembly", "route_selection")
        self.assertEqual(gate.template_preferred_decision, "select_ont")

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["supervision_state"] = "blocked_human"
            state["active_interrupt"] = {
                "stage": "assembly",
                "gate_id": "route_selection",
                "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            }
            state["stage_outputs"] = {"assembly": {"status": "blocked_human"}}
            request = ResumeReviewRequest(
                session_id="s1",
                stage="assembly",
                gate_id="route_selection",
                decision="select_cyclone",
            )

            result = resume_review_state(state, request)

            self.assertEqual(result["stage_outputs"]["assembly"]["selected_route"], "cyclone")
