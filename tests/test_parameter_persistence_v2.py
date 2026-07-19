"""End-to-end persistence tests for the compact assembly parameter loop."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.control.lifecycle import resume_review_state
from app.storage.artifacts import read_artifact_records
from app.review.base import ResumeReviewRequest
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.integrity import sha256_file
from app.schemas.state import initial_state
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.assembly.parameters.storage import (
    ParameterPersistenceError,
    load_recommendation_bundle,
    recover_recommendation_json_artifact,
)
from app.stages.assembly.parameters.workflow import recommend_assembly_parameters
from app.storage.parameter_evidence import (
    ParameterEvidenceError,
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
        "parameters": dict(overrides),
        "summary": "Apply the recommended parameters.",
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
    bundle = load_recommendation_bundle(
        {**state, "stage_outputs": update["stage_outputs"]},
        cursor.manifest_ref,
    )
    model_request = bundle["model_request"]
    assert model_request["messages"] == model.messages
    assert "request_id" not in model_request
    assert model_request["parameter_request_schema_version"] == 2
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
    assert bundle["recommendation"]["recommendation"]["parameters"] == {
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
        "assembly.parameter.response_parse",
        "assembly.parameter.recommendation_manifest",
        "assembly.parameter_approval.request",
    }.issubset(types)
    assert all("s1" not in Path(record["path_ref"]).parts[1:] for record in records)


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

    approved = resume_review_state(
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
    rejected = resume_review_state(
        second_blocked,
        _decision_request(second_blocked, "reject_parameters"),
    )
    rejected_assembly = rejected["stage_outputs"]["assembly"]
    assert rejected_assembly.get("parameter_attempts") in (None, [])
    assert rejected_assembly.get("retry_counts") in (
        None,
        {},
        {"execution": 0, "review": 0},
    )


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
    bundle = load_recommendation_bundle(
        {**state, "stage_outputs": update["stage_outputs"]},
        cursor.manifest_ref,
    )
    assert bundle["model_response"]["validity"] == "invalid"
    assert "model unavailable" in bundle["model_response"]["error"]


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
    source = paths.work_path("assembly", "parameter_persistence", "response.tmp")
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
    destination = paths.artifact_path(
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
    unindexed_path = paths.artifact_path(
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
    source = Path(state["data_dir"]) / "work" / "assembly" / "command.sh"
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
