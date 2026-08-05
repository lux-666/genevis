"""Contracts for parent-registered GapCloser and Base Polish placeholders."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from unittest.mock import patch

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.entrypoints.ask_planner import _recommended_stages_from_manifest
from app.entrypoints.review_interactive import ASSEMBLY_REVIEW_DOWNSTREAM_FLOW
from app.entrypoints.tui_intake import deterministic_workflow_intent
from app.graph.builder import build_graph
from app.review.annotation_intake import DOWNSTREAM_FLOW
from app.schemas.run_config import RunConfig
from app.schemas.state import (
    StateUpdate,
    TopLevelState,
    initial_state,
    validate_stage_outputs,
)
from app.stages.base import append_route_trace
from app.stages.base_polish.state import (
    read_base_polish_stage_output,
    update_base_polish_stage_output,
)
from app.stages.gapcloser.state import (
    read_gapcloser_stage_output,
    update_gapcloser_stage_output,
)
from app.storage.ledger import Ledger
from app.workflow import MANUAL_ONLY_STAGES, STAGE_ORDER, stages_through
from tests.data_v2 import v2_run_dir


def _terminal_stage(stage: str):
    """Build a no-process terminal stage used to stop legacy traversal tests."""

    def complete(state: TopLevelState) -> StateUpdate:
        return {
            "current_stage": "done",
            "supervision_state": "completed",
            "route_trace": append_route_trace(state, f"{stage}_stage"),
        }

    graph = StateGraph(TopLevelState)
    graph.add_node("complete", complete)
    graph.add_edge(START, "complete")
    graph.add_edge("complete", END)
    return graph.compile()


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish"])
def test_explicit_placeholder_request_fails_closed_with_durable_evidence(
    stage: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = v2_run_dir(tmp, f"explicit-{stage}")
        session_id = f"explicit-{stage}"
        ledger = Ledger(run_dir)
        ledger.create_session(session_id, "control/manifest.yaml")
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            requested_stages=[stage],  # type: ignore[list-item]
        )
        state["current_stage"] = stage  # type: ignore[typeddict-item]

        with patch("app.reports.review_report.write_operator_report"):
            result = build_graph(backend="memory", data_dir=str(run_dir)).invoke(
                state,
                config={"configurable": {"thread_id": session_id}},
            )

        reason = (
            f"{stage} is registered in the parent graph but has no implemented method"
        )
        assert result["current_stage"] == stage
        assert result["supervision_state"] == "failed_terminal"
        assert result["last_error"] == reason
        assert result["stage_outputs"][stage] == {
            "status": "blocked_not_implemented",
            "reason": reason,
        }
        assert f"{stage}_stage" in result["route_trace"]

        rows = ledger.read_event_rows(session_id)
        assert any(
            row["event_type"] == "stage_failed" and row["stage"] == stage
            for row in rows
        )
        assert not any(
            row["event_type"] == "stage_completed" and row["stage"] == stage
            for row in rows
        )
        terminals = [row for row in rows if row["event_type"] == "session_terminal"]
        assert len(terminals) == 1
        assert terminals[0]["summary"]["outcome"] == "failed"
        assert terminals[0]["stage"] == stage


@pytest.mark.parametrize(
    ("stage", "next_stage", "builder_name"),
    [
        ("gapcloser", "t2t", "build_t2t_stage"),
        ("base_polish", "repeat_annotation", "build_repeat_annotation_stage"),
    ],
)
def test_legacy_unscoped_traversal_skips_placeholder_without_success_evidence(
    stage: str,
    next_stage: str,
    builder_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = v2_run_dir(tmp, f"legacy-{stage}")
        session_id = f"legacy-{stage}"
        ledger = Ledger(run_dir)
        ledger.create_session(session_id, "control/manifest.yaml")
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            requested_stages=None,
        )
        state["current_stage"] = stage  # type: ignore[typeddict-item]

        with patch(
            f"app.graph.builder.{builder_name}",
            return_value=_terminal_stage(next_stage),
        ):
            result = build_graph(backend="memory", data_dir=str(run_dir)).invoke(
                state,
                config={"configurable": {"thread_id": session_id}},
            )

        assert result["current_stage"] == "done"
        assert result["skip_reasons"][stage] == "not_implemented"
        assert stage not in result["stage_outputs"]
        assert f"{stage}_stage" not in result["route_trace"]

        rows = ledger.read_event_rows(session_id)
        skipped = [
            row
            for row in rows
            if row["event_type"] == "stage_skipped" and row["stage"] == stage
        ]
        assert len(skipped) == 1
        assert skipped[0]["summary"]["reason"] == "not_implemented"
        assert not any(
            row["event_type"] in {"stage_completed", "stage_failed"}
            and row["stage"] == stage
            for row in rows
        )


@pytest.mark.parametrize(
    ("stage", "reader", "updater"),
    [
        (
            "gapcloser",
            read_gapcloser_stage_output,
            update_gapcloser_stage_output,
        ),
        (
            "base_polish",
            read_base_polish_stage_output,
            update_base_polish_stage_output,
        ),
    ],
)
def test_placeholder_cursor_schema_is_strict(
    stage: str,
    reader: Callable[[TopLevelState], object],
    updater: Callable[[TopLevelState, dict[str, object]], StateUpdate],
) -> None:
    state = initial_state(session_id="schema", input_manifest_ref="manifest.yaml")
    update = updater(
        state,
        {
            "status": "blocked_not_implemented",
            "reason": "not implemented",
        },
    )
    state["stage_outputs"] = update["stage_outputs"]
    assert reader(state) == {
        "status": "blocked_not_implemented",
        "reason": "not implemented",
    }

    state["stage_outputs"] = validate_stage_outputs(
        {
            stage: {
                "status": "blocked_not_implemented",
                "reason": "not implemented",
                "unexpected": True,
            }
        }
    )
    with pytest.raises(ValidationError):
        reader(state)

    state["stage_outputs"] = validate_stage_outputs(
        {stage: {"status": "blocked_not_implemented"}}
    )
    with pytest.raises(ValidationError):
        reader(state)


def test_placeholder_stages_are_manual_only_but_requestable() -> None:
    assert MANUAL_ONLY_STAGES == {"gapcloser", "base_polish"}
    assert stages_through("t2t") == [
        "read_qc",
        "genome_survey",
        "assembly",
        "assembly_dedup",
        "assembly_review",
        "hic_scaffolding",
        "t2t",
    ]
    intent = deterministic_workflow_intent("对这个样本做 T2T 组装")
    assert "gapcloser" not in intent.requested_stages
    assert "base_polish" not in intent.requested_stages

    parsed = RunConfig(
        requested_stages=["gapcloser", "base_polish"],
        local_exec=[],
    )
    assert parsed.requested_stages == ["gapcloser", "base_polish"]
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {"requested_stages": ["gapcloser"], "local_exec": ["gapcloser"]}
        )


def test_manifest_recommendation_omits_manual_only_placeholders(tmp_path) -> None:
    reads = tmp_path / "reads.fq"
    reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "session_id: s1\nassets:\n"
        "  - asset_type: raw_long_reads\n"
        f"    paths:\n      - {reads}\n"
        "    format: fastq\n    platform: ont\n",
        encoding="utf-8",
    )

    recommended = _recommended_stages_from_manifest(str(manifest))
    assert "t2t" in recommended
    assert "gapcloser" not in recommended
    assert "base_polish" not in recommended


def test_contig_only_route_does_not_enter_placeholders() -> None:
    requested = ["read_qc", "genome_survey", "assembly"]
    assert all(stage not in requested for stage in MANUAL_ONLY_STAGES)
    assert STAGE_ORDER.index("gapcloser") > STAGE_ORDER.index("assembly")
    assert STAGE_ORDER.index("base_polish") > STAGE_ORDER.index("assembly")


def test_operator_downstream_flows_follow_canonical_parent_order() -> None:
    expected = " -> ".join(STAGE_ORDER[STAGE_ORDER.index("assembly_review") :])
    assert ASSEMBLY_REVIEW_DOWNSTREAM_FLOW == expected
    assert DOWNSTREAM_FLOW == " -> ".join(
        STAGE_ORDER[STAGE_ORDER.index("assembly_review") + 1 :]
    )
