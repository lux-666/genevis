"""Tests for route selection timeout auto-accept behavior."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.control.lifecycle as lifecycle
from app.control.lifecycle import ResumeValidationError, resume_review_state, timeout_terminal_update
from app.review.base import ResumeReviewRequest
from app.schemas.state import initial_state


def test_route_selection_timeout_auto_accepts_recommendation():
    """P1 fix: timeout should auto-accept recommended route, not fail"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up blocked state with route selection
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)  # 400 seconds ago
        state = initial_state(
            session_id="timeout_test",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {
            "assembly": {
                "status": "blocked_human",
                "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            }
        }
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "reason": "assembly_route_human_confirmation_required",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "recommendation_reasoning": "HiFi reads available",
            "uncertainty_notes": [],
            "blocked_at": blocked_at.isoformat(),
            "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            "review_policy_outcome": {
                "action": "BLOCK_WITH_TIMEOUT",
                "mode": "strict",
                "stage": "assembly",
                "auto_decidable": False,
                "max_blocked_seconds": 300,
                "reason": "strict_policy_timeout_configured",
            },
        }

        # Create artifacts directory
        Path(tmpdir).joinpath("artifacts").mkdir(exist_ok=True)

        request = ResumeReviewRequest(
            session_id="timeout_test",
            stage="assembly",
            gate_id="route_selection",
            decision="select_hifi",  # This would normally be user input
        )

        # Resume with timeout check (300 seconds configured, 400 elapsed)
        result = resume_review_state(state, request, blocked_timeout_seconds=300)

        # Should auto-accept the recommended route
        assembly = result["stage_outputs"]["assembly"]
        assert assembly["status"] == "route_selected"
        assert assembly["selected_route"] == "hifi"
        assert result["supervision_state"] == "running_stage"

        # Check that it was auto-accepted with correct attribution
        route_decision = assembly.get("route_decision", {})
        assert "auto_accepted" in route_decision.get("metadata", {})
        assert route_decision.get("decided_by") == "llm_auto"


def test_route_selection_timeout_with_insufficient_data_fails():
    """Timeout with insufficient_data recommendation should fail, not auto-accept"""
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        state = initial_state(
            session_id="timeout_test_fail",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {
            "assembly": {
                "status": "blocked_human",
                "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            }
        }
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "reason": "assembly_route_insufficient_data",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_route": "insufficient_data",
            "recommendation_reasoning": "Cannot determine route",
            "uncertainty_notes": ["No reads available"],
            "blocked_at": blocked_at.isoformat(),
            "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            "review_policy_outcome": {
                "action": "BLOCK_WITH_TIMEOUT",
                "mode": "strict",
                "stage": "assembly",
                "auto_decidable": False,
                "max_blocked_seconds": 300,
                "reason": "strict_policy_timeout_configured",
            },
        }

        Path(tmpdir).joinpath("artifacts").mkdir(exist_ok=True)

        request = ResumeReviewRequest(
            session_id="timeout_test_fail",
            stage="assembly",
            gate_id="route_selection",
            decision="select_hifi",
        )

        result = resume_review_state(state, request, blocked_timeout_seconds=300)

        # Should fail with timeout expired
        assert result["supervision_state"] == "failed_terminal"
        assert "timeout expired" in result.get("last_error", "")


def test_route_selection_timeout_within_limit_proceeds_normally():
    """Within timeout limit, should proceed with user decision normally"""
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=100)  # 100 seconds ago
        state = initial_state(
            session_id="timeout_test_normal",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {
            "assembly": {
                "status": "blocked_human",
                "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            }
        }
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "reason": "assembly_route_human_confirmation_required",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "recommendation_reasoning": "HiFi reads available",
            "blocked_at": blocked_at.isoformat(),
            "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
        }

        Path(tmpdir).joinpath("artifacts").mkdir(exist_ok=True)

        request = ResumeReviewRequest(
            session_id="timeout_test_normal",
            stage="assembly",
            gate_id="route_selection",
            decision="select_ont",  # User chooses different route
        )

        # Resume within timeout (300 seconds configured, 100 elapsed)
        result = resume_review_state(state, request, blocked_timeout_seconds=300)

        # Should use user's choice, not recommendation
        assembly = result["stage_outputs"]["assembly"]
        assert assembly["status"] == "route_selected"
        assert assembly["selected_route"] == "ont"
        assert result["supervision_state"] == "running_stage"

        # Normal human decision should use human_override
        route_decision = assembly.get("route_decision", {})
        assert route_decision.get("decided_by") == "human_override"
        assert "auto_accepted" not in route_decision.get("metadata", {})


def test_route_selection_timeout_reads_from_review_policy_outcome():
    """P1 fix: timeout should be read from review_policy_outcome when not explicitly provided"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Set up blocked state with timeout in review_policy_outcome
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)  # 400 seconds ago
        state = initial_state(
            session_id="timeout_from_policy",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {
            "assembly": {
                "status": "blocked_human",
                "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            }
        }
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "reason": "assembly_route_human_confirmation_required",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "recommendation_reasoning": "HiFi reads available",
            "blocked_at": blocked_at.isoformat(),
            "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            "review_policy_outcome": {
                "action": "BLOCK_WITH_TIMEOUT",
                "mode": "strict",
                "stage": "assembly",
                "auto_decidable": False,
                "max_blocked_seconds": 300,  # This should be used when blocked_timeout_seconds is None
                "reason": "strict_policy_timeout_configured",
            },
        }

        Path(tmpdir).joinpath("artifacts").mkdir(exist_ok=True)

        request = ResumeReviewRequest(
            session_id="timeout_from_policy",
            stage="assembly",
            gate_id="route_selection",
            decision="select_hifi",
        )

        # Resume WITHOUT explicit blocked_timeout_seconds - should read from review_policy_outcome
        result = resume_review_state(state, request, blocked_timeout_seconds=None)

        # Should auto-accept because timeout (300s) expired (400s elapsed)
        assembly = result["stage_outputs"]["assembly"]
        assert assembly["status"] == "route_selected"
        assert assembly["selected_route"] == "hifi"
        assert result["supervision_state"] == "running_stage"

        # Should be auto-accepted with correct attribution
        route_decision = assembly.get("route_decision", {})
        assert "auto_accepted" in route_decision.get("metadata", {})
        assert route_decision.get("decided_by") == "llm_auto"


def test_route_selection_timeout_conflict_explicit_arg_wins_and_warns():
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        state = initial_state(
            session_id="timeout_conflict",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {
            "assembly": {
                "status": "blocked_human",
                "review_policy": {"mode": "strict", "max_blocked_seconds": 600},
            }
        }
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "reason": "assembly_route_human_confirmation_required",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "recommendation_reasoning": "HiFi reads available",
            "blocked_at": blocked_at.isoformat(),
            "review_policy": {"mode": "strict", "max_blocked_seconds": 600},
            "review_policy_outcome": {
                "action": "BLOCK_WITH_TIMEOUT",
                "mode": "strict",
                "stage": "assembly",
                "auto_decidable": False,
                "max_blocked_seconds": 600,
                "reason": "strict_policy_timeout_configured",
            },
        }

        Path(tmpdir).joinpath("artifacts").mkdir(exist_ok=True)

        request = ResumeReviewRequest(
            session_id="timeout_conflict",
            stage="assembly",
            gate_id="route_selection",
            decision="select_ont",
        )

        result = resume_review_state(state, request, blocked_timeout_seconds=300)

        assembly = result["stage_outputs"]["assembly"]
        assert assembly["selected_route"] == "hifi"
        timeout = assembly["route_decision"]["metadata"]["blocked_timeout"]
        assert timeout["configured_seconds"] == 300
        assert timeout["source"] == "blocked_timeout_seconds"
        assert timeout["review_policy_outcome_max_blocked_seconds"] == 600
        assert "overrides" in timeout["warning"]


def test_route_selection_timeout_auto_accept_preserves_ont_route():
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        state = initial_state(
            session_id="timeout_ont",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {
            "assembly": {
                "status": "blocked_human",
                "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            }
        }
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "reason": "assembly_route_human_confirmation_required",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_route": "ont",
            "recommended_assembly_selection": {
                "read_type": "ont_raw",
                "primary_method": "assembly.candidate.hifiasm_ont",
                "source": "recommendation",
                "rationale": "ONT route recommended",
            },
            "recommendation_reasoning": "ONT reads available",
            "blocked_at": blocked_at.isoformat(),
            "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            "review_policy_outcome": {
                "action": "BLOCK_WITH_TIMEOUT",
                "mode": "strict",
                "stage": "assembly",
                "auto_decidable": False,
                "max_blocked_seconds": 300,
                "reason": "strict_policy_timeout_configured",
            },
        }

        Path(tmpdir).joinpath("artifacts").mkdir(exist_ok=True)

        request = ResumeReviewRequest(
            session_id="timeout_ont",
            stage="assembly",
            gate_id="route_selection",
            decision="select_cyclone",
        )

        result = resume_review_state(state, request, blocked_timeout_seconds=300)

        assembly = result["stage_outputs"]["assembly"]
        assert assembly["selected_route"] == "ont"
        assert assembly["route_decision"]["selected_route"] == "ont"
        assert assembly["assembly_selection"]["read_type"] == "ont_raw"


def test_route_selection_timeout_invalid_recommendation_fails_terminal():
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        state = initial_state(
            session_id="timeout_bad_recommendation",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {"assembly": {"status": "blocked_human"}}
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_assembly_selection": {
                "read_type": "not-a-read-type",
                "primary_method": "assembly.candidate.hifiasm",
                "source": "recommendation",
                "rationale": "bad recommendation payload",
            },
            "blocked_at": blocked_at.isoformat(),
            "review_policy_outcome": {"max_blocked_seconds": 300},
        }

        request = ResumeReviewRequest(
            session_id="timeout_bad_recommendation",
            stage="assembly",
            gate_id="route_selection",
            decision="select_hifi",
        )

        result = resume_review_state(state, request, blocked_timeout_seconds=None)

        assert result["supervision_state"] == "failed_terminal"
        assert result["last_error"] == "assembly blocked timeout expired"
        assert result["blocked_timeout"]["source"] == "active_interrupt.review_policy_outcome.max_blocked_seconds"


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
    """Timeout must not bypass the active interrupt's decision whitelist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)  # 400 seconds ago
        state = initial_state(
            session_id="timeout_invalid_decision",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {
            "assembly": {
                "status": "blocked_human",
                "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            }
        }
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "reason": "assembly_route_human_confirmation_required",
            "decisions_allowed": ["select_cyclone", "select_ont", "select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "recommendation_reasoning": "HiFi reads available",
            "blocked_at": blocked_at.isoformat(),
            "review_policy": {"mode": "strict", "max_blocked_seconds": 300},
            "review_policy_outcome": {
                "action": "BLOCK_WITH_TIMEOUT",
                "mode": "strict",
                "stage": "assembly",
                "auto_decidable": False,
                "max_blocked_seconds": 300,
                "reason": "strict_policy_timeout_configured",
            },
        }

        Path(tmpdir).joinpath("artifacts").mkdir(exist_ok=True)

        # User provides an invalid decision that's not in decisions_allowed
        request = ResumeReviewRequest(
            session_id="timeout_invalid_decision",
            stage="assembly",
            gate_id="route_selection",
            decision="invalid_choice",  # This would normally be rejected by validation
        )

        def fail_if_timeout_resolution_runs(*args, **kwargs):
            raise AssertionError("timeout resolution ran before decision validation")

        monkeypatch.setattr(lifecycle, "_resolve_blocked_timeout_seconds", fail_if_timeout_resolution_runs)

        with pytest.raises(ResumeValidationError, match="not allowed"):
            resume_review_state(state, request, blocked_timeout_seconds=None)


def test_route_selection_timeout_rejects_wrong_session_before_auto_accept():
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        state = initial_state(
            session_id="timeout_identity",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {"assembly": {"status": "blocked_human"}}
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "decisions_allowed": ["select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "blocked_at": blocked_at.isoformat(),
            "review_policy_outcome": {"max_blocked_seconds": 300},
        }

        request = ResumeReviewRequest(
            session_id="wrong_session",
            stage="assembly",
            gate_id="route_selection",
            decision="invalid_choice",
        )

        with pytest.raises(ResumeValidationError, match="session_id"):
            resume_review_state(state, request, blocked_timeout_seconds=None)


def test_route_selection_timeout_rejects_wrong_gate_before_auto_accept():
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        state = initial_state(
            session_id="timeout_gate",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {"assembly": {"status": "blocked_human"}}
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "decisions_allowed": ["select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "blocked_at": blocked_at.isoformat(),
            "review_policy_outcome": {"max_blocked_seconds": 300},
        }

        request = ResumeReviewRequest(
            session_id="timeout_gate",
            stage="assembly",
            gate_id="different_gate",
            decision="invalid_choice",
        )

        with pytest.raises(ResumeValidationError, match="gate_id"):
            resume_review_state(state, request, blocked_timeout_seconds=None)


def test_route_selection_timeout_rejects_wrong_stage_before_auto_accept():
    with tempfile.TemporaryDirectory() as tmpdir:
        blocked_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        state = initial_state(
            session_id="timeout_stage",
            data_dir=tmpdir,
            input_manifest_ref="manifest.yaml",
        )
        state["supervision_state"] = "blocked_human"
        state["stage_outputs"] = {"assembly": {"status": "blocked_human"}}
        state["active_interrupt"] = {
            "stage": "assembly",
            "gate_id": "route_selection",
            "decisions_allowed": ["select_hifi", "stop_human_review"],
            "recommended_route": "hifi",
            "blocked_at": blocked_at.isoformat(),
            "review_policy_outcome": {"max_blocked_seconds": 300},
        }

        request = ResumeReviewRequest(
            session_id="timeout_stage",
            stage="assembly_review",
            gate_id="route_selection",
            decision="invalid_choice",
        )

        with pytest.raises(ResumeValidationError, match="stage"):
            resume_review_state(state, request, blocked_timeout_seconds=None)
