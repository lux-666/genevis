"""Tests for route-selection timeout behavior on v2 review evidence."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import app.control.lifecycle as lifecycle
from app.storage.ledger import Ledger
from app.control.lifecycle import ResumeValidationError, resume_review_state, timeout_terminal_update
from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request
from app.schemas.state import initial_state
from app.stages.assembly.nodes import block_assembly_for_human, select_route
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
    policy_seconds: float | None = 300,
    elapsed_seconds: float = 400,
) -> dict:
    """Create the gate through the production route selector and persistence path."""

    run_dir = v2_run_dir(tmpdir, session_id)
    Ledger(run_dir).create_session(session_id, "control/manifest.yaml")
    state = initial_state(
        session_id=session_id,
        data_dir=str(run_dir),
        input_manifest_ref="control/manifest.yaml",
    )
    state["current_stage"] = "assembly"

    if recommended_selection is None and recommended_route in _RECOMMENDED_SELECTIONS:
        recommended_selection = {
            **_RECOMMENDED_SELECTIONS[recommended_route],
            "source": "recommendation",
            "rationale": f"{recommended_route} route recommended",
        }

    policy = {"mode": "strict"}
    if policy_seconds is not None:
        policy["max_blocked_seconds"] = policy_seconds
    assembly = {
        "status": "profile_ready",
        "review_policy": policy,
        "input_profile": {
            "recommended_route": recommended_route,
            "recommendation_reasoning": f"{recommended_route} reads available",
            "uncertainty_notes": (
                ["No usable reads available"]
                if recommended_route == "insufficient_data"
                else []
            ),
        },
        "recommended_assembly_selection": recommended_selection,
        "has_hifi": recommended_route == "hifi",
        "ont_reads_ref": (
            "artifacts/inputs/ont.fastq"
            if recommended_route in {"ont", "cyclone"}
            else None
        ),
    }
    state["stage_outputs"] = {"assembly": assembly}

    routed = {**state, **select_route(state)}
    blocked = {**routed, **block_assembly_for_human(routed)}
    blocked["active_interrupt"] = {
        **blocked["active_interrupt"],
        "blocked_at": (
            datetime.now(timezone.utc) - timedelta(seconds=elapsed_seconds)
        ).isoformat(),
    }
    return blocked


def _request(
    state: dict,
    decision: str,
    *,
    session_id: str | None = None,
    stage: str = "assembly",
    gate_id: str = "route_selection",
) -> ResumeReviewRequest:
    interrupt = state["active_interrupt"]
    return ResumeReviewRequest(
        session_id=session_id or state["session_id"],
        stage=stage,
        gate_id=gate_id,
        decision=decision,
        review_request_id=interrupt["review_request_id"],
        review_request_hash=interrupt["request_hash"],
    )


def _request_payload(state: dict) -> dict:
    interrupt = state["active_interrupt"]
    record = load_review_request(
        state["data_dir"],
        interrupt["request_ref"],
        session_id=state["session_id"],
    )
    return record["payload"]


def test_route_selection_timeout_auto_accepts_recommendation():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "timeout_test")
        payload = _request_payload(state)
        assert payload["recommended_route"] == "hifi"
        assert payload["recommended_assembly_selection"]["read_type"] == "pacbio_hifi"

        result = resume_review_state(
            state,
            _request(state, "select_hifi"),
            blocked_timeout_seconds=300,
        )

        assembly = result["stage_outputs"]["assembly"]
        assert assembly["status"] == "route_selected"
        assert assembly["selected_route"] == "hifi"
        assert result["supervision_state"] == "running_stage"
        assert assembly["route_decision"]["decided_by"] == "llm_auto"
        assert assembly["route_decision"]["metadata"]["auto_accepted"] is True


def test_route_selection_timeout_with_insufficient_data_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "timeout_test_fail",
            recommended_route="insufficient_data",
        )
        assert _request_payload(state)["recommended_assembly_selection"] == {}

        result = resume_review_state(
            state,
            _request(state, "select_hifi"),
            blocked_timeout_seconds=300,
        )

        assert result["supervision_state"] == "failed_terminal"
        assert "timeout expired" in result["last_error"]


def test_route_selection_timeout_within_limit_proceeds_normally():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "timeout_test_normal",
            elapsed_seconds=100,
        )

        result = resume_review_state(
            state,
            _request(state, "select_ont"),
            blocked_timeout_seconds=300,
        )

        assembly = result["stage_outputs"]["assembly"]
        assert assembly["status"] == "route_selected"
        assert assembly["selected_route"] == "ont"
        assert result["supervision_state"] == "running_stage"
        assert assembly["route_decision"]["decided_by"] == "human_override"
        assert "auto_accepted" not in assembly["route_decision"]["metadata"]


def test_route_selection_timeout_reads_from_review_policy_outcome():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "timeout_from_policy")
        payload = _request_payload(state)
        assert payload["review_policy_outcome"]["max_blocked_seconds"] == 300

        result = resume_review_state(
            state,
            _request(state, "select_hifi"),
            blocked_timeout_seconds=None,
        )

        assembly = result["stage_outputs"]["assembly"]
        timeout = assembly["route_decision"]["metadata"]["blocked_timeout"]
        assert assembly["selected_route"] == "hifi"
        assert assembly["route_decision"]["decided_by"] == "llm_auto"
        assert timeout["source"] == "review_request.review_policy_outcome.max_blocked_seconds"


def test_route_selection_timeout_conflict_explicit_arg_wins_and_warns():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "timeout_conflict",
            policy_seconds=600,
        )

        result = resume_review_state(
            state,
            _request(state, "select_ont"),
            blocked_timeout_seconds=300,
        )

        assembly = result["stage_outputs"]["assembly"]
        assert assembly["selected_route"] == "hifi"
        timeout = assembly["route_decision"]["metadata"]["blocked_timeout"]
        assert timeout["configured_seconds"] == 300
        assert timeout["source"] == "blocked_timeout_seconds"
        assert timeout["review_policy_outcome_max_blocked_seconds"] == 600
        assert "overrides" in timeout["warning"]


def test_route_selection_timeout_auto_accept_preserves_ont_route():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "timeout_ont",
            recommended_route="ont",
        )
        assert _request_payload(state)["recommended_assembly_selection"] == {
            "read_type": "ont_raw",
            "primary_method": "assembly.candidate.hifiasm_ont",
            "source": "recommendation",
            "rationale": "ont route recommended",
        }

        result = resume_review_state(
            state,
            _request(state, "select_cyclone"),
            blocked_timeout_seconds=300,
        )

        assembly = result["stage_outputs"]["assembly"]
        assert assembly["selected_route"] == "ont"
        assert assembly["route_decision"]["selected_route"] == "ont"
        assert assembly["assembly_selection"]["read_type"] == "ont_raw"


def test_route_selection_timeout_invalid_recommendation_fails_terminal():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(
            tmpdir,
            "timeout_bad_recommendation",
            recommended_selection={
                "read_type": "not-a-read-type",
                "primary_method": "assembly.candidate.hifiasm",
                "source": "recommendation",
                "rationale": "bad recommendation payload",
            },
        )

        result = resume_review_state(
            state,
            _request(state, "select_hifi"),
            blocked_timeout_seconds=None,
        )

        assert result["supervision_state"] == "failed_terminal"
        assert result["last_error"] == "assembly blocked timeout expired"
        assert result["blocked_timeout"]["source"] == (
            "review_request.review_policy_outcome.max_blocked_seconds"
        )


def test_timeout_terminal_update_delegates_shared_terminal_failure_shape():
    state = {"stage_outputs": {"assembly": {"status": "blocked_human"}}}
    request = ResumeReviewRequest(
        session_id="timeout_terminal",
        stage="assembly",
        gate_id="route_selection",
        decision="select_hifi",
    )
    timeout = {"configured_seconds": 300, "expired": True}

    result = timeout_terminal_update(state, request, timeout)

    assert result == {
        "current_stage": "assembly",
        "supervision_state": "failed_terminal",
        "active_interrupt": None,
        "last_error": "assembly blocked timeout expired",
        "stage_outputs": {"assembly": {"status": "blocked_human"}},
        "blocked_timeout": timeout,
    }


def test_route_selection_timeout_rejects_invalid_decision_before_timeout_resolution(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "timeout_invalid_decision")
        request = _request(state, "invalid_choice")

        def fail_if_timeout_resolution_runs(*args, **kwargs):
            raise AssertionError("timeout resolution ran before decision validation")

        monkeypatch.setattr(
            lifecycle,
            "_resolve_blocked_timeout_seconds",
            fail_if_timeout_resolution_runs,
        )
        with pytest.raises(ResumeValidationError, match="not allowed"):
            resume_review_state(state, request, blocked_timeout_seconds=None)


def test_route_selection_timeout_rejects_wrong_session_before_auto_accept():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "timeout_identity")
        request = _request(state, "select_hifi", session_id="wrong_session")

        with pytest.raises(ResumeValidationError, match="session_id"):
            resume_review_state(state, request, blocked_timeout_seconds=None)


def test_route_selection_timeout_rejects_wrong_gate_before_auto_accept():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "timeout_gate")
        request = _request(state, "select_hifi", gate_id="different_gate")

        with pytest.raises(ResumeValidationError, match="gate_id"):
            resume_review_state(state, request, blocked_timeout_seconds=None)


def test_route_selection_timeout_rejects_wrong_stage_before_auto_accept():
    with tempfile.TemporaryDirectory() as tmpdir:
        state = _blocked_route_state(tmpdir, "timeout_stage")
        request = _request(state, "select_hifi", stage="assembly_review")

        with pytest.raises(ResumeValidationError, match="stage"):
            resume_review_state(state, request, blocked_timeout_seconds=None)
