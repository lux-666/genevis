"""End-to-end persistence tests for the compact assembly parameter loop."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.control.errors import ResumeValidationError
from app.control.inspection import inspect_state
from app.control.resume import (
    build_foreground_auto_resume_request,
    normalize_resume_review_request,
)
from app.execution.parameter_attempts import current_parameter_attempt
from app.integrity import sha256_file
from app.llm.parameters.assembly.request import validate_recorded_parameter_request
from app.review.application import (
    VerifiedReviewApplication,
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
)
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ASSEMBLY_PARAMETER_COMMAND,
    ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.state import initial_state
from app.stages.assembly.method_application.hifiasm_ont import (
    freeze_hifiasm_ont_attempt,
)
from app.stages.assembly.nodes import plan_candidate_attempts
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.assembly.parameters.storage import (
    ParameterPersistenceError,
)
from app.stages.assembly.parameters.storage import (
    recover_artifact as recover_recommendation_json_artifact,
)
from app.stages.assembly.parameters.workflow import recommend_assembly_parameters
from app.storage.artifacts import read_artifact_records
from app.storage.parameter_evidence import (
    ApprovedParameterBundleV3,
    ParameterEvidenceError,
    load_approved_parameter_chain_v3,
    load_parameter_recommendation_chain_v3,
    publish_parameter_file,
    publish_parameter_json,
    read_parameter_json,
)
from app.storage.paths import SessionPaths, ensure_run_layout
from app.storage.publish import publish_file


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _Model:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages: list[dict[str, str]] | None = None

    def invoke(self, messages: list[dict[str, str]]) -> _Response:
        self.messages = messages
        return _Response(json.dumps(self.payload))


class _FailingModel:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir

    def invoke(self, _messages: list[dict[str, str]]) -> _Response:
        request_path = (
            self.run_dir
            / "artifacts"
            / "steps"
            / "step-003-assembly"
            / "assembly"
            / "parameter_recommendations"
            / "000"
            / "model_request.json"
        )
        assert request_path.is_file()
        raise RuntimeError("model unavailable")


def _apply_review_application(
    state: dict,
    request: ResumeReviewRequest,
    *,
    source: str = "test",
    blocked_timeout_seconds: float | None = None,
    now: datetime | None = None,
) -> tuple[dict, VerifiedReviewApplication]:
    """Exercise the durable intent/application boundary before pure projection."""

    canonical = normalize_resume_review_request(
        state,
        request,
        blocked_timeout_seconds=blocked_timeout_seconds,
        decision_source=source,
        now=now,
    )
    intent = persist_review_intent(
        state,
        canonical,
        source=source,
        blocked_timeout_seconds=blocked_timeout_seconds,
    )
    gate = get_review_gate(canonical.stage, canonical.gate_id)
    prepared = gate.load_prepare_application()(state, intent.intent)
    application_ref = persist_review_application(
        state,
        intent=intent,
        prepared=prepared,
    )
    application = load_verified_review_application(
        state["data_dir"],
        application_ref,
    )
    return dict(gate.load_apply_application()(state, application)), application


def _response(**overrides: int) -> dict:
    catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")
    assert set(overrides).issubset(
        {
            name
            for name, spec in catalog.parameters.items()
            if spec.layers.l2 is not None
        }
    )
    return {
        "parameters": dict(overrides),
        "summary": "Apply the recommended parameters.",
    }


def _state(
    tmp_path: Path,
    *,
    genome_size: int = 1_000_000_000,
    read_qc_summary: dict | None = None,
) -> dict:
    paths = SessionPaths(tmp_path, "s1")
    ensure_run_layout(paths)
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(paths.run_dir),
        workflow_steps=[
            {
                "selected_method_names": [],
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "selected_method_names": [],
                "step_id": "step-001-read_qc",
                "stage": "read_qc",
                "allow_local_execution": False,
            },
            {
                "selected_method_names": [],
                "step_id": "step-002-genome_survey",
                "stage": "genome_survey",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-003-assembly",
                "stage": "assembly",
                "allow_local_execution": True,
            },
            {
                "selected_method_names": [],
                "step_id": "step-004-assembly_review",
                "stage": "assembly_review",
                "allow_local_execution": False,
                "review_target": {
                    "resource_type": "assembly",
                    "producer_step_id": "step-003-assembly",
                },
            },
        ],
    )
    state["shared_parameters"] = {
        "organism": "Example species",
        "target_level": "draft",
    }
    read_qc_summary = read_qc_summary or {
        "n50": 30_000,
        "total_bases": 42_000_000_000,
    }
    genome_survey_result = {
        "estimates": {"genome_size": genome_size, "heterozygosity": 0.01},
        "estimate_sources": {"genome_size": "survey"},
    }
    state["current_stage"] = "read_qc"
    state["workflow_step_index"] = 1
    state["entered_workflow_step_index"] = 1
    read_qc_ref = publish_parameter_json(
        state,
        stage="read_qc",
        scope="recommendation",
        identifier=0,
        filename="summary.json",
        payload={"schema_version": 2, **read_qc_summary},
        artifact_type="read_qc.summary",
        method_name="read_qc.test_fixture",
    )
    state["current_stage"] = "genome_survey"
    state["workflow_step_index"] = 2
    state["entered_workflow_step_index"] = 2
    genome_survey_ref = publish_parameter_json(
        state,
        stage="genome_survey",
        scope="recommendation",
        identifier=0,
        filename="result.json",
        payload={"schema_version": 2, **genome_survey_result},
        artifact_type="genome_survey.result",
        method_name="genome_survey.test_fixture",
    )
    state["current_stage"] = "assembly"
    state["workflow_step_index"] = 3
    state["entered_workflow_step_index"] = 3
    state["stage_outputs"] = {
        "read_qc": {
            "summary": read_qc_summary,
            "summary_artifact_ref": read_qc_ref.model_dump(mode="json"),
        },
        "genome_survey": {
            **genome_survey_result,
            "result_artifact_ref": genome_survey_ref.model_dump(mode="json"),
        },
        "assembly": {
            "status": "selected",
            "execution_mode": "local",
            "primary_method": "assembly.candidate.hifiasm_ont",
            "selected_route": "ont",
            "assembly_route": "ont",
            "target_level": "draft",
            "input_ref": "reads.fastq",
            "input_type": "clean_long_reads",
            "input_profile": {
                "recommended_route": "ont",
                "has_ont": True,
                "has_hifi": False,
            },
            "assembly_selection": {
                "read_type": "ont_raw",
                "primary_method": "assembly.candidate.hifiasm_ont",
                "source": "recommendation",
                "rationale": "test",
                "metadata": {},
            },
            "parameter_recommendation_mode": "advisor",
        },
    }
    return state


def _blocked_state(state: dict, update: dict) -> dict:
    assembly = update["stage_outputs"]["assembly"]
    return {
        **state,
        "current_stage": "assembly",
        "supervision_state": "blocked_human",
        "active_interrupt": assembly["active_interrupt"],
        "stage_outputs": update["stage_outputs"],
    }


def _decision_request(state: dict, decision: str) -> ResumeReviewRequest:
    interrupt = state["active_interrupt"]
    return ResumeReviewRequest(
        session_id=state["session_id"],
        stage="assembly",
        gate_id="parameter_approval",
        review_request_id=interrupt["review_request_id"],
        review_request_hash=interrupt["request_hash"],
        decision=decision,
        metadata={
            key: interrupt.get(key)
            for key in (
                "attempt_id",
                "task",
                "previous_attempt_id",
                "recommendation_hash",
            )
        },
    )


def test_recommendation_persists_exact_request_and_keeps_checkpoint_compact(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    model = _Model(_response(purge_level=1, read_length_cutoff=3000))
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=model,
    ):
        update = recommend_assembly_parameters(state)

    assembly = update["stage_outputs"]["assembly"]
    assert assembly["status"] == "blocked_human"
    assert assembly.get("parameter_attempts") in (None, [])
    cursor = ParameterRecommendationCursor.model_validate(
        assembly["current_parameter_recommendation"]
    )
    chain = load_parameter_recommendation_chain_v3(
        {**state, "stage_outputs": update["stage_outputs"]},
        manifest_ref=cursor.manifest_ref,
        expected_stage="assembly",
        expected_method_id="assembly.candidate.hifiasm_ont",
        request_validator=validate_recorded_parameter_request,
    )
    model_request = chain.model_request
    assert model_request["messages"] == model.messages
    assert "request_id" not in model_request
    assert model_request["parameter_request_schema_version"] == 3
    assert "context_projection_schema_version" not in model_request
    user_message = model_request["messages"][1]["content"]
    assert user_message == json.dumps(
        json.loads(user_message),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for internal_field in (
        "prompt_profile",
        "evidence_registry",
        "baseline_overrides",
        "input_profile_hash",
        "payload_hash",
    ):
        assert internal_field not in user_message
    assert chain.recommendation["recommendation"]["parameters"] == {
        "purge_level": 1,
        "read_length_cutoff": 3000,
    }
    assert len(json.dumps(assembly["active_interrupt"], separators=(",", ":"))) < 4096
    assert "parameter_diff" not in assembly["active_interrupt"]
    assert "validation" not in json.dumps(cursor.model_dump(mode="json"))

    records = read_artifact_records(
        Path(state["data_dir"]),
        state["session_id"],
    )
    types = {record["artifact_type"] for record in records}
    assert {
        "assembly.parameter_recommendation.input_snapshot",
        "assembly.parameter.model_request",
        "assembly.parameter.model_response",
        "assembly.parameter.recommendation",
        "assembly.parameter.response_parse",
        "assembly.parameter.recommendation_manifest",
        "assembly.parameter_approval.request",
    }.issubset(types)
    assert all("s1" not in Path(record["path_ref"]).parts[1:] for record in records)


def test_fasta_quality_none_reaches_real_parameter_model_boundary(
    tmp_path: Path,
) -> None:
    state = _state(
        tmp_path,
        read_qc_summary={
            "n50": 30_000,
            "total_bases": 42_000_000_000,
            "q20_percent": None,
            "q30_percent": None,
            "avg_qual": None,
        },
    )
    model = _Model(_response(purge_level=1))
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=model,
    ):
        update = recommend_assembly_parameters(state)

    assert model.messages is not None
    assert update["stage_outputs"]["assembly"]["status"] == "blocked_human"


def test_approval_creates_compact_attempt_but_rejection_does_not(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        update = recommend_assembly_parameters(state)
    blocked = _blocked_state(state, update)

    approved, _application = _apply_review_application(
        blocked, _decision_request(blocked, "approve_parameters")
    )
    attempt = ParameterAttempt.model_validate(
        approved["stage_outputs"]["assembly"]["parameter_attempts"][0]
    )
    dumped = attempt.model_dump(mode="json")
    assert attempt.status == "recommended"
    assert len(json.dumps(dumped, separators=(",", ":"))) < 2048
    assert {
        "catalog_snapshot",
        "recommended_params",
        "validation",
        "argv",
        "execution_result",
    }.isdisjoint(dumped)

    second_state = _state(tmp_path / "reject")
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        second_update = recommend_assembly_parameters(second_state)
    second_blocked = _blocked_state(second_state, second_update)
    rejected, _application = _apply_review_application(
        second_blocked,
        _decision_request(second_blocked, "reject_parameters"),
    )
    assert rejected["supervision_state"] == "failed_terminal"
    assert rejected["workflow_step_outcome"]["outcome"] == "failed"
    assert (
        rejected["workflow_step_outcome"]["reason"]
        == "parameter_recommendation_rejected_by_human"
    )
    rejected_assembly = rejected["stage_outputs"]["assembly"]
    assert rejected_assembly.get("parameter_attempts") in (None, [])
    assert rejected_assembly.get("retry_counts") in (
        None,
        {},
        {"execution": 0, "review": 0},
    )


def test_foreground_countdown_auto_approves_bound_parameters_with_audit(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        update = recommend_assembly_parameters(state)
    blocked = _blocked_state(state, update)
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    blocked["active_interrupt"]["blocked_at"] = (
        now - timedelta(seconds=300)
    ).isoformat()
    summary = inspect_state(blocked)
    request = build_foreground_auto_resume_request(
        summary,
        configured_seconds=300,
        elapsed_seconds=300.0,
    )

    assert request.decision == "approve_parameters"
    assert request.decided_by == "auto_rule"
    assert request.metadata["attempt_id"] == blocked["active_interrupt"]["attempt_id"]
    assert (
        request.metadata["recommendation_hash"]
        == blocked["active_interrupt"]["recommendation_hash"]
    )

    approved, application = _apply_review_application(
        blocked,
        request,
        blocked_timeout_seconds=300,
        source="foreground_countdown",
        now=now,
    )
    attempt = ParameterAttempt.model_validate(
        approved["stage_outputs"]["assembly"]["parameter_attempts"][0]
    )
    decision_path = Path(blocked["data_dir"]) / attempt.approval_decision_ref.path_ref
    decision_record = json.loads(decision_path.read_text(encoding="utf-8"))
    decision = decision_record["decision"]

    assert (
        attempt.approval_decision_ref.artifact_type
        == ASSEMBLY_PARAMETER_APPROVAL_DECISION
    )
    assert decision["decided_by"] == "auto_rule"
    assert (
        decision["rationale_artifact_ref"] == blocked["active_interrupt"]["request_ref"]
    )
    assert decision["automatic_resolution"] == {
        "source": "foreground_countdown",
        "configured_seconds": 300,
        "elapsed_seconds": 300.0,
        "outcome": "approve_persisted_recommendation",
    }
    assert decision["bindings"] == {
        key: blocked["active_interrupt"][key]
        for key in (
            "attempt_id",
            "task",
            "previous_attempt_id",
            "recommendation_hash",
        )
    }

    assert application.application.effect_kind == "assembly.parameter_approval.v1"
    assert application.application.decision_ref == attempt.approval_decision_ref


def test_parameter_countdown_reject_decision_fails_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        update = recommend_assembly_parameters(state)
    blocked = _blocked_state(state, update)
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    blocked["active_interrupt"]["blocked_at"] = (
        now - timedelta(seconds=300)
    ).isoformat()
    request = build_foreground_auto_resume_request(
        inspect_state(blocked),
        configured_seconds=300,
        elapsed_seconds=300.0,
    ).model_copy(update={"decision": "reject_parameters"})

    with pytest.raises(
        ResumeValidationError,
        match="automatic parameter approval can only approve",
    ):
        _apply_review_application(
            blocked,
            request,
            blocked_timeout_seconds=300,
            source="foreground_countdown",
            now=now,
        )

    assert blocked["supervision_state"] == "blocked_human"
    assert blocked["active_interrupt"] is not None


def test_parameter_chains_reject_an_earlier_same_stage_occurrence(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        update = recommend_assembly_parameters(state)
    blocked = _blocked_state(state, update)
    approval_update, _application = _apply_review_application(
        blocked,
        _decision_request(blocked, "approve_parameters"),
    )
    first_cursor = ParameterRecommendationCursor.model_validate(
        update["stage_outputs"]["assembly"]["current_parameter_recommendation"]
    )
    first_attempt = ParameterAttempt.model_validate(
        approval_update["stage_outputs"]["assembly"]["parameter_attempts"][0]
    )

    second_occurrence = {
        **state,
        "workflow_steps": [
            *state["workflow_steps"],
            {
                "step_id": "step-005-assembly",
                "stage": "assembly",
                "allow_local_execution": True,
            },
            {
                "selected_method_names": [],
                "step_id": "step-006-assembly_review",
                "stage": "assembly_review",
                "allow_local_execution": False,
                "review_target": {
                    "resource_type": "assembly",
                    "producer_step_id": "step-005-assembly",
                },
            },
        ],
        "current_stage": "assembly",
        "workflow_step_index": 5,
        "entered_workflow_step_index": 5,
    }

    with pytest.raises(
        ParameterEvidenceError,
        match="recommendation manifest targets another workflow occurrence",
    ):
        load_parameter_recommendation_chain_v3(
            second_occurrence,
            manifest_ref=first_cursor.manifest_ref,
            expected_stage="assembly",
            expected_method_id="assembly.candidate.hifiasm_ont",
            request_validator=validate_recorded_parameter_request,
        )
    with pytest.raises(
        ParameterEvidenceError,
        match="recommendation manifest targets another workflow occurrence",
    ):
        load_approved_parameter_chain_v3(
            second_occurrence,
            attempt=first_attempt,
            expected_stage="assembly",
            expected_method_id="assembly.candidate.hifiasm_ont",
            request_validator=validate_recorded_parameter_request,
        )


def test_parameter_publication_rejects_a_source_from_another_occurrence(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    paths = SessionPaths.from_run_dir(state["data_dir"])
    source = paths.workflow_step_work_path(
        "step-001-read_qc",
        "read_qc",
        "parameter_persistence",
        "stale.json",
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"schema_version": 2}\n', encoding="utf-8")

    with pytest.raises(
        ParameterEvidenceError,
        match="parameter evidence source targets another workflow occurrence",
    ):
        publish_parameter_file(
            state,
            stage="assembly",
            scope="recommendation",
            identifier=0,
            filename="stale.json",
            source=source,
            artifact_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
            method_name="assembly.parameter_test",
        )


def test_freeze_uses_approved_snapshot_after_mutable_genome_state_changes(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path, genome_size=1_000_000)
    reads = tmp_path / "reads.fastq"
    reads.write_text("@read\nACGT\n+\nIIII\n", encoding="utf-8")
    assembly = state["stage_outputs"]["assembly"]
    assembly.update(
        {
            "input_ref": str(reads.resolve()),
            "hifiasm_ont_parameters": {"threads": 4},
            "assembly_input_member_ids": {"primary": ["fixture_member_0"]},
            "assembly_input_member_platforms": {"primary": ["ont"]},
        }
    )

    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_Model(_response()),
    ):
        recommendation_update = recommend_assembly_parameters(state)
    blocked = _blocked_state(state, recommendation_update)
    approval_update, _application = _apply_review_application(
        blocked,
        _decision_request(blocked, "approve_parameters"),
    )
    approved_state = {
        **blocked,
        **approval_update,
        "stage_outputs": approval_update["stage_outputs"],
    }

    approved_state["stage_outputs"]["genome_survey"]["estimates"]["genome_size"] = (
        2_000_000
    )
    plan_update = plan_candidate_attempts(approved_state)
    planned_state = {
        **approved_state,
        **plan_update,
        "stage_outputs": plan_update["stage_outputs"],
    }
    with (
        patch(
            "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
            return_value=(None, [], None),
        ),
        patch(
            "app.stages.assembly.method_application.hifiasm_ont.build_read_input_dependencies",
            return_value=[],
        ),
        patch(
            "app.stages.assembly.method_application.hifiasm_ont.resolve_long_read_members",
            return_value=[SimpleNamespace(path=str(reads), format="fastq")],
        ),
    ):
        freeze_update = freeze_hifiasm_ont_attempt(planned_state)
    frozen_state = {
        **planned_state,
        **freeze_update,
        "stage_outputs": freeze_update["stage_outputs"],
    }
    frozen_output = frozen_state["stage_outputs"]["assembly"]
    assert frozen_output["status"] == "hifiasm_ont_frozen"
    attempt = ParameterAttempt.model_validate(frozen_output["parameter_attempts"][0])

    frozen_parameters = read_parameter_json(
        frozen_state,
        attempt.frozen_parameters_ref,
        expected_type=ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    )
    command = read_parameter_json(
        frozen_state,
        attempt.command_ref,
        expected_type=ASSEMBLY_PARAMETER_COMMAND,
    )
    assert frozen_parameters["l1_facts"]["hg_size"] == "1m"
    assert frozen_parameters["effective_parameters"]["hg_size"] == "1m"
    hg_size_index = command["argv"].index("--hg-size")
    assert command["argv"][hg_size_index + 1] == "1m"


def test_model_request_is_durable_before_invoke_failure(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_FailingModel(Path(state["data_dir"])),
    ):
        update = recommend_assembly_parameters(state)

    assembly = update["stage_outputs"]["assembly"]
    assert assembly["status"] == "parameter_response_invalid"
    cursor = ParameterRecommendationCursor.model_validate(
        assembly["current_parameter_recommendation"]
    )
    chain = load_parameter_recommendation_chain_v3(
        {**state, "stage_outputs": update["stage_outputs"]},
        manifest_ref=cursor.manifest_ref,
        expected_stage="assembly",
        expected_method_id="assembly.candidate.hifiasm_ont",
        request_validator=validate_recorded_parameter_request,
    )
    assert chain.model_response["validity"] == "invalid"
    assert "model unavailable" in chain.model_response["error"]


def test_deterministic_baseline_persists_policy_decision_without_model_call(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    assembly = state["stage_outputs"]["assembly"]
    assembly["parameter_recommendation_mode"] = "deterministic_baseline"
    state["stage_seeds"] = {"assembly": {"parameter_advisor": "deterministic_baseline"}}

    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        side_effect=AssertionError("baseline must not call the model"),
    ):
        update = recommend_assembly_parameters(state)

    result = update["stage_outputs"]["assembly"]
    assert result["status"] == "parameters_approved"
    assert result["parameter_recommendation_mode"] == "deterministic_baseline"
    cursor = ParameterRecommendationCursor.model_validate(
        result["current_parameter_recommendation"]
    )
    assert cursor.status == "approved"
    assert cursor.review_request_id is not None

    attempt = current_parameter_attempt(
        result,
        method_id="assembly.candidate.hifiasm_ont",
        label="Hifiasm ONT",
    )
    assert attempt.approval_decision_ref is not None

    approved_bundle = load_approved_parameter_chain_v3(
        {**state, **update},
        attempt=attempt,
        expected_stage="assembly",
        expected_method_id="assembly.candidate.hifiasm_ont",
        request_validator=validate_recorded_parameter_request,
    )
    assert isinstance(approved_bundle, ApprovedParameterBundleV3)
    assert approved_bundle.chain.manifest.summary["policy"] == "deterministic_baseline"
    assert approved_bundle.chain.manifest.summary["policy_skipped_model"] is True
    assert approved_bundle.approved_l2 == {}


def test_baseline_freeze_uses_empty_l2_without_policy_branch(tmp_path: Path) -> None:
    state = _state(tmp_path, genome_size=1_000_000)
    reads = tmp_path / "reads.fastq"
    reads.write_text("@read\nACGT\n+\nIIII\n", encoding="utf-8")
    assembly = state["stage_outputs"]["assembly"]
    assembly.update(
        {
            "parameter_recommendation_mode": "deterministic_baseline",
            "input_ref": str(reads.resolve()),
            "hifiasm_ont_parameters": {"threads": 4},
            "assembly_input_member_ids": {"primary": ["fixture_member_0"]},
            "assembly_input_member_platforms": {"primary": ["ont"]},
        }
    )
    state["stage_seeds"] = {
        **(state.get("stage_seeds") or {}),
        "assembly": {"parameter_advisor": "deterministic_baseline"},
    }

    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        side_effect=AssertionError("baseline must not call the model"),
    ):
        recommendation_update = recommend_assembly_parameters(state)
    approved_state = {
        **state,
        **recommendation_update,
        "stage_outputs": recommendation_update["stage_outputs"],
    }

    plan_update = plan_candidate_attempts(approved_state)
    planned_state = {
        **approved_state,
        **plan_update,
        "stage_outputs": plan_update["stage_outputs"],
    }
    with (
        patch(
            "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
            return_value=(None, [], None),
        ),
        patch(
            "app.stages.assembly.method_application.hifiasm_ont.build_read_input_dependencies",
            return_value=[],
        ),
        patch(
            "app.stages.assembly.method_application.hifiasm_ont.resolve_long_read_members",
            return_value=[SimpleNamespace(path=str(reads), format="fastq")],
        ),
    ):
        freeze_update = freeze_hifiasm_ont_attempt(planned_state)
    frozen_state = {
        **planned_state,
        **freeze_update,
        "stage_outputs": freeze_update["stage_outputs"],
    }
    frozen_output = frozen_state["stage_outputs"]["assembly"]
    attempt = ParameterAttempt.model_validate(frozen_output["parameter_attempts"][0])
    frozen_parameters = read_parameter_json(
        frozen_state,
        attempt.frozen_parameters_ref,
        expected_type=ASSEMBLY_PARAMETER_FROZEN_PARAMETERS,
    )
    command = read_parameter_json(
        frozen_state,
        attempt.command_ref,
        expected_type=ASSEMBLY_PARAMETER_COMMAND,
    )

    assert frozen_output["status"] == "hifiasm_ont_frozen"
    assert frozen_parameters["l2_approved"] == {}
    assert "l2" not in frozen_parameters["effective_source_by_parameter"].values()
    assert "--hg-size" not in command["argv"] or (
        command["argv"][command["argv"].index("--hg-size") + 1] == "1m"
    )


def test_approved_baseline_rejects_nonempty_l2_evidence(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assembly = state["stage_outputs"]["assembly"]
    assembly["parameter_recommendation_mode"] = "deterministic_baseline"
    state["stage_seeds"] = {"assembly": {"parameter_advisor": "deterministic_baseline"}}

    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        side_effect=AssertionError("baseline must not call the model"),
    ):
        update = recommend_assembly_parameters(state)
    merged_state = {**state, **update}
    result = merged_state["stage_outputs"]["assembly"]
    attempt = current_parameter_attempt(
        result,
        method_id="assembly.candidate.hifiasm_ont",
        label="Hifiasm ONT",
    )
    bundle = load_approved_parameter_chain_v3(
        merged_state,
        attempt=attempt,
        expected_stage="assembly",
        expected_method_id="assembly.candidate.hifiasm_ont",
        request_validator=validate_recorded_parameter_request,
    )

    tampered_chain = replace(
        bundle.chain,
        recommendation={"recommendation": {"parameters": {"purge_level": 1}}},
        manifest=bundle.chain.manifest.model_copy(
            update={
                "summary": {
                    **bundle.chain.manifest.summary,
                    "policy": "deterministic_baseline",
                }
            }
        ),
    )
    tampered_bundle = ApprovedParameterBundleV3(
        attempt=attempt,
        chain=tampered_chain,
        approval_decision=bundle.approval_decision,
    )
    with pytest.raises(ParameterEvidenceError, match="deterministic baseline"):
        _ = tampered_bundle.approved_l2


def test_stale_checkpoint_reuses_persisted_model_response(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        first = recommend_assembly_parameters(state)

    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        side_effect=AssertionError("the model must not be called during recovery"),
    ):
        replay = recommend_assembly_parameters(state)

    first_cursor = ParameterRecommendationCursor.model_validate(
        first["stage_outputs"]["assembly"]["current_parameter_recommendation"]
    )
    replay_cursor = ParameterRecommendationCursor.model_validate(
        replay["stage_outputs"]["assembly"]["current_parameter_recommendation"]
    )
    assert replay["stage_outputs"]["assembly"]["status"] == "blocked_human"
    assert replay_cursor.manifest_ref == first_cursor.manifest_ref
    assert replay_cursor.review_request_ref == first_cursor.review_request_ref
    assert replay_cursor.review_request_id == first_cursor.review_request_id


def test_recovery_repairs_file_published_before_index_append(tmp_path: Path) -> None:
    state = _state(tmp_path)
    paths = SessionPaths.from_run_dir(state["data_dir"])
    source = paths.workflow_step_work_path(
        "step-003-assembly",
        "assembly",
        "parameter_persistence",
        "response.tmp",
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "raw_response": json.dumps(_response(purge_level=1)),
                "validity": "parsed",
                "error": None,
                "model_id": "test",
            }
        ),
        encoding="utf-8",
    )
    destination = paths.workflow_step_artifact_path(
        "step-003-assembly",
        "assembly",
        "parameter_recommendations",
        "000",
        "model_response.json",
    )
    publish_file(source, destination)
    assert not any(
        record["path_ref"] == paths.to_ref(destination)
        for record in read_artifact_records(paths.run_dir, "s1")
    )

    recovered = recover_recommendation_json_artifact(
        state,
        0,
        "model_response.json",
        expected_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
        method_name="assembly.parameter_recommender",
    )

    assert recovered is not None
    recovered_ref, payload = recovered
    assert recovered_ref.path_ref == paths.to_ref(destination)
    assert payload["validity"] == "parsed"
    assert (
        sum(
            record["path_ref"] == recovered_ref.path_ref
            for record in read_artifact_records(paths.run_dir, "s1")
        )
        == 1
    )


def test_parameter_read_requires_exact_index_membership(tmp_path: Path) -> None:
    state = _state(tmp_path)
    ref = publish_parameter_json(
        state,
        stage="assembly",
        scope="recommendation",
        identifier=0,
        filename="indexed.json",
        payload={"schema_version": 2, "value": "indexed"},
        artifact_type="assembly.parameter.test",
        method_name="assembly.parameter.test",
    )
    forged = ref.model_copy(update={"artifact_id": "art_" + "0" * 32})

    with pytest.raises(ParameterEvidenceError, match="unreadable"):
        read_parameter_json(
            state,
            forged,
            expected_type="assembly.parameter.test",
        )

    paths = SessionPaths.from_run_dir(state["data_dir"])
    unindexed_path = paths.workflow_step_artifact_path(
        "step-003-assembly",
        "assembly",
        "parameter_recommendations",
        "000",
        "unindexed.json",
    )
    unindexed_path.parent.mkdir(parents=True, exist_ok=True)
    unindexed_path.write_text('{"schema_version": 2}\n', encoding="utf-8")
    unindexed_ref = ArtifactRef(
        artifact_id="art_" + "f" * 32,
        path_ref=paths.to_ref(unindexed_path),
        sha256=sha256_file(unindexed_path),
        size_bytes=unindexed_path.stat().st_size,
        artifact_type="assembly.parameter.test",
    )

    with pytest.raises(ParameterEvidenceError, match="unreadable"):
        read_parameter_json(
            state,
            unindexed_ref,
            expected_type="assembly.parameter.test",
        )


def test_recovery_fails_closed_when_indexed_file_disappears(tmp_path: Path) -> None:
    state = _state(tmp_path)
    ref = publish_parameter_json(
        state,
        stage="assembly",
        scope="recommendation",
        identifier=0,
        filename="model_response.json",
        payload={"schema_version": 2, "validity": "parsed"},
        artifact_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
        method_name="assembly.parameter_recommender",
    )
    (Path(state["data_dir"]) / ref.path_ref).unlink()

    with pytest.raises(ParameterPersistenceError, match="recovery failed"):
        recover_recommendation_json_artifact(
            state,
            0,
            "model_response.json",
            expected_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
            method_name="assembly.parameter_recommender",
        )


def test_parameter_file_publication_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    source = SessionPaths.from_run_dir(state["data_dir"]).workflow_step_work_path(
        "step-003-assembly",
        "assembly",
        "command.sh",
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"set -e\necho exact\n")

    first = publish_parameter_file(
        state,
        stage="assembly",
        scope="attempt",
        identifier=0,
        filename="command.sh",
        source=source,
        artifact_type="assembly.parameter.script",
        method_name="assembly.candidate.test",
    )
    self_path = Path(state["data_dir"]) / first.path_ref
    assert self_path.read_bytes() == b"set -e\necho exact\n"
    assert not source.exists()

    source.write_bytes(b"set -e\necho exact\n")
    replay = publish_parameter_file(
        state,
        stage="assembly",
        scope="attempt",
        identifier=0,
        filename="command.sh",
        source=source,
        artifact_type="assembly.parameter.script",
        method_name="assembly.candidate.test",
    )
    assert replay == first
    assert not source.exists()
    matching_records = [
        record
        for record in read_artifact_records(
            Path(state["data_dir"]), state["session_id"]
        )
        if record["path_ref"] == first.path_ref
    ]
    assert len(matching_records) == 1

    source.write_bytes(b"set -e\necho changed\n")
    with pytest.raises(FileExistsError, match="conflicts with existing snapshot"):
        publish_parameter_file(
            state,
            stage="assembly",
            scope="attempt",
            identifier=0,
            filename="command.sh",
            source=source,
            artifact_type="assembly.parameter.script",
            method_name="assembly.candidate.test",
        )
    assert source.read_bytes() == b"set -e\necho changed\n"
