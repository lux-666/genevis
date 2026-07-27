"""Tests for Phase 8 Slice 1 contracts and InputProfiler nodes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import ResumeValidationError, resume_review_state
from app.knowledge.packs import load_knowledge_pack
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate
from app.review.persistence import load_review_request
from app.schemas.artifacts import (
    AssemblyInputProfile,
    AssemblyQCSummary,
    AssemblyReviewTaskItem,
    AssemblyReviewTasks,
    AssemblyRouteDecision,
    AssemblyT2TReadiness,
)
from app.schemas.state import initial_state
from app.stages.assembly.nodes import (
    apply_route_decision,
    block_assembly_for_human,
    infer_input_profile,
    resolve_extended_inputs,
    select_route,
)
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


class Phase8SchemaTests(unittest.TestCase):
    def test_new_assembly_artifact_schemas_validate(self):
        profile = AssemblyInputProfile(
            session_id="s1",
            target_level="draft",
            recommended_route="hifi",
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
    def test_load_route_selector_context_full(self):
        ctx = load_knowledge_pack(
            "route_selector",
            strict=False,
            include_actions=True,
            required_files=(),
        )

        self.assertIn("hifi", ctx["allowed_actions"])
        self.assertIn("Route Selection Skill", ctx["skill_text"])
        self.assertTrue(ctx["cases"])

    def test_load_route_selector_context_missing(self):
        ctx = load_knowledge_pack(
            "does_not_exist",
            strict=False,
            include_actions=True,
            required_files=(),
        )

        self.assertEqual(
            ctx,
            {
                "rules": {},
                "cases": [],
                "skill": None,
                "skill_text": None,
                "strategies": {},
                "allowed_actions": [],
            },
        )

    def test_load_route_selector_context_cwd_independent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                ctx = load_knowledge_pack(
                    "qc_interpretation",
                    strict=False,
                    include_actions=True,
                    required_files=(),
                )
            finally:
                os.chdir(old_cwd)

        self.assertIn("safety_boundaries", ctx["rules"])
        self.assertIn(
            "all_candidates_fragmented",
            ctx["rules"]["safety_boundaries"]["always_block_human"],
        )

    def test_load_route_selector_context_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "assembly"
            pack = root / "partial_pack"
            pack.mkdir(parents=True)
            (pack / "rules.yaml").write_text(
                "allowed_actions:\n  - choose\n",
                encoding="utf-8",
            )
            ctx = load_knowledge_pack(
                "partial_pack",
                root=root,
                strict=False,
                include_actions=True,
                required_files=(),
            )

        self.assertEqual(ctx["allowed_actions"], ["choose"])
        self.assertIsNone(ctx["skill_text"])
        self.assertEqual(ctx["cases"], [])


class InputProfilerTests(unittest.TestCase):
    def _state(self, tmpdir: str, *, assets: list[dict] | None = None) -> dict:
        run_dir = v2_run_dir(tmpdir, "s1")
        Ledger(run_dir).create_session("s1", "control/manifest.yaml")
        state = initial_state(
            session_id="s1",
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
        )
        state["current_stage"] = "assembly"
        registry = run_dir / "artifacts" / "inventory" / "asset_registry.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            json.dumps({"session_id": "s1", "assets": assets or []}), encoding="utf-8"
        )
        state["asset_registry_ref"] = str(registry)
        return state

    def _blocked_route_state(self, state: dict, assembly: dict | None = None) -> dict:
        if assembly is not None:
            state["stage_outputs"] = {"assembly": assembly}
        routed = {**state, **select_route(state)}
        return {**routed, **block_assembly_for_human(routed)}

    def _route_assembly(
        self,
        recommended_route: str = "hifi",
        *,
        review_policy: dict | None = None,
    ) -> dict:
        selections = {
            "hifi": ("pacbio_hifi", "assembly.candidate.hifiasm"),
            "ont": ("ont_raw", "assembly.candidate.hifiasm_ont"),
            "cyclone": ("cyclone", "assembly.candidate.flye"),
        }
        selection = None
        if recommended_route in selections:
            read_type, method = selections[recommended_route]
            selection = {
                "read_type": read_type,
                "primary_method": method,
                "source": "recommendation",
                "rationale": f"{recommended_route} recommended",
            }
        assembly = {
            "status": "profile_ready",
            "input_profile": {
                "recommended_route": recommended_route,
                "recommendation_reasoning": f"{recommended_route} recommendation",
                "uncertainty_notes": [],
            },
            "recommended_assembly_selection": selection,
        }
        if review_policy is not None:
            assembly["review_policy"] = review_policy
        return assembly

    def _request(self, state: dict, decision: str, **kwargs) -> ResumeReviewRequest:
        interrupt = state["active_interrupt"]
        return ResumeReviewRequest(
            session_id=state["session_id"],
            stage="assembly",
            gate_id="route_selection",
            decision=decision,
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            **kwargs,
        )

    def _request_payload(self, state: dict) -> dict:
        interrupt = state["active_interrupt"]
        return load_review_request(
            state["data_dir"],
            interrupt["request_ref"],
            session_id=state["session_id"],
        )["payload"]

    def _ref_path(self, state: dict, ref: str | dict) -> Path:
        if isinstance(ref, dict):
            return SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(
                ref["path_ref"]
            )
        return Path(ref)

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
                    {
                        "asset_id": "ont",
                        "asset_type": "clean_long_reads",
                        "path": str(ont_reads),
                        "platform": "ont",
                    },
                    {
                        "asset_id": "hifi",
                        "asset_type": "pacbio_hifi",
                        "path": str(hifi_reads),
                    },
                ],
            )

            result = resolve_extended_inputs(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertTrue(assembly["has_hifi"])
            self.assertEqual(assembly["ont_reads_ref"], str(ont_reads))
            self.assertEqual(assembly["hifi_reads_ref"], str(hifi_reads))
            self.assertEqual(assembly["primary_long_reads_ref"], str(hifi_reads))
            self.assertEqual(assembly["input_ref"], str(hifi_reads))

    def test_resolve_extended_inputs_hifi_hint_falls_back_to_clean_long_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(
                tmpdir,
                assets=[
                    {
                        "asset_id": "reads",
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                    }
                ],
            )
            state["shared_parameters"] = {"read_type": "pacbio_hifi"}

            result = resolve_extended_inputs(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["status"], "resolved_extended")
            self.assertEqual(assembly["primary_long_reads_ref"], str(reads))
            self.assertEqual(assembly["hifi_reads_ref"], str(reads))
            self.assertEqual(assembly["input_ref"], str(reads))
            self.assertEqual(assembly["normalized_read_type"], "pacbio_hifi")

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
            state = self._state(
                tmpdir,
                assets=[
                    {
                        "asset_id": "ont",
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                    }
                ],
            )
            state["stage_outputs"] = {
                "assembly": {"assembly_route": "hifi", "hifi_reads_ref": str(reads)}
            }
            resolved = {**state, **resolve_extended_inputs(state)}

            with patch(
                "app.stages.assembly.decision.build_chat_model",
                side_effect=AssertionError("LLM should not run"),
            ):
                result = infer_input_profile(resolved)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(
                assembly["input_profile"]["profile_source"], "run_config_explicit"
            )
            self.assertEqual(assembly["route_decision"]["selected_route"], "hifi")
            self.assertTrue(
                self._ref_path(state, assembly["input_profile_ref"]).exists()
            )

    def test_explicit_cyclone_route_defaults_to_flye_assembly_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(
                tmpdir,
                assets=[
                    {
                        "asset_id": "cyclone",
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                    }
                ],
            )
            state["stage_outputs"] = {"assembly": {"assembly_route": "cyclone"}}
            resolved = {**state, **resolve_extended_inputs(state)}

            result = infer_input_profile(resolved)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["selected_route"], "cyclone")
            selection = assembly["assembly_selection"]
            self.assertEqual(selection["read_type"], "cyclone")
            self.assertEqual(selection["primary_method"], "assembly.candidate.flye")
            self.assertIsNone(selection["fallback_method"])
            self.assertEqual(selection["source"], "run_config")
            self.assertEqual(
                selection["rationale"],
                "run_config explicitly selected cyclone read type",
            )
            self.assertNotIn("method_decision", assembly)

    def test_explicit_ont_route_preserves_ont_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(
                tmpdir,
                assets=[
                    {
                        "asset_id": "ont",
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                    }
                ],
            )
            state["stage_outputs"] = {"assembly": {"assembly_route": "ont"}}
            resolved = {**state, **resolve_extended_inputs(state)}

            result = infer_input_profile(resolved)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["selected_route"], "ont")
            self.assertEqual(assembly["route_decision"]["selected_route"], "ont")
            self.assertEqual(assembly["input_profile"]["recommended_route"], "ont")
            selection = assembly["assembly_selection"]
            self.assertEqual(selection["read_type"], "ont_raw")
            self.assertEqual(
                selection["primary_method"], "assembly.candidate.hifiasm_ont"
            )
            self.assertIsNone(selection["fallback_method"])
            self.assertEqual(selection["source"], "run_config")
            self.assertEqual(
                selection["rationale"], "run_config explicitly selected ont read type"
            )
            self.assertNotIn("method_decision", assembly)

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
            state = self._state(
                tmpdir,
                assets=[
                    {
                        "asset_id": "ont",
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                    }
                ],
            )
            resolved = {**state, **resolve_extended_inputs(state)}
            model = FakeModel()

            with patch(
                "app.stages.assembly.decision.build_chat_model", return_value=model
            ):
                result = infer_input_profile(resolved)

            self.assertIsNotNone(model.messages)
            self.assertIn("Route Selection Skill", model.messages[0]["content"])
            self.assertEqual(
                result["stage_outputs"]["assembly"]["input_profile"][
                    "recommended_route"
                ],
                "cyclone",
            )
            self.assertNotIn(
                "route_selector_prompt", result["stage_outputs"]["assembly"]
            )
            self.assertNotIn(
                "route_selector_source", result["stage_outputs"]["assembly"]
            )

    def test_infer_input_profile_llm_failure_returns_insufficient_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            reads = Path(tmpdir) / "reads.fastq"
            reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            state = self._state(
                tmpdir,
                assets=[
                    {
                        "asset_id": "ont",
                        "asset_type": "clean_long_reads",
                        "path": str(reads),
                    }
                ],
            )
            resolved = {**state, **resolve_extended_inputs(state)}

            with patch(
                "app.stages.assembly.decision.build_chat_model",
                side_effect=RuntimeError("llm down"),
            ):
                result = infer_input_profile(resolved)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(
                assembly["input_profile"]["recommended_route"], "insufficient_data"
            )
            self.assertNotIn("route_selector_prompt", assembly)
            self.assertNotIn("route_selector_source", assembly)

    def test_select_route_defaults_to_strict_and_blocks_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            blocked = self._blocked_route_state(
                state,
                self._route_assembly("cyclone"),
            )
            assembly = blocked["stage_outputs"]["assembly"]
            payload = self._request_payload(blocked)

            self.assertEqual(assembly["status"], "blocked_human")
            self.assertNotIn("route_decision", assembly)
            self.assertEqual(blocked["active_interrupt"]["gate_id"], "route_selection")
            self.assertEqual(payload["recommended_route"], "cyclone")
            self.assertEqual(payload["review_policy"]["mode"], "strict")
            self.assertNotIn("recommended_route", blocked["active_interrupt"])
            self.assertNotIn("review_policy", blocked["active_interrupt"])

    def test_select_route_auto_policy_can_adopt_high_confidence(self):
        """All LLM recommendations now require human gate (confidence removed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            blocked = self._blocked_route_state(
                state,
                self._route_assembly("cyclone", review_policy={"mode": "auto"}),
            )
            assembly = blocked["stage_outputs"]["assembly"]
            payload = self._request_payload(blocked)

            # After confidence removal, all LLM recommendations block for human gate
            self.assertEqual(assembly["status"], "blocked_human")
            self.assertNotIn("route_decision", assembly)
            self.assertEqual(blocked["active_interrupt"]["gate_id"], "route_selection")
            self.assertEqual(payload["review_policy"]["mode"], "auto")

    def test_select_route_insufficient_data_blocks_without_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            blocked = self._blocked_route_state(
                state,
                self._route_assembly("insufficient_data"),
            )
            assembly = blocked["stage_outputs"]["assembly"]
            payload = self._request_payload(blocked)

            self.assertEqual(assembly["status"], "blocked_human")
            self.assertNotIn("route_decision", assembly)
            self.assertEqual(blocked["active_interrupt"]["gate_id"], "route_selection")
            self.assertEqual(payload["recommended_route"], "insufficient_data")
            self.assertEqual(payload["recommended_assembly_selection"], {})

    def test_apply_route_decision_maps_gate_decision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state = self._blocked_route_state(state, self._route_assembly())
            request = self._request(state, "select_ont")

            result = apply_route_decision(state, request)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "route_selected")
            self.assertEqual(assembly["selected_route"], "ont")
            self.assertEqual(assembly["route_decision"]["selected_route"], "ont")
            self.assertTrue(
                self._ref_path(state, assembly["route_decision_ref"]).exists()
            )

    def test_route_selection_gate_registered_and_resumable(self):
        gate = get_review_gate("assembly", "route_selection")
        self.assertEqual(gate.template_preferred_decision, "select_ont")

        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state = self._blocked_route_state(state, self._route_assembly())
            request = self._request(state, "select_cyclone")

            result = resume_review_state(state, request)

            self.assertEqual(
                result["stage_outputs"]["assembly"]["selected_route"], "cyclone"
            )

    def test_route_decision_maps_to_assembly_selection(self):
        """Route decisions write the complete canonical assembly selection."""
        cases = [
            {
                "name": "hifi",
                "decision": "select_hifi",
                "selected_route": "hifi",
                "assembly_selection": {
                    "read_type": "pacbio_hifi",
                    "primary_method": "assembly.candidate.hifiasm",
                    "fallback_method": None,
                    "source": "human",
                    "rationale": "human selected hifi read type",
                },
            },
            {
                "name": "ont",
                "decision": "select_ont",
                "selected_route": "ont",
                "assembly_selection": {
                    "read_type": "ont_raw",
                    "primary_method": "assembly.candidate.hifiasm_ont",
                    "fallback_method": None,
                    "source": "human",
                    "rationale": "human selected ont read type",
                },
            },
            {
                "name": "cyclone",
                "decision": "select_cyclone",
                "selected_route": "cyclone",
                "assembly_selection": {
                    "read_type": "cyclone",
                    "primary_method": "assembly.candidate.flye",
                    "fallback_method": None,
                    "source": "human",
                    "rationale": "human selected cyclone read type",
                },
            },
        ]

        for case in cases:
            with self.subTest(case["name"]):
                with tempfile.TemporaryDirectory() as tmpdir:
                    state = self._state(tmpdir)
                    state = self._blocked_route_state(state, self._route_assembly())
                    request = self._request(state, case["decision"])

                    result = apply_route_decision(state, request)
                    assembly = result["stage_outputs"]["assembly"]

                    self.assertEqual(assembly["selected_route"], case["selected_route"])
                    for key, expected in case["assembly_selection"].items():
                        self.assertEqual(assembly["assembly_selection"][key], expected)
                    self.assertNotIn("method_decision", assembly)

    def test_route_selection_includes_review_policy_outcome(self):
        """P1 fix: route selection should call evaluate() and include policy outcome"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            blocked = self._blocked_route_state(
                state,
                self._route_assembly(
                    review_policy={"mode": "strict", "max_blocked_seconds": 180},
                ),
            )
            assembly = blocked["stage_outputs"]["assembly"]
            payload = self._request_payload(blocked)

            self.assertEqual(assembly["status"], "blocked_human")
            outcome = payload["review_policy_outcome"]
            self.assertEqual(outcome["action"], "BLOCK_WITH_TIMEOUT")
            self.assertEqual(outcome["max_blocked_seconds"], 180)
            self.assertEqual(payload["review_policy"]["max_blocked_seconds"], 180)
            self.assertIn("blocked_at", blocked["active_interrupt"])
            self.assertNotIn("review_policy_outcome", blocked["active_interrupt"])

    def test_route_selection_no_timeout_blocks_indefinitely(self):
        """P1 fix: strict mode without timeout should block indefinitely"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            blocked = self._blocked_route_state(
                state,
                self._route_assembly(review_policy={"mode": "strict"}),
            )
            assembly = blocked["stage_outputs"]["assembly"]
            payload = self._request_payload(blocked)

            self.assertEqual(assembly["status"], "blocked_human")
            outcome = payload["review_policy_outcome"]
            self.assertEqual(outcome["action"], "BLOCK_INDEFINITE")
            self.assertIsNone(outcome["max_blocked_seconds"])

    def test_explicit_route_maps_to_assembly_selection(self):
        """An explicit run-config route writes the canonical assembly selection."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "resolved_extended",
                    "explicit_route": "hifi",
                    "hifi_reads_ref": "reads/hifi.fastq",
                }
            }

            result = infer_input_profile(state)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["status"], "profile_ready")
            self.assertEqual(assembly["selected_route"], "hifi")
            selection = assembly["assembly_selection"]
            self.assertEqual(selection["read_type"], "pacbio_hifi")
            self.assertEqual(selection["primary_method"], "assembly.candidate.hifiasm")
            self.assertIsNone(selection["fallback_method"])
            self.assertEqual(selection["source"], "run_config")
            self.assertEqual(
                selection["rationale"], "run_config explicitly selected hifi read type"
            )
            self.assertNotIn("method_decision", assembly)

    def test_route_decision_honors_explicit_resume_method_and_read_type(self):
        """Selector V2 uses method_name plus metadata.read_type for human intent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state = self._blocked_route_state(state, self._route_assembly())
            request = self._request(
                state,
                "select_hifi",
                method_name="assembly.candidate.nextdenovo",
                metadata={"read_type": "pacbio_hifi"},
            )

            result = apply_route_decision(state, request)
            assembly = result["stage_outputs"]["assembly"]

            self.assertEqual(assembly["selected_route"], "hifi")
            self.assertEqual(assembly["assembly_selection"]["read_type"], "pacbio_hifi")
            self.assertEqual(
                assembly["assembly_selection"]["primary_method"],
                "assembly.candidate.nextdenovo",
            )
            self.assertEqual(
                assembly["assembly_selection"]["metadata"]["read_type_source"],
                "metadata.read_type",
            )

    def test_route_decision_rejects_read_type_metadata_that_conflicts_with_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state = self._blocked_route_state(state, self._route_assembly())
            request = self._request(
                state,
                "select_hifi",
                metadata={"read_type": "ont"},
            )

            with self.assertRaisesRegex(
                ResumeValidationError, "maps to route 'hifi'.*metadata.read_type"
            ):
                apply_route_decision(state, request)

    def test_route_decision_rejects_unknown_method_name_as_resume_validation_error(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state = self._blocked_route_state(state, self._route_assembly())
            request = self._request(
                state,
                "select_hifi",
                method_name="assembly.candidate.unknown",
            )

            with self.assertRaisesRegex(
                ResumeValidationError, "unknown assembly method"
            ):
                apply_route_decision(state, request)
