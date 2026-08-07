"""Foreground countdown behavior for the assembly route-selection gate."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app.control.lifecycle import (
    ResumeValidationError,
    _blocked_timeout_status,
    build_foreground_auto_resume_request,
    inspect_state,
    normalize_resume_review_request,
    review_max_blocked_seconds,
)
from app.review.application import (
    ReviewApplicationIdentity,
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
)
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate
from app.review.persistence import load_review_request
from app.schemas.artifacts import ArtifactRef
from app.schemas.state import initial_state
from app.stages.assembly.nodes import block_assembly_for_human, select_route
from app.storage.ledger import Ledger
from tests.data_v2 import v2_run_dir

_RECOMMENDED_SELECTIONS = {
    "hifi": {
        "read_type": "pacbio_hifi",
        "primary_method": "assembly.candidate.hifiasm",
    },
    "ont": {
        "read_type": "ont_raw",
        "primary_method": "assembly.candidate.hifiasm_ont",
    },
    "cyclone": {
        "read_type": "cyclone",
        "primary_method": "assembly.candidate.flye",
    },
}


def _blocked_route_state(
    tmpdir: str,
    session_id: str,
    *,
    recommended_route: str = "hifi",
    recommended_selection: dict | None = None,
    wait_seconds: int = 300,
    elapsed_seconds: float = 400,
) -> dict:
    run_dir = v2_run_dir(tmpdir, session_id)
    Ledger(run_dir).create_session(session_id, "control/manifest.yaml")
    state = initial_state(
        session_id=session_id,
        data_dir=str(run_dir),
        input_manifest_ref="control/manifest.yaml",
        workflow_steps=[
            {
                "step_id": "step-000-inventory",
                "stage": "inventory",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-001-assembly",
                "stage": "assembly",
                "allow_local_execution": False,
            },
            {
                "step_id": "step-002-assembly_review",
                "stage": "assembly_review",
                "allow_local_execution": False,
                "review_target": {
                    "resource_type": "assembly",
                    "producer_step_id": "step-001-assembly",
                },
            },
        ],
    )
    state["current_stage"] = "assembly"
    state["workflow_step_index"] = 1
    state["entered_workflow_step_index"] = 1
    if recommended_selection is None and recommended_route in _RECOMMENDED_SELECTIONS:
        recommended_selection = {
            **_RECOMMENDED_SELECTIONS[recommended_route],
            "source": "recommendation",
            "rationale": f"{recommended_route} route recommended",
        }
    state["stage_outputs"] = {
        "assembly": {
            "status": "profile_ready",
            "review_policy": {"max_blocked_seconds": wait_seconds},
            "input_profile": {
                "recommended_route": recommended_route,
                "recommendation_reasoning": f"{recommended_route} reads available",
                "uncertainty_notes": [],
            },
            "recommended_assembly_selection": recommended_selection,
            "has_hifi": recommended_route == "hifi",
            "ont_reads_ref": (
                "artifacts/inputs/ont.fastq"
                if recommended_route in {"ont", "cyclone"}
                else None
            ),
        }
    }
    routed = {**state, **select_route(state)}
    blocked = {**routed, **block_assembly_for_human(routed)}
    blocked["active_interrupt"] = {
        **blocked["active_interrupt"],
        "blocked_at": (
            datetime.now(timezone.utc) - timedelta(seconds=elapsed_seconds)
        ).isoformat(),
    }
    return blocked


def _request_payload(state: dict) -> dict:
    interrupt = state["active_interrupt"]
    record = load_review_request(
        state["data_dir"],
        interrupt["request_ref"],
        session_id=state["session_id"],
    )
    return record["payload"]


def _human_request(state: dict, decision: str) -> ResumeReviewRequest:
    interrupt = state["active_interrupt"]
    return ResumeReviewRequest(
        session_id=state["session_id"],
        stage="assembly",
        gate_id="route_selection",
        decision=decision,
        review_request_id=interrupt["review_request_id"],
        review_request_hash=interrupt["request_hash"],
    )


def _auto_request(state: dict) -> ResumeReviewRequest:
    summary = inspect_state(state)
    wait_seconds = review_max_blocked_seconds(summary)
    return build_foreground_auto_resume_request(
        summary,
        configured_seconds=wait_seconds,
        elapsed_seconds=float(wait_seconds),
    )


def _apply_route_review(
    state: dict,
    request: ResumeReviewRequest,
    *,
    blocked_timeout_seconds: float | None = None,
    decision_source: str = "manual",
) -> dict:
    canonical = normalize_resume_review_request(
        state,
        request,
        blocked_timeout_seconds=blocked_timeout_seconds,
        decision_source=decision_source,
    )
    intent = persist_review_intent(
        state,
        canonical,
        source=decision_source,
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
        expected_identity=ReviewApplicationIdentity.from_intent(intent.intent),
    )
    return dict(gate.load_apply_application()(state, application))


def test_route_request_persists_effective_wait_without_policy_mode():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "persist_wait", wait_seconds=600)
        payload = _request_payload(state)

    assert payload["max_blocked_seconds"] == 600
    assert "review_policy" not in payload
    assert "review_policy_outcome" not in payload


@pytest.mark.parametrize(
    ("recommended_route", "expected_decision", "expected_method"),
    [
        ("hifi", "select_hifi", "assembly.candidate.hifiasm"),
        ("ont", "select_ont", "assembly.candidate.hifiasm_ont"),
        ("cyclone", "select_cyclone", "assembly.candidate.flye"),
    ],
)
def test_route_countdown_accepts_registered_recommendation(
    recommended_route: str,
    expected_decision: str,
    expected_method: str,
):
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            f"timeout_{recommended_route}",
            recommended_route=recommended_route,
        )
        request = _auto_request(state)

        assert request.decision == expected_decision
        assert request.method_name == expected_method
        result = _apply_route_review(
            state,
            request,
            blocked_timeout_seconds=300,
            decision_source="foreground_countdown",
        )

    assembly = result["stage_outputs"]["assembly"]
    assert assembly["selected_route"] == recommended_route
    assert assembly["route_decision"]["decided_by"] == "auto_rule"
    assert assembly["route_decision"]["metadata"]["auto_accepted"] is True
    assert result["supervision_state"] == "running_stage"


def test_manual_resume_after_deadline_uses_human_decision_without_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "manual_after_deadline")
        result = _apply_route_review(state, _human_request(state, "select_ont"))

    assembly = result["stage_outputs"]["assembly"]
    assert assembly["selected_route"] == "ont"
    assert assembly["route_decision"]["decided_by"] == "human_override"
    assert "blocked_timeout" not in assembly["route_decision"]["metadata"]


def test_auto_rule_request_without_foreground_timeout_is_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "auto_without_foreground")
        request = _auto_request(state)

        with pytest.raises(ResumeValidationError, match="foreground countdown"):
            _apply_route_review(
                state,
                request,
                decision_source="foreground_countdown",
            )


def test_auto_rule_request_requires_foreground_decision_source():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "auto_without_source")
        request = _auto_request(state)

        with pytest.raises(ResumeValidationError, match="decision source"):
            _apply_route_review(
                state,
                request,
                blocked_timeout_seconds=300,
            )


def test_auto_rule_request_before_foreground_deadline_is_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "auto_before_deadline",
            elapsed_seconds=0,
        )
        request = _auto_request(state)

        with pytest.raises(ResumeValidationError, match="has not expired"):
            _apply_route_review(
                state,
                request,
                blocked_timeout_seconds=300,
                decision_source="foreground_countdown",
            )


def test_auto_rule_timeout_must_match_verified_request():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "auto_timeout_mismatch")
        request = _auto_request(state)

        with pytest.raises(ResumeValidationError, match="does not match"):
            _apply_route_review(
                state,
                request,
                blocked_timeout_seconds=0,
                decision_source="foreground_countdown",
            )


def test_auto_rule_request_requires_verified_foreground_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "auto_metadata_missing")
        request = _auto_request(state).model_copy(update={"metadata": {}})

        with pytest.raises(ResumeValidationError, match="not bound"):
            _apply_route_review(
                state,
                request,
                blocked_timeout_seconds=300,
                decision_source="foreground_countdown",
            )


def test_indefinite_wait_rejects_auto_rule_even_with_internal_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "auto_indefinite",
            wait_seconds=-1,
        )
        interrupt = state["active_interrupt"]
        request = ResumeReviewRequest(
            session_id=state["session_id"],
            stage="assembly",
            gate_id="route_selection",
            decision="select_hifi",
            method_name="assembly.candidate.hifiasm",
            review_request_id=interrupt["review_request_id"],
            review_request_hash=interrupt["request_hash"],
            decided_by="auto_rule",
            rationale_artifact_ref=ArtifactRef.model_validate(interrupt["request_ref"]),
            metadata={
                "source": "foreground_countdown",
                "configured_seconds": -1,
                "elapsed_seconds": 0,
            },
        )

        with pytest.raises(ResumeValidationError, match="has not expired"):
            _apply_route_review(
                state,
                request,
                blocked_timeout_seconds=-1,
                decision_source="foreground_countdown",
            )


def test_expired_timeout_rejects_human_request():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "human_timeout")

        with pytest.raises(
            ResumeValidationError,
            match="foreground_countdown decision source",
        ):
            _apply_route_review(
                state,
                _human_request(state, "select_hifi"),
                blocked_timeout_seconds=300,
            )


def test_invalid_route_recommendation_leaves_gate_blocked():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "invalid_recommendation",
            recommended_selection={
                "read_type": "not-a-read-type",
                "primary_method": "assembly.candidate.hifiasm",
                "source": "recommendation",
                "rationale": "bad recommendation payload",
            },
        )
        request = _auto_request(state)

        with pytest.raises(ResumeValidationError, match="invalid read type"):
            _apply_route_review(
                state,
                request,
                blocked_timeout_seconds=300,
                decision_source="foreground_countdown",
            )

    assert state["supervision_state"] == "blocked_human"
    assert state["active_interrupt"] is not None


def test_indefinite_wait_never_expires():
    timeout = _blocked_timeout_status(
        {"blocked_at": "2026-05-08T10:00:00+00:00"},
        -1,
        now=datetime(2026, 5, 8, 20, 0, 0, tzinfo=timezone.utc),
    )

    assert timeout == {
        "configured_seconds": -1,
        "expired": False,
        "reason": "wait_indefinitely",
    }
