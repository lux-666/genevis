"""End-to-end persistence tests for the compact assembly parameter loop."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from app.control.lifecycle import resume_review_state
from app.execution.artifacts import read_artifact_records
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import ParameterAttempt, ParameterRecommendationCursor
from app.schemas.state import initial_state
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.parameter_nodes import recommend_assembly_parameters
from app.stages.assembly.parameter_persistence import load_recommendation_bundle
from app.storage.paths import SessionPaths, ensure_run_layout


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
            / "assembly"
            / "parameter_recommendations"
            / "000"
            / "model_request.json"
        )
        assert request_path.is_file()
        raise RuntimeError("model unavailable")


def _response(**overrides: int) -> dict:
    catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")
    assert set(overrides).issubset(
        {name for name, spec in catalog.parameters.items() if spec.level == 2}
    )
    return {
        "sample_diagnosis": {
            "effect_areas": ["assembly_graph"],
            "summary": "Use a bounded adjustment for this sample.",
        },
        "parameter_patch": {
            parameter: {
                "value": value,
                "observation_ids": [],
                "guidance_ids": [],
                "literature_anchors": [],
                "expected_effect": "Improve assembly behavior.",
                "tradeoffs": ["Requires review."],
                "confidence": "medium",
                "reason": "Bounded test adjustment.",
            }
            for parameter, value in overrides.items()
        },
        "summary": "Apply the bounded sparse parameter patch.",
    }


def _state(tmp_path: Path) -> dict:
    paths = SessionPaths(tmp_path, "s1")
    ensure_run_layout(paths)
    state = initial_state(
        session_id="s1",
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(paths.run_dir),
    )
    state["shared_parameters"] = {
        "organism": "Example species",
        "target_level": "draft",
    }
    state["stage_outputs"] = {
        "read_qc": {"summary": {"n50": 30_000, "effective_coverage_5kb": 42}},
        "genome_survey": {
            "estimates": {"genome_size": 1_000_000_000, "heterozygosity": "medium"}
        },
        "assembly": {
            "status": "selected",
            "execution_mode": "local",
            "primary_method": "assembly.candidate.hifiasm_ont",
            "selected_route": "ont",
            "assembly_route": "ont",
            "input_ref": "reads.fastq",
            "input_type": "clean_long_reads",
            "input_profile": {
                "recommended_route": "ont",
                "has_ont": True,
                "has_hifi": False,
            },
            "assembly_selection": {
                "read_type": "ont",
                "primary_method": "assembly.candidate.hifiasm_ont",
                "source": "recommendation",
                "rationale": "test",
                "metadata": {},
            },
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
    with patch("app.stages.assembly.decision.build_chat_model", return_value=model):
        update = recommend_assembly_parameters(state)

    assembly = update["stage_outputs"]["assembly"]
    assert assembly["status"] == "blocked_human"
    assert assembly.get("parameter_attempts") in (None, [])
    cursor = ParameterRecommendationCursor.model_validate(
        assembly["current_parameter_recommendation"]
    )
    bundle = load_recommendation_bundle(
        {**state, "stage_outputs": update["stage_outputs"]},
        cursor.manifest_ref,
    )
    model_request = bundle["model_request"]
    assert model_request["messages"] == model.messages
    assert model_request["parameter_request_schema_version"] == 1
    assert "context_projection_schema_version" not in model_request
    user_message = model_request["messages"][1]["content"]
    for internal_field in (
        "prompt_profile",
        "evidence_registry",
        "baseline_overrides",
        "input_profile_hash",
        "payload_hash",
    ):
        assert internal_field not in user_message
    assert bundle["recommendation"]["recommendation"]["params"] == {
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
        "assembly.parameter.input_profile",
        "assembly.parameter.model_request",
        "assembly.parameter.model_response",
        "assembly.parameter.recommendation",
        "assembly.parameter.validation",
        "assembly.parameter.recommendation_manifest",
        "assembly.parameter_approval.request",
    }.issubset(types)
    assert all("s1" not in Path(record["path_ref"]).parts[1:] for record in records)


def test_approval_creates_compact_attempt_but_rejection_does_not(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    with patch(
        "app.stages.assembly.decision.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        update = recommend_assembly_parameters(state)
    blocked = _blocked_state(state, update)

    approved = resume_review_state(blocked, _decision_request(blocked, "approve_parameters"))
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
        "app.stages.assembly.decision.build_chat_model",
        return_value=_Model(_response(purge_level=1)),
    ):
        second_update = recommend_assembly_parameters(second_state)
    second_blocked = _blocked_state(second_state, second_update)
    rejected = resume_review_state(
        second_blocked,
        _decision_request(second_blocked, "reject_parameters"),
    )
    rejected_assembly = rejected["stage_outputs"]["assembly"]
    assert rejected_assembly.get("parameter_attempts") in (None, [])
    assert rejected_assembly.get("retry_counts") in (None, {}, {"execution": 0, "review": 0})


def test_model_request_is_durable_before_invoke_failure(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with patch(
        "app.stages.assembly.decision.build_chat_model",
        return_value=_FailingModel(Path(state["data_dir"])),
    ):
        update = recommend_assembly_parameters(state)

    assembly = update["stage_outputs"]["assembly"]
    assert assembly["status"] == "parameter_validation_failed"
    cursor = ParameterRecommendationCursor.model_validate(
        assembly["current_parameter_recommendation"]
    )
    bundle = load_recommendation_bundle(
        {**state, "stage_outputs": update["stage_outputs"]},
        cursor.manifest_ref,
    )
    assert bundle["model_response"]["validity"] == "invalid"
    assert "model unavailable" in bundle["model_response"]["error"]
