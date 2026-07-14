from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.control.lifecycle import ResumeValidationError, resume_review_state
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ArtifactRef, ParameterAttempt
from app.stages.assembly.nodes import (
    _parameter_proposal_semantic_hash,
    _seal_parameter_attempt,
    _validate_parameter_attempt_chain,
    build_parameter_attempt_timeline,
    execution_retry_gate,
)


class ParameterApprovalGateTests(unittest.TestCase):
    def _three_attempt_chain(self, root: Path) -> list[dict]:
        state = self._state(root, retry=True)
        approved = resume_review_state(state, self._request(state))
        attempts = list(approved["stage_outputs"]["assembly"]["parameter_attempts"])
        attempt1 = _seal_parameter_attempt(
            {**attempts[1], "execution_result": {"status": "failed", "return_code": 137}}
        )
        attempts[1] = attempt1
        attempt0 = attempts[0]
        attempt2 = _seal_parameter_attempt(
            ParameterAttempt(
                attempt_id=2,
                method_id="assembly.candidate.hifiasm",
                status="recommended",
                task="retry_after_review_failure",
                input_profile_ref=ArtifactRef(path_ref="profile2", sha256="1" * 64, artifact_type="profile"),
                prompt_payload_ref=ArtifactRef(path_ref="prompt2", sha256="2" * 64, artifact_type="prompt"),
                catalog_snapshot=attempt0["catalog_snapshot"],
                previous_attempt_id=1,
                previous_attempt_seal_hash=attempt1["seal_hash"],
                attempt_kind="retry",
                retry_origin="assembly_review",
                inherited_initial_attempt_id=0,
                inherited_initial_snapshot_ref=attempt0["seal_hash"],
                inherited_profile_ref=attempt0["input_profile_ref"],
                recommended_params={"purge_level": 2},
                execution_params={"purge_level": 2},
            ).model_dump(mode="json")
        )
        return [attempt0, attempt1, attempt2]

    def _state(self, root: Path, *, retry: bool = False) -> dict:
        attempt_id = 1 if retry else 0
        task = "retry_after_execution_failure" if retry else "recommend_initial"
        previous = None
        attempts = []
        if retry:
            previous = _seal_parameter_attempt(
                ParameterAttempt(
                    attempt_id=0,
                    method_id="assembly.candidate.hifiasm",
                    status="execution_failed",
                    task="recommend_initial",
                    input_profile_ref=ArtifactRef(path_ref="profile0", sha256="a" * 64, artifact_type="profile"),
                    prompt_payload_ref=ArtifactRef(path_ref="prompt0", sha256="b" * 64, artifact_type="prompt"),
                    rendered_parameters={"purge_level": 1},
                    argv=["hifiasm", "-l", "1"],
                    execution_result={"status": "failed", "return_code": 137},
                    catalog_snapshot={},
                    effective_params={"purge_level": 1},
                    execution_params={"purge_level": 1},
                    parameter_recommendation_hash="e" * 64,
                    parameter_approval_recommendation_hash="e" * 64,
                    parameter_approval_decision_ref=ArtifactRef(
                        path_ref="decision0", sha256="f" * 64, artifact_type="decision"
                    ),
                ).model_dump(mode="json")
            )
            attempts = [previous]
        pending = ParameterAttempt(
            attempt_id=attempt_id,
            method_id="assembly.candidate.hifiasm",
            status="recommended",
            task=task,
            input_profile_ref=ArtifactRef(path_ref="profile", sha256="c" * 64, artifact_type="profile"),
            prompt_payload_ref=ArtifactRef(path_ref="prompt", sha256="d" * 64, artifact_type="prompt"),
            catalog_snapshot={},
            previous_attempt_id=0 if retry else None,
            previous_attempt_seal_hash=previous.get("seal_hash") if previous else None,
            attempt_kind="retry" if retry else "initial",
            retry_origin="assembly_execution" if retry else None,
            inherited_initial_attempt_id=0 if retry else None,
            inherited_initial_snapshot_ref=previous.get("seal_hash") if previous else None,
            inherited_profile_ref=previous.get("input_profile_ref") if previous else None,
            recommended_params={"purge_level": 1},
            effective_params={"purge_level": 1},
            execution_params={"purge_level": 1},
            failure_signature={"return_code": 137} if retry else None,
            proposed_parameter_changes={"purge_level": {"from": 0, "to": 1}} if retry else {},
            parameter_reasoning={"purge_level": {"reason": "attempt0"}} if retry else {},
        ).model_dump(mode="json")
        payload = {"attempt": pending, "validation": {"valid": True}}
        artifact = root / "artifacts" / "assembly" / "s" / f"parameter_attempt_{attempt_id}" / "validated_recommendation.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        ref = ArtifactRef(
            path_ref=f"data/artifacts/assembly/s/parameter_attempt_{attempt_id}/validated_recommendation.json",
            sha256=digest,
            artifact_type="assembly.parameter.recommendation",
        )
        pending["parameter_recommendation_ref"] = ref.model_dump(mode="json")
        pending["parameter_recommendation_hash"] = digest
        interrupt = {
            "stage": "assembly",
            "gate_id": "parameter_approval",
            "attempt_id": attempt_id,
            "task": task,
            "previous_attempt_id": 0 if retry else None,
            "recommendation_hash": digest,
            "proposal_semantic_hash": _parameter_proposal_semantic_hash(pending),
            "decisions_allowed": ["approve_parameters", "reject_parameters"],
        }
        return {
            "session_id": "s",
            "data_dir": str(root),
            "current_stage": "assembly",
            "supervision_state": "blocked_human",
            "active_interrupt": interrupt,
            "stage_outputs": {
                "assembly": {
                    "status": "blocked_human",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": attempts,
                    "retry_counts": {"execution": 0, "review": 0},
                    "active_interrupt": interrupt,
                    "pending_parameter_recommendation": pending,
                }
            },
        }

    def _request(self, state: dict, decision: str = "approve_parameters", gate_id: str | None = "parameter_approval") -> ResumeReviewRequest:
        interrupt = state["active_interrupt"]
        return ResumeReviewRequest(
            session_id="s",
            stage="assembly",
            gate_id=gate_id,
            decision=decision,
            metadata={key: interrupt.get(key) for key in ("attempt_id", "task", "previous_attempt_id", "recommendation_hash")},
        )

    def test_initial_approval_appends_only_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp))
            self.assertEqual(state["stage_outputs"]["assembly"]["parameter_attempts"], [])
            result = resume_review_state(state, self._request(state))
            output = result["stage_outputs"]["assembly"]
            self.assertEqual([item["attempt_id"] for item in output["parameter_attempts"]], [0])
            self.assertEqual(output["status"], "parameters_approved")

    def test_retry_approval_consumes_only_execution_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp), retry=True)
            result = resume_review_state(state, self._request(state))
            output = result["stage_outputs"]["assembly"]
            self.assertEqual([item["attempt_id"] for item in output["parameter_attempts"]], [0, 1])
            self.assertEqual(output["retry_counts"], {"execution": 1, "review": 0})

    def test_rejection_does_not_append_or_consume_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp), retry=True)
            result = resume_review_state(state, self._request(state, "reject_parameters"))
            output = result["stage_outputs"]["assembly"]
            self.assertEqual(len(output["parameter_attempts"]), 1)
            self.assertEqual(output["retry_counts"], {"execution": 0, "review": 0})
            self.assertEqual(output["status"], "parameter_recommendation_rejected_by_human")

    def test_missing_gate_and_stale_hash_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp))
            with self.assertRaises(ResumeValidationError):
                resume_review_state(state, self._request(state, gate_id=None))
            request = self._request(state)
            request.metadata["recommendation_hash"] = "0" * 64
            with self.assertRaises(ResumeValidationError):
                resume_review_state(state, request)

    def test_pending_retry_semantic_tampering_fails_closed(self):
        mutations = {
            "retry_origin": lambda pending: pending.__setitem__("retry_origin", "assembly_review"),
            "catalog_snapshot": lambda pending: pending.__setitem__("catalog_snapshot", {"method_id": "changed"}),
            "inherited_ref": lambda pending: pending.__setitem__(
                "inherited_profile_ref",
                {"path_ref": "other", "sha256": "9" * 64, "artifact_type": "profile"},
            ),
            "failure_signature": lambda pending: pending.__setitem__("failure_signature", {"return_code": 1}),
            "parameter_delta": lambda pending: pending.__setitem__(
                "proposed_parameter_changes", {"purge_level": {"from": 1, "to": 2}}
            ),
            "parameter_reasoning": lambda pending: pending.__setitem__(
                "parameter_reasoning", {"purge_level": {"reason": "tampered"}}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                state = self._state(Path(tmp), retry=True)
                pending = state["stage_outputs"]["assembly"]["pending_parameter_recommendation"]
                mutate(pending)
                with self.assertRaises(ResumeValidationError):
                    resume_review_state(state, self._request(state))

    def test_mutated_initial_snapshot_in_blocked_retry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp), retry=True)
            initial = state["stage_outputs"]["assembly"]["parameter_attempts"][0]
            initial["catalog_snapshot"] = {"method_id": "tampered"}
            with self.assertRaises(ResumeValidationError):
                resume_review_state(state, self._request(state))

    def test_approval_rejects_noncontiguous_existing_chain_before_decision_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chain = self._three_attempt_chain(root / "source")
            target = root / "target"
            state = self._state(target, retry=True)
            state["stage_outputs"]["assembly"]["parameter_attempts"] = [chain[0], chain[2]]
            with self.assertRaises(ResumeValidationError):
                resume_review_state(state, self._request(state))
            decision_path = (
                target / "artifacts" / "assembly" / "s" / "parameter_attempt_1"
                / "parameter_approval_decision.json"
            )
            self.assertFalse(decision_path.exists())

    def test_complete_chain_rejects_delete_reorder_and_middle_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain = self._three_attempt_chain(Path(tmp))
            self.assertEqual(
                [item["attempt_id"] for item in _validate_parameter_attempt_chain(chain)],
                [0, 1, 2],
            )
            replaced = dict(chain[1])
            replaced.pop("sealed_at", None)
            replaced.pop("seal_hash", None)
            replaced["recommended_params"] = {"purge_level": 99}
            replaced = _seal_parameter_attempt(replaced)
            malformed = {
                "delete": [chain[0], chain[2]],
                "reorder": [chain[0], chain[2], chain[1]],
                "replace": [chain[0], replaced, chain[2]],
            }
            for label, candidate in malformed.items():
                with self.subTest(label=label), self.assertRaises(ValueError):
                    _validate_parameter_attempt_chain(candidate)

    def test_duplicate_approval_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp))
            result = resume_review_state(state, self._request(state))
            with self.assertRaises(ResumeValidationError):
                resume_review_state(result, self._request(state))

    def test_execution_retry_gate_does_not_preconsume_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp), retry=True)
            output = state["stage_outputs"]["assembly"]
            output["parameter_attempts"] = [output["parameter_attempts"][0]]
            output["status"] = "failed"
            output["parameter_recommendation_config"] = {"max_execution_retries": 1}
            update = execution_retry_gate(state)["stage_outputs"]["assembly"]
            self.assertEqual(update["status"], "retry_parameters_after_execution_failure")
            self.assertEqual(update["retry_counts"]["execution"], 0)
            self.assertEqual(update["proposed_parameter_attempt_id"], 1)

    def test_execution_retry_tails_survive_block_approval_and_terminal_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = self._state(root, retry=True)
            output = state["stage_outputs"]["assembly"]
            previous = dict(output["parameter_attempts"][0])
            previous.pop("sealed_at", None)
            previous.pop("seal_hash", None)
            stderr_path = root / "stderr.log"
            stdout_path = root / "stdout.log"
            stderr_path.write_text("allocator warning\nOOM killed\n", encoding="utf-8")
            stdout_path.write_text("hifiasm starting\nloaded reads\n", encoding="utf-8")
            previous["execution_result"] = {
                "status": "failed",
                "return_code": 137,
                "stderr_path": str(stderr_path),
                "stdout_path": str(stdout_path),
            }
            output.update(
                status="failed",
                parameter_attempts=[previous],
                current_parameter_attempt_id=0,
                parameter_recommendation_config={"max_execution_retries": 1},
            )
            transitioned = execution_retry_gate(state)
            transitioned_output = transitioned["stage_outputs"]["assembly"]
            frozen_previous = transitioned_output["parameter_attempts"][0]
            self.assertIn("OOM killed", frozen_previous["stderr_tail"])
            self.assertIn("loaded reads", frozen_previous["stdout_tail"])
            self.assertEqual(
                _seal_parameter_attempt(frozen_previous)["seal_hash"], frozen_previous["seal_hash"]
            )

            blocked = self._state(root, retry=True)
            blocked_output = blocked["stage_outputs"]["assembly"]
            blocked_output["parameter_attempts"] = [frozen_previous]
            pending = blocked_output["pending_parameter_recommendation"]
            pending["previous_attempt_seal_hash"] = frozen_previous["seal_hash"]
            pending["inherited_initial_snapshot_ref"] = frozen_previous["seal_hash"]
            pending["inherited_profile_ref"] = frozen_previous["input_profile_ref"]
            artifact_ref = pending["parameter_recommendation_ref"]
            artifact_path = root / artifact_ref["path_ref"].removeprefix("data/")
            artifact_attempt = dict(pending)
            artifact_attempt["parameter_recommendation_ref"] = None
            artifact_attempt["parameter_recommendation_hash"] = None
            artifact_path.write_text(
                json.dumps({"attempt": artifact_attempt, "validation": {"valid": True}}),
                encoding="utf-8",
            )
            digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            artifact_ref["sha256"] = digest
            pending["parameter_recommendation_hash"] = digest
            interrupt = blocked["active_interrupt"]
            interrupt["recommendation_ref"] = artifact_ref
            interrupt["recommendation_hash"] = digest
            interrupt["proposal_semantic_hash"] = _parameter_proposal_semantic_hash(pending)
            blocked_output["active_interrupt"] = interrupt

            approved = resume_review_state(blocked, self._request(blocked))
            terminal_output = approved["stage_outputs"]["assembly"]
            completed = dict(terminal_output["parameter_attempts"][-1])
            completed["execution_result"] = {"status": "succeeded", "return_code": 0}
            terminal_output["parameter_attempts"][-1] = _seal_parameter_attempt(completed)
            timeline = build_parameter_attempt_timeline(
                terminal_output, status="assembly_succeeded", reason="completed"
            )
            self.assertIn("OOM killed", timeline["entries"][0]["stderr_tail"])
            self.assertIn("loaded reads", timeline["entries"][0]["stdout_tail"])
            self.assertEqual(timeline["terminal"]["status"], "assembly_succeeded")

    def test_review_approval_uses_independent_budget_and_timeline_is_derived(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(Path(tmp), retry=True)
            interrupt = state["active_interrupt"]
            pending = state["stage_outputs"]["assembly"]["pending_parameter_recommendation"]
            interrupt["task"] = "retry_after_review_failure"
            pending["task"] = "retry_after_review_failure"
            pending["retry_origin"] = "assembly_review"
            artifact_ref = pending["parameter_recommendation_ref"]
            artifact_path = Path(tmp) / artifact_ref["path_ref"].removeprefix("data/")
            artifact_attempt = dict(pending)
            artifact_attempt["parameter_recommendation_ref"] = None
            artifact_attempt["parameter_recommendation_hash"] = None
            artifact_path.write_text(
                json.dumps({"attempt": artifact_attempt, "validation": {"valid": True}}),
                encoding="utf-8",
            )
            digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            artifact_ref["sha256"] = digest
            pending["parameter_recommendation_hash"] = digest
            interrupt["recommendation_ref"] = artifact_ref
            interrupt["recommendation_hash"] = digest
            interrupt["proposal_semantic_hash"] = _parameter_proposal_semantic_hash(pending)
            state["stage_outputs"]["assembly"]["active_interrupt"] = interrupt
            result = resume_review_state(state, self._request(state))
            output = result["stage_outputs"]["assembly"]
            self.assertEqual(output["retry_counts"], {"execution": 0, "review": 1})

            sealed = []
            for item in output["parameter_attempts"]:
                current = dict(item)
                current["execution_result"] = current.get("execution_result") or {"status": "succeeded"}
                sealed.append(_seal_parameter_attempt(current))
            output["parameter_attempts"] = sealed
            timeline = build_parameter_attempt_timeline(
                output, status="assembly_succeeded", reason="completed"
            )
            self.assertEqual([entry["attempt_id"] for entry in timeline["entries"]], [0, 1])
            self.assertEqual(timeline["terminal"]["review_retries_used"], 1)

    def test_mixed_execution_then_review_retry_chain_keeps_budgets_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = self._state(root, retry=True)
            execution_update = resume_review_state(state, self._request(state))
            mixed = {**state, **execution_update}
            output = dict(mixed["stage_outputs"]["assembly"])
            attempt1 = dict(output["parameter_attempts"][-1])
            attempt1["execution_result"] = {"status": "succeeded", "return_code": 0}
            attempt1["review_failure"] = {"failure_mode": "under_purged"}
            attempt1 = _seal_parameter_attempt(attempt1)
            output["parameter_attempts"][-1] = attempt1

            artifact = root / "artifacts" / "assembly" / "s" / "parameter_attempt_2" / "validated_recommendation.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            pending = ParameterAttempt(
                attempt_id=2,
                method_id="assembly.candidate.hifiasm",
                status="recommended",
                task="retry_after_review_failure",
                input_profile_ref=ArtifactRef(path_ref="profile2", sha256="1" * 64, artifact_type="profile"),
                prompt_payload_ref=ArtifactRef(path_ref="prompt2", sha256="2" * 64, artifact_type="prompt"),
                previous_attempt_id=1,
                previous_attempt_seal_hash=attempt1["seal_hash"],
                attempt_kind="retry",
                retry_origin="assembly_review",
                inherited_initial_attempt_id=0,
                inherited_initial_snapshot_ref=output["parameter_attempts"][0]["seal_hash"],
                inherited_profile_ref=output["parameter_attempts"][0]["input_profile_ref"],
                catalog_snapshot={},
                recommended_params={"purge_level": 2},
                execution_params={"purge_level": 2},
            ).model_dump(mode="json")
            artifact.write_text(json.dumps({"attempt": pending, "validation": {}}, sort_keys=True), encoding="utf-8")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            ref = ArtifactRef(
                path_ref="data/artifacts/assembly/s/parameter_attempt_2/validated_recommendation.json",
                sha256=digest,
                artifact_type="assembly.parameter.recommendation",
            )
            pending["parameter_recommendation_ref"] = ref.model_dump(mode="json")
            pending["parameter_recommendation_hash"] = digest
            interrupt = {
                "stage": "assembly",
                "gate_id": "parameter_approval",
                "attempt_id": 2,
                "task": "retry_after_review_failure",
                "previous_attempt_id": 1,
                "recommendation_hash": digest,
                "proposal_semantic_hash": _parameter_proposal_semantic_hash(pending),
                "decisions_allowed": ["approve_parameters", "reject_parameters"],
            }
            output.update(
                status="blocked_human",
                active_interrupt=interrupt,
                pending_parameter_recommendation=pending,
            )
            mixed.update(
                supervision_state="blocked_human",
                active_interrupt=interrupt,
                stage_outputs={**mixed["stage_outputs"], "assembly": output},
            )
            review_update = resume_review_state(mixed, self._request(mixed))
            final = review_update["stage_outputs"]["assembly"]
            self.assertEqual([item["attempt_id"] for item in final["parameter_attempts"]], [0, 1, 2])
            self.assertEqual(final["retry_counts"], {"execution": 1, "review": 1})


if __name__ == "__main__":
    unittest.main()
