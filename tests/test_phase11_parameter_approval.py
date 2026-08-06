"""Focused v3 contracts for the assembly parameter-approval gate.

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
from app.execution.parameter_attempts import (
    ParameterAttemptStateError,
    current_parameter_attempt,
    update_current_parameter_attempt,
)
from app.schemas.artifacts import ArtifactRef, ParameterAttempt
from app.stages.assembly.parameters.storage import read_artifact as read_json_artifact
from app.stages.assembly.parameters.workflow import (
    _seal_parameter_attempt,
    _validate_parameter_attempt_chain,
    build_parameter_attempt_timeline,
    execution_retry_gate,
    recommend_assembly_parameters,
)
from tests.test_parameter_persistence_v2 import (
    _blocked_state,
    _decision_request,
    _Model,
    _response,
    _state,
)


def _ref(name: str, artifact_type: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"art_{name[0] * 32}",
        path_ref=(
            "artifacts/steps/step-003-assembly/assembly/"
            f"parameter_recommendations/{name}.json"
        ),
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
        schema_version=3,
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
        frozen_attempt_ref=(
            _ref(f"f{attempt_id}", "assembly.frozen_attempt")
            if status != "recommended"
            else None
        ),
        execution_ref=execution_ref,
    ).model_dump(mode="json")


class ParameterApprovalGateV3Tests(unittest.TestCase):
    def _blocked(self, root: Path) -> dict:
        state = _state(root)
        with patch(
            "app.llm.parameters.assembly.recommender.build_chat_model",
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

    def test_pre_v3_attempt_chain_fails_closed_without_escaping(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(Path(tmp))
            legacy_attempt = _attempt(0, task="recommend_initial")
            legacy_attempt["schema_version"] = 2
            state["stage_outputs"]["assembly"].update(
                {
                    "status": "retry_parameters_after_execution_failure",
                    "parameter_attempts": [legacy_attempt],
                }
            )

            update = recommend_assembly_parameters(state)

            assembly = update["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "failed")
            self.assertIn(
                "unsupported_parameter_recommendation_schema",
                assembly["reason"],
            )

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
                    task="retry_after_execution_failure",
                    previous_attempt_id=0,
                    previous_attempt_seal_hash=initial["seal_hash"],
                ),
            )
            chain = _validate_parameter_attempt_chain(state, [initial, retry])
            timeline = build_parameter_attempt_timeline(
                state,
                {
                    "parameter_attempts": chain,
                    "retry_counts": {"execution": 1},
                },
                status="assembly_succeeded",
                reason="completed",
            )

            self.assertEqual(
                [entry["attempt_id"] for entry in timeline["entries"]], [0, 1]
            )
            self.assertEqual(timeline["terminal"]["execution_retries_used"], 1)
            serialized = json.dumps(timeline, separators=(",", ":"))
            for forbidden in (
                "catalog_snapshot",
                "recommended_params",
                "execution_result",
                "stderr_tail",
                "stdout_tail",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_retry_preserves_each_attempts_exact_frozen_runner_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = _state(Path(tmp))
            initial = _seal_parameter_attempt(
                state,
                _attempt(
                    0,
                    task="recommend_initial",
                    status="execution_failed",
                    execution_ref=_ref("c0", "assembly.execution_result"),
                ),
            )
            retry = _attempt(
                1,
                task="retry_after_execution_failure",
                previous_attempt_id=0,
                previous_attempt_seal_hash=initial["seal_hash"],
            )
            output = {
                "current_parameter_attempt_id": 1,
                "parameter_attempts": [initial, retry],
            }

            attempts = update_current_parameter_attempt(
                output,
                method_id="assembly.candidate.hifiasm_ont",
                label="hifiasm-ont",
                status="frozen",
                frozen_parameters_ref=_ref(
                    "d1", "assembly.parameter.frozen_parameters"
                ).model_dump(mode="json"),
                command_ref=_ref("e1", "assembly.parameter.command").model_dump(
                    mode="json"
                ),
                frozen_attempt_ref=_ref("f1", "assembly.frozen_attempt").model_dump(
                    mode="json"
                ),
            )

            self.assertEqual(
                attempts[0]["frozen_attempt_ref"],
                initial["frozen_attempt_ref"],
            )
            self.assertNotEqual(
                attempts[1]["frozen_attempt_ref"],
                attempts[0]["frozen_attempt_ref"],
            )
            initial_manifest = read_json_artifact(
                state,
                initial["attempt_manifest_ref"],
                expected_type="assembly.parameter.attempt_manifest",
            )
            self.assertEqual(
                initial_manifest["frozen_attempt_ref"],
                initial["frozen_attempt_ref"],
            )
            sealed_retry = _seal_parameter_attempt(state, attempts[1])
            timeline = build_parameter_attempt_timeline(
                state,
                {
                    "parameter_attempts": [initial, sealed_retry],
                    "retry_counts": {"execution": 1, "review": 0},
                },
                status="failed",
                reason="test",
            )
            self.assertEqual(
                [entry["frozen_attempt_ref"] for entry in timeline["entries"]],
                [
                    initial["frozen_attempt_ref"],
                    sealed_retry["frozen_attempt_ref"],
                ],
            )

    def test_current_parameter_attempt_requires_strict_explicit_cursor(self):
        attempt = _attempt(0, task="recommend_initial")
        invalid_cursors = {
            "missing": ...,
            "none": None,
            "string": "0",
            "float": 0.0,
            "false": False,
            "true": True,
            "negative": -1,
        }

        for name, cursor in invalid_cursors.items():
            with self.subTest(cursor=name):
                output = {"parameter_attempts": [attempt]}
                if cursor is not ...:
                    output["current_parameter_attempt_id"] = cursor
                with self.assertRaisesRegex(
                    ParameterAttemptStateError,
                    "current_parameter_attempt_id must be an explicit "
                    "non-negative integer",
                ):
                    current_parameter_attempt(
                        output,
                        method_id="assembly.candidate.hifiasm_ont",
                        label="hifiasm-ont",
                    )

        selected = current_parameter_attempt(
            {
                "current_parameter_attempt_id": 0,
                "parameter_attempts": [attempt],
            },
            method_id="assembly.candidate.hifiasm_ont",
            label="hifiasm-ont",
        )
        self.assertEqual(selected.attempt_id, 0)

    def test_execution_cursor_preserves_corrupt_history_without_revalidating_it(self):
        output = {
            "current_parameter_attempt_id": 0,
            "parameter_attempts": [
                _attempt(0, task="recommend_initial"),
                {"attempt_id": 1},
            ],
        }

        attempts = update_current_parameter_attempt(
            output,
            method_id="assembly.candidate.hifiasm_ont",
            label="hifiasm-ont",
        )

        self.assertEqual(attempts[1], output["parameter_attempts"][1])
        self.assertEqual(attempts[0]["status"], "recommended")

        # Full-chain validation remains an explicit control-boundary check.
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(
                ValueError,
                "unsupported_parameter_recommendation_schema",
            ),
        ):
            _validate_parameter_attempt_chain(_state(Path(tmp)), attempts)

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
            execution_ref = _ref("c0", "assembly.execution_result")
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

            self.assertEqual(
                output["status"], "retry_parameters_after_execution_failure"
            )
            self.assertEqual(output["retry_counts"], {"execution": 0, "review": 0})
            self.assertEqual(output["proposed_parameter_attempt_id"], 1)
            self.assertEqual(
                output["parameter_retry_request"]["failure_ref"],
                execution_ref.model_dump(mode="json"),
            )
            self.assertNotIn("execution_result", output["parameter_attempts"][0])


if __name__ == "__main__":
    unittest.main()
