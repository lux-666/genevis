"""Focused v2 contracts for the assembly parameter-approval gate.

Persistence breadth lives in ``test_parameter_persistence_v2`` and schema
strictness lives in ``test_parameter_attempt_schema``.  These tests retain
only gate binding, replay, compact-chain, and retry-cursor behavior.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import ResumeValidationError, resume_review_state
from app.schemas.artifacts import ArtifactRef, ParameterAttempt
from app.stages.assembly.parameter_nodes import (
    _seal_parameter_attempt,
    _validate_parameter_attempt_chain,
    build_parameter_attempt_timeline,
    execution_retry_gate,
    recommend_assembly_parameters,
)
from tests.test_parameter_persistence_v2 import (
    _Model,
    _blocked_state,
    _decision_request,
    _response,
    _state,
)


def _ref(name: str, artifact_type: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"art_{name[0] * 32}",
        path_ref=f"artifacts/assembly/parameter_recommendations/{name}.json",
        sha256=name[0] * 64,
        size_bytes=1,
        artifact_type=artifact_type,
    )


def _attempt(
    attempt_id: int,
    *,
    task: str,
    previous_attempt_id: int | None = None,
    previous_attempt_seal_hash: str | None = None,
    status: str = "recommended",
    execution_ref: ArtifactRef | None = None,
) -> dict:
    return ParameterAttempt(
        attempt_id=attempt_id,
        method_id="assembly.candidate.hifiasm_ont",
        status=status,
        task=task,
        previous_attempt_id=previous_attempt_id,
        previous_attempt_seal_hash=previous_attempt_seal_hash,
        recommendation_manifest_ref=_ref(
            f"a{attempt_id}", "assembly.parameter.recommendation_manifest"
        ),
        approval_decision_ref=_ref(
            f"b{attempt_id}", "assembly.parameter_approval.decision"
        ),
        execution_ref=execution_ref,
    ).model_dump(mode="json")


class ParameterApprovalGateV2Tests(unittest.TestCase):
    def _blocked(self, root: Path) -> dict:
        state = _state(root)
        with patch(
            "app.stages.assembly.decision.build_chat_model",
            return_value=_Model(_response(purge_level=1)),
        ):
            update = recommend_assembly_parameters(state)
        return _blocked_state(state, update)

    def test_exact_resume_binding_fails_closed_on_gate_or_hash_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = self._blocked(Path(tmp))

            missing_gate = _decision_request(blocked, "approve_parameters").model_copy(
                update={"gate_id": None}
            )
            with self.assertRaises(ResumeValidationError):
                resume_review_state(blocked, missing_gate)

            stale_hash = _decision_request(blocked, "approve_parameters")
            stale_hash.metadata["recommendation_hash"] = "0" * 64
            with self.assertRaises(ResumeValidationError):
                resume_review_state(blocked, stale_hash)

    def test_resolved_request_cannot_be_approved_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = self._blocked(Path(tmp))
            request = _decision_request(blocked, "approve_parameters")
            approved = resume_review_state(blocked, request)

            with self.assertRaises(ResumeValidationError):
                resume_review_state(approved, request)

    def test_compact_chain_seals_and_timeline_contains_only_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(Path(tmp))
            initial = _seal_parameter_attempt(
                state,
                _attempt(0, task="recommend_initial"),
            )
            retry = _seal_parameter_attempt(
                state,
                _attempt(
                    1,
                    task="retry_after_review_failure",
                    previous_attempt_id=0,
                    previous_attempt_seal_hash=initial["seal_hash"],
                ),
            )
            chain = _validate_parameter_attempt_chain(state, [initial, retry])
            timeline = build_parameter_attempt_timeline(
                state,
                {
                    "parameter_attempts": chain,
                    "retry_counts": {"execution": 0, "review": 1},
                },
                status="assembly_succeeded",
                reason="completed",
            )

            self.assertEqual([entry["attempt_id"] for entry in timeline["entries"]], [0, 1])
            self.assertEqual(timeline["terminal"]["review_retries_used"], 1)
            serialized = json.dumps(timeline, separators=(",", ":"))
            for forbidden in (
                "catalog_snapshot",
                "recommended_params",
                "execution_result",
                "stderr_tail",
                "stdout_tail",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_sealed_chain_rejects_reorder_delete_and_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(Path(tmp))
            initial = _seal_parameter_attempt(
                state,
                _attempt(0, task="recommend_initial"),
            )
            retry = _seal_parameter_attempt(
                state,
                _attempt(
                    1,
                    task="retry_after_execution_failure",
                    previous_attempt_id=0,
                    previous_attempt_seal_hash=initial["seal_hash"],
                ),
            )
            changed = {**initial, "status": "executed"}

            for candidate in ([retry], [retry, initial], [changed, retry]):
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    _validate_parameter_attempt_chain(state, candidate)

    def test_execution_retry_gate_emits_ref_only_request_without_spending_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(Path(tmp))
            execution_ref = _ref("c0", "assembly.parameter.execution")
            attempt = _attempt(
                0,
                task="recommend_initial",
                status="execution_failed",
                execution_ref=execution_ref,
            )
            state["stage_outputs"]["assembly"].update(
                {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                    "retry_counts": {"execution": 0, "review": 0},
                    "parameter_recommendation_config": {"max_execution_retries": 1},
                }
            )

            output = execution_retry_gate(state)["stage_outputs"]["assembly"]

            self.assertEqual(output["status"], "retry_parameters_after_execution_failure")
            self.assertEqual(output["retry_counts"], {"execution": 0, "review": 0})
            self.assertEqual(output["proposed_parameter_attempt_id"], 1)
            self.assertEqual(
                output["parameter_retry_request"]["failure_ref"],
                execution_ref.model_dump(mode="json"),
            )
            self.assertNotIn("execution_result", output["parameter_attempts"][0])


if __name__ == "__main__":
    unittest.main()
