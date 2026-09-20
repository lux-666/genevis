"""Contract tests for the shared executable-method lifecycle."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from app.control.workflow_plan import bind_attempt_to_current_workflow_step
from app.execution.attempt_owners import ParameterAttemptOwner
from app.execution.evidence import execution_log_paths
from app.execution.fake_runner import FakeRunner
from app.execution.lifecycle import (
    MethodExecutionLifecycle,
    build_method_graph,
    freeze_method_attempts,
    load_method_executions,
    run_method_attempts,
)
from app.execution.method_outcomes import MethodOutcome, settle_method_outcomes
from app.execution.method_runner import build_frozen_attempt
from app.llm.parameters.assembly.request import build_parameter_request_dto
from app.review.persistence import persist_review_decision, persist_review_request
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ASSEMBLY_PARAMETER_ATTEMPT_MANIFEST,
    ASSEMBLY_PARAMETER_BASIS,
    ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    ArtifactRef,
    ParameterAttempt,
)
from app.schemas.descriptors import FrozenAttemptSpec
from app.schemas.execution import ExecutionResult
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
    ExecutionFailureTrigger,
    ParameterAttemptEvidenceV3,
    ParameterRecommendationManifestV3,
    RecommendationInputSnapshotV3,
    RecommendationRetryContext,
)
from app.schemas.state import initial_state
from app.stages.base import stage_output, update_stage_output
from app.storage.artifacts import read_artifact_records
from app.storage.ledger import Ledger
from app.storage.parameter_evidence import (
    load_approved_parameter_chain_v3,
    load_parameter_recommendation_chain_v3,
    publish_parameter_json,
)
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir

DIRECT_LIFECYCLE = MethodExecutionLifecycle(
    stage="assembly_review",
    name="contract method",
    method_name="assembly_review.eval.quast",
    frozen_status="contract_frozen",
    ran_status="contract_ran",
    outcome_policy=lambda _state: None,
)


def _state(tmpdir: str, *, stage: str = "assembly_review") -> dict:
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(v2_run_dir(tmpdir, "s1")),
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
                "selected_method_names": [],
            },
            {
                "step_id": f"step-001-{stage}",
                "stage": stage,
                "allow_local_execution": False,
                "selected_method_names": (
                    ["assembly_review.eval.quast"]
                    if stage == "assembly_review"
                    else ["assembly.candidate.flye"]
                ),
                **(
                    {
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-000-inventory",
                        }
                    }
                    if stage == "assembly_review"
                    else {}
                ),
            },
            *(
                [
                    {
                        "step_id": "step-002-assembly_review",
                        "stage": "assembly_review",
                        "allow_local_execution": False,
                        "selected_method_names": ["assembly_review.eval.quast"],
                        "review_target": {
                            "resource_type": "assembly",
                            "producer_step_id": "step-001-assembly",
                        },
                    }
                ]
                if stage == "assembly"
                else []
            ),
        ],
    )
    state["current_stage"] = stage
    state["workflow_step_index"] = 1
    state["entered_workflow_step_index"] = 1
    state["stage_outputs"] = {stage: {"status": "planned"}}
    return state


def _attempt(
    state: dict,
    *,
    attempt_id: str = "s1:contract:0",
    stage: str = "assembly_review",
    method_name: str = "assembly_review.eval.quast",
    parameter_attempt_id: int | None = None,
    script_body: str = "#!/bin/sh\nexit 1\n",
) -> FrozenAttemptSpec:
    workflow_step_id = state["workflow_steps"][state["workflow_step_index"]]["step_id"]
    work_dir = SessionPaths.from_run_dir(state["data_dir"]).workflow_step_work_path(
        workflow_step_id,
        stage,
        attempt_id.replace(":", "_"),
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "run.sh"
    script.write_text(script_body, encoding="utf-8")
    script.chmod(0o755)
    attempt = build_frozen_attempt(
        attempt_id=attempt_id,
        session_id=state["session_id"],
        stage=stage,
        method_name=method_name,
        script_path=script,
        working_dir=work_dir,
        expected_outputs=[work_dir / "output.txt"],
        env=None,
        env_spec=None,
        metadata={
            **(
                {"parameter_attempt_id": parameter_attempt_id}
                if parameter_attempt_id is not None
                else {}
            ),
        },
    )
    return bind_attempt_to_current_workflow_step(state, attempt)


def _bound_attempt_id(state: dict, attempt_id: str) -> str:
    session_prefix = f"{state['session_id']}:"
    local_attempt_id = attempt_id.removeprefix(session_prefix)
    workflow_step_id = state["workflow_steps"][state["workflow_step_index"]]["step_id"]
    return f"{session_prefix}{workflow_step_id}:{local_attempt_id}"


def _merge(state: dict, update: dict) -> dict:
    return {**state, **update}


def _freeze_contract(state: dict) -> dict:
    if stage_output(state, DIRECT_LIFECYCLE.stage).get("status") != "planned":
        return {}
    return freeze_method_attempts(
        state,
        DIRECT_LIFECYCLE,
        [_attempt(state)],
    )


def _collect_contract(state: dict) -> dict:
    executions = load_method_executions(state, DIRECT_LIFECYCLE)
    if len(executions) != 1:
        raise AssertionError("expected one contract execution")
    return update_stage_output(
        state,
        DIRECT_LIFECYCLE.stage,
        {"status": "collected"},
    )


class _OutcomeRunner:
    def __init__(self, failures: set[str]):
        self.failures = failures
        self.attempt_ids: list[str] = []

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        self.attempt_ids.append(spec.attempt_id)
        spec.working_dir.mkdir(parents=True, exist_ok=True)
        stdout = spec.working_dir / "stdout.log"
        stderr = spec.working_dir / "stderr.log"
        stdout.write_text("", encoding="utf-8")
        failed = spec.attempt_id in self.failures
        stderr.write_text("expected failure" if failed else "", encoding="utf-8")
        if not failed:
            for output in spec.expected_outputs:
                output.write_text("ok\n", encoding="utf-8")
        return ExecutionResult(
            attempt_id=spec.attempt_id,
            success=not failed,
            return_code=7 if failed else 0,
            stdout_path=stdout,
            stderr_path=stderr,
            output_paths=list(spec.expected_outputs),
            status="failed" if failed else "ok",
        )


class _RaisingRunner:
    def __init__(self) -> None:
        self.attempt_ids: list[str] = []

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        self.attempt_ids.append(spec.attempt_id)
        raise FileNotFoundError("runner binary missing")


class _BuggyRunner:
    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        raise RuntimeError(f"runner contract bug for {spec.attempt_id}")


class _ContractValueErrorRunner:
    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        raise ValueError(f"runner contract bug for {spec.attempt_id}")


class _MismatchedRunner(_OutcomeRunner):
    def __init__(self) -> None:
        super().__init__(set())

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        result = super().run(spec)
        return result.model_copy(update={"attempt_id": "s1:wrong:99"})


class _LargeStderrRunner:
    def __init__(self, stderr_text: str) -> None:
        self.stderr_text = stderr_text

    def run(self, spec: FrozenAttemptSpec) -> ExecutionResult:
        stdout = spec.working_dir / "stdout.log"
        stderr = spec.working_dir / "stderr.log"
        stdout.write_text("", encoding="utf-8")
        stderr.write_text(self.stderr_text, encoding="utf-8")
        return ExecutionResult(
            attempt_id=spec.attempt_id,
            success=False,
            return_code=7,
            stdout_path=stdout,
            stderr_path=stderr,
            output_paths=list(spec.expected_outputs),
            status="failed",
        )


def _ref(name: str, artifact_type: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=f"art_{name * 32}",
        path_ref=f"artifacts/steps/step-001-assembly/assembly/{name}.json",
        sha256=name * 64,
        size_bytes=1,
        artifact_type=artifact_type,
    )


@dataclass(frozen=True)
class _V3RecommendationFixture:
    manifest_ref: ArtifactRef
    input_snapshot_ref: ArtifactRef
    approval_decision_ref: ArtifactRef
    attempt_manifest_ref: ArtifactRef | None = None
    seal_hash: str | None = None


def _publish_v3_parameter_record(
    state: dict,
    *,
    attempt_id: int,
    filename: str,
    payload: dict,
    artifact_type: str,
    scope: str = "recommendation",
) -> ArtifactRef:
    return publish_parameter_json(
        state,
        stage="assembly",
        scope=scope,
        identifier=attempt_id,
        filename=filename,
        payload=payload,
        artifact_type=artifact_type,
        method_name="assembly.contract",
    )


def _persist_v3_recommendation_fixture(
    state: dict,
    *,
    attempt_id: int,
    task: str,
    previous: _V3RecommendationFixture | None = None,
    seal_for_retry: bool = False,
) -> _V3RecommendationFixture:
    """Publish a complete canonical-A recommendation and approval chain."""

    if task == "recommend_initial":
        if attempt_id != 0 or previous is not None:
            raise AssertionError("initial v3 fixture cannot bind a predecessor")
        retry = None
        dependency_refs = {}
    else:
        if (
            attempt_id != 1
            or previous is None
            or previous.attempt_manifest_ref is None
            or previous.seal_hash is None
        ):
            raise AssertionError("retry v3 fixture requires one sealed predecessor")
        retry = RecommendationRetryContext(
            trigger=ExecutionFailureTrigger(
                status="failed",
                failure_class="fixture_failure",
            ),
            attempt_history=(
                ParameterAttemptEvidenceV3(
                    attempt_id=0,
                    task="recommend_initial",
                    status="execution_failed",
                    recommended_parameters={},
                    approved_parameters={},
                    effective_parameters={},
                    effective_sources={},
                ),
            ),
        )
        dependency_refs = {
            "initial_input_snapshot_ref": previous.input_snapshot_ref,
            "prior_attempt_manifest_refs": (previous.attempt_manifest_ref,),
        }

    snapshot = RecommendationInputSnapshotV3(
        schema_version=3,
        method_id="assembly.contract",
        task=task,
        facts=CanonicalRecommendationFacts(),
        stage_context=AssemblyRecommendationContext(
            stage="assembly",
            assembly_route="hifi",
            read_type="pacbio_hifi",
            target_level="draft",
            retry=retry,
        ),
        dependency_refs=dependency_refs,
    )
    catalog = MethodParameterCatalog(
        catalog_id="contract",
        method_id="assembly.contract",
        tool="contract",
        parameters={},
    )
    input_snapshot_ref = _publish_v3_parameter_record(
        state,
        attempt_id=attempt_id,
        filename="input_snapshot.json",
        payload=snapshot.model_dump(mode="json"),
        artifact_type=ASSEMBLY_PARAMETER_INPUT_SNAPSHOT,
    )
    basis = {
        "schema_version": 3,
        "method_id": "assembly.contract",
        "catalog_snapshot": catalog.model_dump(mode="json"),
        "consumer_version": 5,
        "knowledge_evidence": {
            "purpose": "parameters",
            "stage": "assembly",
            "task": "contig_assembly",
            "limitations": [],
            "target_method": catalog.tool,
            "cases": [],
        },
        "material_query": {},
        "method_decision": {"method_id": catalog.method_id},
        "method_guidance": [],
    }
    basis_snapshot_ref = _publish_v3_parameter_record(
        state,
        attempt_id=attempt_id,
        filename="basis_snapshot.json",
        payload=basis,
        artifact_type=ASSEMBLY_PARAMETER_BASIS,
    )
    dto = build_parameter_request_dto(
        method_id=catalog.method_id,
        catalog=catalog,
        facts=snapshot.facts,
        context=snapshot.stage_context,
        task=task,
        basis_snapshot=basis,
    )
    model_request_ref = _publish_v3_parameter_record(
        state,
        attempt_id=attempt_id,
        filename="model_request.json",
        payload={
            "schema_version": 3,
            "parameter_request_schema_version": 3,
            "input_snapshot_sha256": input_snapshot_ref.sha256,
            "basis_sha256": basis_snapshot_ref.sha256,
            "messages": [
                {"role": "system", "content": "Lifecycle fixture"},
                {"role": "user", "content": json.dumps(dto)},
            ],
        },
        artifact_type=ASSEMBLY_PARAMETER_MODEL_REQUEST,
    )
    model_response_ref = _publish_v3_parameter_record(
        state,
        attempt_id=attempt_id,
        filename="model_response.json",
        payload={"schema_version": 2, "validity": "parsed"},
        artifact_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    )
    response_parse_ref = _publish_v3_parameter_record(
        state,
        attempt_id=attempt_id,
        filename="response_parse.json",
        payload={"schema_version": 1, "parsed": True},
        artifact_type=ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    )
    recommendation_ref = _publish_v3_parameter_record(
        state,
        attempt_id=attempt_id,
        filename="recommendation.json",
        payload={
            "schema_version": 2,
            "method_id": "assembly.contract",
            "task": task,
            "recommendation": {"parameters": {}},
        },
        artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION,
    )
    manifest = ParameterRecommendationManifestV3(
        schema_version=3,
        recommendation_contract="parameter_recommendation_v1",
        stage="assembly",
        method_id="assembly.contract",
        task=task,
        recommendation_id=attempt_id,
        status="pending_approval",
        previous_attempt_id=(0 if previous is not None else None),
        previous_attempt_manifest_ref=(
            previous.attempt_manifest_ref if previous is not None else None
        ),
        previous_attempt_seal_hash=(
            previous.seal_hash if previous is not None else None
        ),
        refs={
            "input_snapshot_ref": input_snapshot_ref,
            "basis_snapshot_ref": basis_snapshot_ref,
            "model_request_ref": model_request_ref,
            "model_response_ref": model_response_ref,
            "response_parse_ref": response_parse_ref,
            "recommendation_ref": recommendation_ref,
        },
    )
    manifest_ref = _publish_v3_parameter_record(
        state,
        attempt_id=attempt_id,
        filename="recommendation_manifest.json",
        payload=manifest.model_dump(mode="json"),
        artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    )
    request = persist_review_request(
        state,
        stage="assembly",
        gate_id="parameter_approval",
        reason="execution_lifecycle_v3_fixture",
        payload={
            "schema_version": 2,
            "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
            "recommendation_manifest_hash": manifest_ref.sha256,
            "decisions_allowed": ["approve_parameters", "reject_parameters"],
        },
        bindings={
            "attempt_id": attempt_id,
            "task": task,
            "previous_attempt_id": 0 if previous is not None else None,
            "recommendation_hash": manifest_ref.sha256,
        },
    )
    approval_decision_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=request.review_request_id,
        stage="assembly",
        gate_id="parameter_approval",
        decision={
            "schema_version": 2,
            "decision": "approve_parameters",
            "validated_recommendation_manifest_ref": manifest_ref.model_dump(
                mode="json"
            ),
            "validated_recommendation_hash": manifest_ref.sha256,
        },
        request_ref=request.request_ref,
        request_hash=request.request_hash,
        artifact_type=ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    )
    load_parameter_recommendation_chain_v3(
        state,
        manifest_ref=manifest_ref,
        expected_stage="assembly",
        expected_method_id="assembly.contract",
    )

    attempt_manifest_ref = None
    seal_hash = None
    if seal_for_retry:
        seal_hash = "c" * 64
        attempt_manifest_ref = _publish_v3_parameter_record(
            state,
            attempt_id=attempt_id,
            filename="attempt_manifest.json",
            payload={
                "schema_version": 3,
                "stage": "assembly",
                "attempt_id": attempt_id,
                "method_id": "assembly.contract",
                "task": task,
                "status": "execution_failed",
                "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
                "approval_decision_ref": approval_decision_ref.model_dump(mode="json"),
                "seal_hash": seal_hash,
            },
            artifact_type=ASSEMBLY_PARAMETER_ATTEMPT_MANIFEST,
            scope="attempt",
        )
    return _V3RecommendationFixture(
        manifest_ref=manifest_ref,
        input_snapshot_ref=input_snapshot_ref,
        approval_decision_ref=approval_decision_ref,
        attempt_manifest_ref=attempt_manifest_ref,
        seal_hash=seal_hash,
    )


def _v3_parameter_attempt(
    state: dict,
    *,
    frozen_attempt_ref: ArtifactRef,
    status: str = "frozen",
    execution_ref: ArtifactRef | None = None,
    retry: bool = False,
) -> ParameterAttempt:
    previous = None
    if retry:
        previous = _persist_v3_recommendation_fixture(
            state,
            attempt_id=0,
            task="recommend_initial",
            seal_for_retry=True,
        )
    attempt_id = 1 if retry else 0
    task = "retry_after_execution_failure" if retry else "recommend_initial"
    fixture = _persist_v3_recommendation_fixture(
        state,
        attempt_id=attempt_id,
        task=task,
        previous=previous,
    )
    attempt = ParameterAttempt(
        schema_version=3,
        attempt_id=attempt_id,
        method_id="assembly.contract",
        status=status,
        task=task,
        previous_attempt_id=0 if retry else None,
        previous_attempt_seal_hash=previous.seal_hash if previous else None,
        recommendation_manifest_ref=fixture.manifest_ref,
        approval_decision_ref=fixture.approval_decision_ref,
        frozen_attempt_ref=frozen_attempt_ref,
        execution_ref=execution_ref,
    )
    load_approved_parameter_chain_v3(
        state,
        attempt=attempt,
        expected_stage="assembly",
        expected_method_id="assembly.contract",
    )
    return attempt


class ExecutionLifecycleContractTests(unittest.TestCase):
    def test_method_graph_requires_an_outcome_policy(self):
        lifecycle = MethodExecutionLifecycle(
            stage="assembly_review",
            name="missing outcome contract",
            method_name="assembly_review.eval.quast",
            frozen_status="outcome_frozen",
            ran_status="outcome_ran",
        )

        with self.assertRaisesRegex(ValueError, "requires an outcome_policy"):
            build_method_graph(lifecycle, _freeze_contract, _collect_contract)

    def test_success_outcome_policy_settles_once_and_exact_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            lifecycle = MethodExecutionLifecycle(
                stage="assembly_review",
                name="outcome contract",
                method_name="assembly_review.eval.quast",
                frozen_status="outcome_frozen",
                ran_status="outcome_ran",
                outcome_policy=lambda _state: MethodOutcome(
                    method_name="assembly_review.eval.quast",
                    disposition="completed",
                ),
            )
            graph = build_method_graph(
                lifecycle,
                lambda state: update_stage_output(
                    state, "assembly_review", {"status": "settled"}
                ),
                _collect_contract,
            )

            graph.invoke(state)
            settle_method_outcomes(
                state,
                stage="assembly_review",
                outcomes=MethodOutcome(
                    method_name="assembly_review.eval.quast",
                    disposition="completed",
                ),
                expected_method_names=("assembly_review.eval.quast",),
            )

            events = [
                event
                for event in Ledger(state["data_dir"]).read_events(state["session_id"])
                if event["event_type"] == "method_disposition_recorded"
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["method_name"], "assembly_review.eval.quast")
            self.assertEqual(events[0]["disposition"], "completed")

    def test_outcome_settlement_rejects_invalid_registered_method_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            outcome = MethodOutcome(
                method_name="other.method",
                disposition="skipped",
                reason="planner selected another method",
            )

            with self.assertRaisesRegex(ValueError, "not selected|not registered"):
                settle_method_outcomes(
                    state,
                    stage="assembly_review",
                    outcomes=outcome,
                    expected_method_names=("assembly_review.eval.quast",),
                )
            with self.assertRaisesRegex(ValueError, "duplicate method outcome"):
                settle_method_outcomes(
                    state,
                    stage="assembly_review",
                    outcomes=(
                        MethodOutcome(
                            method_name="assembly_review.eval.quast",
                            disposition="completed",
                        ),
                    )
                    * 2,
                )
            self.assertEqual(
                Ledger(state["data_dir"]).read_events(state["session_id"]),
                [],
            )

    def test_none_outcome_policy_is_a_noop_after_the_shared_settlement_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            order: list[str] = []

            def freeze_without_attempt(state: dict) -> dict:
                order.append("freeze")
                return update_stage_output(
                    state,
                    "assembly_review",
                    {"status": "planner_continues"},
                )

            def retry_continuation_policy(_state: dict) -> None:
                order.append("settle")
                return None

            lifecycle = MethodExecutionLifecycle(
                stage="assembly_review",
                name="outcome contract",
                method_name="assembly_review.eval.quast",
                frozen_status="outcome_frozen",
                ran_status="outcome_ran",
                outcome_policy=retry_continuation_policy,
            )
            graph = build_method_graph(
                lifecycle,
                freeze_without_attempt,
                _collect_contract,
            )

            graph.invoke(state)

            self.assertEqual(order, ["freeze", "settle"])
            self.assertEqual(
                Ledger(state["data_dir"]).read_events(state["session_id"]),
                [],
            )
            edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}
            self.assertIn(("freeze", "settle_method_outcomes"), edges)
            self.assertIn(("settle_method_outcomes", "__end__"), edges)

    def test_direct_graph_uses_injected_runner_and_collects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            runner = FakeRunner()
            graph = build_method_graph(
                DIRECT_LIFECYCLE,
                _freeze_contract,
                _collect_contract,
                runner=runner,
            )

            result = graph.invoke(state)

            output = result["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "collected")
            self.assertEqual(len(runner.executed_commands), 1)
            self.assertEqual(output["execution_attempts"][0]["status"], "executed")

    def test_ran_reentry_collects_without_reexecuting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))
            ran = _merge(
                frozen,
                run_method_attempts(
                    frozen,
                    DIRECT_LIFECYCLE,
                    runner=FakeRunner(),
                ),
            )
            replay_runner = FakeRunner()

            def fail_if_refrozen(_state: dict) -> dict:
                raise AssertionError("ran checkpoint must bypass freeze")

            graph = build_method_graph(
                DIRECT_LIFECYCLE,
                fail_if_refrozen,
                _collect_contract,
                runner=replay_runner,
            )

            result = graph.invoke(ran)

            self.assertEqual(
                result["stage_outputs"]["assembly_review"]["status"],
                "collected",
            )
            self.assertEqual(replay_runner.executed_commands, [])

    def test_frozen_reentry_runs_without_rebuilding_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))
            frozen_ref = frozen["stage_outputs"]["assembly_review"][
                "execution_attempts"
            ][0]["frozen_attempt_ref"]
            runner = FakeRunner()

            def fail_if_refrozen(_state: dict) -> dict:
                raise AssertionError("frozen checkpoint must bypass freeze")

            graph = build_method_graph(
                DIRECT_LIFECYCLE,
                fail_if_refrozen,
                _collect_contract,
                runner=runner,
            )

            result = graph.invoke(frozen)

            output = result["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "collected")
            self.assertEqual(len(runner.executed_commands), 1)
            self.assertEqual(
                output["execution_attempts"][0]["frozen_attempt_ref"],
                frozen_ref,
            )

    def test_discarded_run_update_recovers_evidence_without_reexecuting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))
            first_runner = _OutcomeRunner(set())

            discarded = run_method_attempts(
                frozen,
                DIRECT_LIFECYCLE,
                runner=first_runner,
            )
            discarded_output = discarded["stage_outputs"]["assembly_review"]
            execution_ref = discarded_output["execution_attempts"][0]["execution_ref"]
            replay_runner = _OutcomeRunner(set())

            recovered = run_method_attempts(
                frozen,
                DIRECT_LIFECYCLE,
                runner=replay_runner,
            )

            output = recovered["stage_outputs"]["assembly_review"]
            self.assertEqual(
                first_runner.attempt_ids,
                [_bound_attempt_id(state, "s1:contract:0")],
            )
            self.assertEqual(replay_runner.attempt_ids, [])
            self.assertEqual(output["status"], DIRECT_LIFECYCLE.ran_status)
            self.assertEqual(output["execution_attempts"][0]["status"], "executed")
            self.assertEqual(
                output["execution_attempts"][0]["execution_ref"],
                execution_ref,
            )

    def test_recovery_rejects_evidence_for_another_frozen_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            original = _attempt(state)
            original_frozen = _merge(
                state,
                freeze_method_attempts(state, DIRECT_LIFECYCLE, [original]),
            )
            run_method_attempts(
                original_frozen,
                DIRECT_LIFECYCLE,
                runner=_OutcomeRunner(set()),
            )
            replacement = _attempt(
                state,
                script_body="#!/bin/sh\n# replacement frozen command\nexit 0\n",
            )
            replacement_frozen = _merge(
                state,
                freeze_method_attempts(state, DIRECT_LIFECYCLE, [replacement]),
            )
            replay_runner = _OutcomeRunner(set())

            update = run_method_attempts(
                replacement_frozen,
                DIRECT_LIFECYCLE,
                runner=replay_runner,
            )

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("does not bind the frozen attempt", output["reason"])
            self.assertEqual(replay_runner.attempt_ids, [])
            self.assertEqual(output["execution_attempts"][0]["status"], "frozen")
            self.assertIsNone(output["execution_attempts"][0]["execution_ref"])

    def test_script_tampering_fails_before_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            attempt = _attempt(state)
            frozen = _merge(
                state,
                freeze_method_attempts(state, DIRECT_LIFECYCLE, [attempt]),
            )
            attempt.script_path.write_text(
                "#!/bin/sh\necho tampered\nexit 0\n",
                encoding="utf-8",
            )
            runner = _OutcomeRunner(set())

            update = run_method_attempts(
                frozen,
                DIRECT_LIFECYCLE,
                runner=runner,
            )

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("script changed after freeze", output["reason"])
            self.assertEqual(runner.attempt_ids, [])

    def test_direct_cursor_identity_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))
            frozen["stage_outputs"]["assembly_review"]["execution_attempts"][0][
                "method_id"
            ] = "contract.other"

            update = run_method_attempts(frozen, DIRECT_LIFECYCLE)

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("wrong method identity", output["reason"])

    def test_direct_run_restores_ran_status_when_stage_is_rewound(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))
            ran = _merge(
                frozen,
                run_method_attempts(
                    frozen,
                    DIRECT_LIFECYCLE,
                    runner=FakeRunner(),
                ),
            )
            ran["stage_outputs"]["assembly_review"]["status"] = (
                DIRECT_LIFECYCLE.frozen_status
            )
            replay_runner = FakeRunner()

            update = run_method_attempts(
                ran,
                DIRECT_LIFECYCLE,
                runner=replay_runner,
            )

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], DIRECT_LIFECYCLE.ran_status)
            self.assertEqual(output["execution_attempts"][0]["status"], "executed")
            self.assertEqual(replay_runner.executed_commands, [])

    def test_collect_mode_persists_mixed_batch_in_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            lifecycle = MethodExecutionLifecycle(
                stage="assembly_review",
                name="contract batch",
                method_name="assembly_review.eval.quast",
                frozen_status="batch_frozen",
                ran_status="batch_ran",
                process_failure="collect",
            )
            attempts = [
                _attempt(state, attempt_id="s1:contract:0"),
                _attempt(state, attempt_id="s1:contract:1"),
            ]
            frozen = _merge(
                state,
                freeze_method_attempts(state, lifecycle, attempts),
            )
            runner = _OutcomeRunner({_bound_attempt_id(state, "s1:contract:0")})

            after_first = _merge(
                frozen,
                run_method_attempts(frozen, lifecycle, runner=runner),
            )
            first_output = after_first["stage_outputs"]["assembly_review"]
            self.assertEqual(first_output["status"], "batch_frozen")
            self.assertEqual(
                runner.attempt_ids,
                [_bound_attempt_id(state, "s1:contract:0")],
            )
            self.assertEqual(
                [cursor["status"] for cursor in first_output["execution_attempts"]],
                ["execution_failed", "frozen"],
            )

            ran = _merge(
                after_first,
                run_method_attempts(after_first, lifecycle, runner=runner),
            )

            output = ran["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "batch_ran")
            self.assertEqual(
                runner.attempt_ids,
                [
                    _bound_attempt_id(state, "s1:contract:0"),
                    _bound_attempt_id(state, "s1:contract:1"),
                ],
            )
            self.assertEqual(
                [cursor["status"] for cursor in output["execution_attempts"]],
                ["execution_failed", "executed"],
            )
            self.assertEqual(
                [
                    execution.evidence.success
                    for execution in load_method_executions(ran, lifecycle)
                ],
                [False, True],
            )

    def test_runner_exception_becomes_bounded_failed_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            runner = _RaisingRunner()
            graph = build_method_graph(
                DIRECT_LIFECYCLE,
                _freeze_contract,
                _collect_contract,
                runner=runner,
            )

            result = graph.invoke(state)

            output = result["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("runner binary missing", output["reason"])
            self.assertLessEqual(len(output["reason"]), 2048)
            self.assertEqual(
                runner.attempt_ids,
                [_bound_attempt_id(state, "s1:contract:0")],
            )
            [execution] = load_method_executions(result, DIRECT_LIFECYCLE)
            self.assertFalse(execution.evidence.success)
            self.assertIn("runner binary missing", execution.evidence.error)

    def test_runner_programming_error_remains_visible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))

            with self.assertRaisesRegex(RuntimeError, "runner contract bug"):
                run_method_attempts(
                    frozen,
                    DIRECT_LIFECYCLE,
                    runner=_BuggyRunner(),
                )

    def test_runner_value_error_remains_visible(self):
        # A ValueError escaping the runner is a contract bug, not an expected
        # execution failure; it must propagate instead of becoming a bounded
        # failed status.
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))

            with self.assertRaisesRegex(ValueError, "runner contract bug"):
                run_method_attempts(
                    frozen,
                    DIRECT_LIFECYCLE,
                    runner=_ContractValueErrorRunner(),
                )

    def test_run_publication_value_error_remains_visible(self):
        """Only declared infrastructure failures are bounded after Runner return."""

        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            frozen = _merge(state, _freeze_contract(state))

            with patch(
                "app.execution.method_runner.publish_execution_logs",
                side_effect=ValueError("publication contract bug"),
            ):
                with self.assertRaisesRegex(ValueError, "publication contract bug"):
                    run_method_attempts(
                        frozen,
                        DIRECT_LIFECYCLE,
                        runner=_OutcomeRunner(set()),
                    )

    def test_large_stderr_is_bounded_in_state_but_retained_in_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            stderr_text = "failure detail\n" * 5_000
            frozen = _merge(state, _freeze_contract(state))

            update = run_method_attempts(
                frozen,
                DIRECT_LIFECYCLE,
                runner=_LargeStderrRunner(stderr_text),
            )
            failed = _merge(frozen, update)

            output = failed["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertLessEqual(len(output["reason"]), 2048)
            [execution] = load_method_executions(failed, DIRECT_LIFECYCLE)
            self.assertLessEqual(len(execution.evidence.error), 2048)
            _, stderr_path = execution_log_paths(failed, execution.evidence)
            self.assertEqual(stderr_path.read_text(encoding="utf-8"), stderr_text)

    def test_attempt_identity_mismatch_stops_collect_mode_before_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            lifecycle = MethodExecutionLifecycle(
                stage="assembly_review",
                name="contract batch",
                method_name="assembly_review.eval.quast",
                frozen_status="batch_frozen",
                ran_status="batch_ran",
                process_failure="collect",
            )
            frozen = _merge(
                state,
                freeze_method_attempts(
                    state,
                    lifecycle,
                    [
                        _attempt(state, attempt_id="s1:contract:0"),
                        _attempt(state, attempt_id="s1:contract:1"),
                    ],
                ),
            )
            runner = _MismatchedRunner()

            update = run_method_attempts(frozen, lifecycle, runner=runner)

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("attempt_id mismatch", output["reason"])
            self.assertEqual(
                runner.attempt_ids,
                [_bound_attempt_id(state, "s1:contract:0")],
            )
            self.assertEqual(
                [cursor["status"] for cursor in output["execution_attempts"]],
                ["frozen", "frozen"],
            )
            self.assertTrue(
                all(
                    cursor["execution_ref"] is None
                    for cursor in output["execution_attempts"]
                )
            )
            artifact_types = {
                record["artifact_type"]
                for record in read_artifact_records(
                    frozen["data_dir"], frozen["session_id"]
                )
            }
            self.assertNotIn("command.stdout", artifact_types)
            self.assertNotIn("command.stderr", artifact_types)

    def test_collect_mode_aborts_on_runner_boundary_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            lifecycle = MethodExecutionLifecycle(
                stage="assembly_review",
                name="contract batch",
                method_name="assembly_review.eval.quast",
                frozen_status="batch_frozen",
                ran_status="batch_ran",
                process_failure="collect",
            )
            frozen = _merge(
                state,
                freeze_method_attempts(
                    state,
                    lifecycle,
                    [
                        _attempt(state, attempt_id="s1:contract:0"),
                        _attempt(state, attempt_id="s1:contract:1"),
                    ],
                ),
            )
            runner = _RaisingRunner()

            update = run_method_attempts(frozen, lifecycle, runner=runner)

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertEqual(
                runner.attempt_ids,
                [_bound_attempt_id(state, "s1:contract:0")],
            )
            self.assertEqual(
                [cursor["status"] for cursor in output["execution_attempts"]],
                ["execution_failed", "frozen"],
            )

    def test_failed_ledger_event_error_does_not_escape_graph(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            graph = build_method_graph(
                DIRECT_LIFECYCLE,
                _freeze_contract,
                _collect_contract,
                runner=_OutcomeRunner({_bound_attempt_id(state, "s1:contract:0")}),
            )

            with patch(
                "app.execution.method_runner.Ledger.record_attempt_failed",
                side_effect=OSError("ledger is read-only"),
            ):
                result = graph.invoke(state)

            output = result["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertEqual(
                output["execution_attempts"][0]["status"],
                "execution_failed",
            )
            self.assertLessEqual(len(output["reason"]), 2048)

    def test_collect_mode_persistence_failure_keeps_prior_cursor_relations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            lifecycle = MethodExecutionLifecycle(
                stage="assembly_review",
                name="contract batch",
                method_name="assembly_review.eval.quast",
                frozen_status="batch_frozen",
                ran_status="batch_ran",
                process_failure="collect",
            )
            frozen = _merge(
                state,
                freeze_method_attempts(
                    state,
                    lifecycle,
                    [
                        _attempt(state, attempt_id="s1:contract:0"),
                        _attempt(state, attempt_id="s1:contract:1"),
                    ],
                ),
            )
            runner = _OutcomeRunner(set())
            after_first = _merge(
                frozen,
                run_method_attempts(frozen, lifecycle, runner=runner),
            )
            with patch(
                "app.execution.lifecycle.persist_execution_result",
                side_effect=OSError("simulated evidence write failure"),
            ):
                update = run_method_attempts(after_first, lifecycle, runner=runner)

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertEqual(
                runner.attempt_ids,
                [
                    _bound_attempt_id(state, "s1:contract:0"),
                    _bound_attempt_id(state, "s1:contract:1"),
                ],
            )
            self.assertEqual(
                [cursor["status"] for cursor in output["execution_attempts"]],
                ["executed", "frozen"],
            )
            self.assertIsNotNone(output["execution_attempts"][0]["execution_ref"])
            self.assertIsNone(output["execution_attempts"][1]["execution_ref"])

    def test_parameterized_run_updates_authoritative_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir, stage="assembly")
            lifecycle = MethodExecutionLifecycle(
                stage="assembly",
                name="parameter contract",
                method_name="assembly.contract",
                frozen_status="parameter_frozen",
                ran_status="parameter_ran",
                owner=ParameterAttemptOwner(),
            )
            attempt = _attempt(
                state,
                attempt_id="s1:assembly:parameter:0",
                stage="assembly",
                method_name="assembly.contract",
                parameter_attempt_id=0,
            )
            from app.execution.evidence import persist_frozen_attempt

            frozen_ref = persist_frozen_attempt(state, attempt)
            parameter_attempt = _v3_parameter_attempt(
                state,
                frozen_attempt_ref=frozen_ref,
            )
            state["stage_outputs"]["assembly"] = {
                "status": "parameter_frozen",
                "current_parameter_attempt_id": 0,
                "parameter_attempts": [parameter_attempt.model_dump(mode="json")],
            }

            update = run_method_attempts(
                state,
                lifecycle,
                runner=FakeRunner(),
            )

            output = update["stage_outputs"]["assembly"]
            self.assertEqual(output["status"], "parameter_ran")
            current = output["parameter_attempts"][0]
            self.assertEqual(current["status"], "executed")
            self.assertIsNotNone(current["execution_ref"])

    def test_parameterized_failure_persists_execution_before_failing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir, stage="assembly")
            lifecycle = MethodExecutionLifecycle(
                stage="assembly",
                name="parameter contract",
                method_name="assembly.contract",
                frozen_status="parameter_frozen",
                ran_status="parameter_ran",
                owner=ParameterAttemptOwner(),
            )
            attempt = _attempt(
                state,
                attempt_id="s1:assembly:parameter:0",
                stage="assembly",
                method_name="assembly.contract",
                parameter_attempt_id=0,
            )
            from app.execution.evidence import persist_frozen_attempt

            frozen_ref = persist_frozen_attempt(state, attempt)
            state["stage_outputs"]["assembly"] = {
                "status": "parameter_frozen",
                "current_parameter_attempt_id": 0,
                "parameter_attempts": [
                    _v3_parameter_attempt(
                        state,
                        frozen_attempt_ref=frozen_ref,
                    ).model_dump(mode="json")
                ],
            }

            update = run_method_attempts(
                state,
                lifecycle,
                runner=_OutcomeRunner({attempt.attempt_id}),
            )

            output = update["stage_outputs"]["assembly"]
            self.assertEqual(output["status"], "failed")
            current = output["parameter_attempts"][0]
            self.assertEqual(current["status"], "execution_failed")
            self.assertIsNotNone(current["execution_ref"])

    def test_parameter_owner_restores_ran_status_when_stage_is_rewound(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir, stage="assembly")
            lifecycle = MethodExecutionLifecycle(
                stage="assembly",
                name="parameter contract",
                method_name="assembly.contract",
                frozen_status="parameter_frozen",
                ran_status="parameter_ran",
                owner=ParameterAttemptOwner(),
            )
            attempt = _attempt(
                state,
                attempt_id="s1:assembly:parameter:0",
                stage="assembly",
                method_name="assembly.contract",
                parameter_attempt_id=0,
            )
            from app.execution.evidence import persist_frozen_attempt

            frozen_ref = persist_frozen_attempt(state, attempt)
            state["stage_outputs"]["assembly"] = {
                "status": lifecycle.frozen_status,
                "current_parameter_attempt_id": 0,
                "parameter_attempts": [
                    _v3_parameter_attempt(
                        state,
                        frozen_attempt_ref=frozen_ref,
                    ).model_dump(mode="json")
                ],
            }
            ran = _merge(
                state,
                run_method_attempts(state, lifecycle, runner=FakeRunner()),
            )
            ran["stage_outputs"]["assembly"]["status"] = lifecycle.frozen_status
            replay_runner = FakeRunner()

            update = run_method_attempts(ran, lifecycle, runner=replay_runner)

            output = update["stage_outputs"]["assembly"]
            self.assertEqual(output["status"], lifecycle.ran_status)
            self.assertEqual(output["parameter_attempts"][0]["status"], "executed")
            self.assertEqual(replay_runner.executed_commands, [])

    def test_parameter_owner_rejects_rewound_cursor_with_execution_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir, stage="assembly")
            lifecycle = MethodExecutionLifecycle(
                stage="assembly",
                name="parameter contract",
                method_name="assembly.contract",
                frozen_status="parameter_frozen",
                ran_status="parameter_ran",
                owner=ParameterAttemptOwner(),
            )
            attempt = _attempt(
                state,
                attempt_id="s1:assembly:parameter:0",
                stage="assembly",
                method_name="assembly.contract",
                parameter_attempt_id=0,
            )
            from app.execution.evidence import persist_frozen_attempt

            frozen_ref = persist_frozen_attempt(state, attempt)
            state["stage_outputs"]["assembly"] = {
                "status": lifecycle.frozen_status,
                "current_parameter_attempt_id": 0,
                "parameter_attempts": [
                    _v3_parameter_attempt(
                        state,
                        frozen_attempt_ref=frozen_ref,
                        execution_ref=_ref("c", "assembly.execution_result"),
                    ).model_dump(mode="json")
                ],
            }
            runner = FakeRunner()

            update = run_method_attempts(state, lifecycle, runner=runner)

            output = update["stage_outputs"]["assembly"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("completion evidence", output["reason"])
            self.assertEqual(runner.executed_commands, [])

    def test_parameter_owner_does_not_revalidate_history_before_starting_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir, stage="assembly")
            lifecycle = MethodExecutionLifecycle(
                stage="assembly",
                name="parameter contract",
                method_name="assembly.contract",
                frozen_status="parameter_frozen",
                ran_status="parameter_ran",
                owner=ParameterAttemptOwner(),
            )
            attempt = _attempt(
                state,
                attempt_id="s1:assembly:parameter:1",
                stage="assembly",
                method_name="assembly.contract",
                parameter_attempt_id=1,
            )
            from app.execution.evidence import persist_frozen_attempt

            frozen_ref = persist_frozen_attempt(state, attempt)
            current = _v3_parameter_attempt(
                state,
                frozen_attempt_ref=frozen_ref,
                retry=True,
            )
            state["stage_outputs"]["assembly"] = {
                "status": lifecycle.frozen_status,
                "current_parameter_attempt_id": 1,
                "parameter_attempts": [
                    {"attempt_id": 0},
                    current.model_dump(mode="json"),
                ],
            }
            runner = FakeRunner()

            update = run_method_attempts(state, lifecycle, runner=runner)

            output = update["stage_outputs"]["assembly"]
            self.assertEqual(output["status"], lifecycle.ran_status)
            self.assertEqual(len(runner.executed_commands), 1)
            self.assertEqual(output["parameter_attempts"][0], {"attempt_id": 0})
            self.assertEqual(output["parameter_attempts"][1]["status"], "executed")

    def test_parameter_owner_rejects_frozen_attempt_for_another_parameter_cursor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir, stage="assembly")
            lifecycle = MethodExecutionLifecycle(
                stage="assembly",
                name="parameter contract",
                method_name="assembly.contract",
                frozen_status="parameter_frozen",
                ran_status="parameter_ran",
                owner=ParameterAttemptOwner(),
            )
            attempt = _attempt(
                state,
                attempt_id="s1:assembly:parameter:0",
                stage="assembly",
                method_name="assembly.contract",
                parameter_attempt_id=1,
            )
            from app.execution.evidence import persist_frozen_attempt

            frozen_ref = persist_frozen_attempt(state, attempt)
            state["stage_outputs"]["assembly"] = {
                "status": lifecycle.frozen_status,
                "current_parameter_attempt_id": 0,
                "parameter_attempts": [
                    _v3_parameter_attempt(
                        state,
                        frozen_attempt_ref=frozen_ref,
                    ).model_dump(mode="json")
                ],
            }
            runner = FakeRunner()

            update = run_method_attempts(state, lifecycle, runner=runner)

            output = update["stage_outputs"]["assembly"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("does not match current cursor 0", output["reason"])
            self.assertEqual(runner.executed_commands, [])

    def test_stale_same_stage_attempt_fails_before_recovery_or_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(tmpdir)
            state = _merge(state, _freeze_contract(state))
            state["workflow_steps"].append(
                {
                    "step_id": "step-002-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": False,
                    "selected_method_names": ["assembly_review.eval.quast"],
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-001-assembly_review",
                    },
                }
            )
            state["workflow_step_index"] = 2
            state["entered_workflow_step_index"] = 2

            with (
                patch("app.execution.lifecycle.recover_execution_result") as recover,
                patch("app.execution.lifecycle.run_frozen_attempt") as run,
            ):
                update = run_method_attempts(
                    state,
                    DIRECT_LIFECYCLE,
                    runner=FakeRunner(),
                )

            output = update["stage_outputs"]["assembly_review"]
            self.assertEqual(output["status"], "failed")
            self.assertIn("does not match the active occurrence", output["reason"])
            recover.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
