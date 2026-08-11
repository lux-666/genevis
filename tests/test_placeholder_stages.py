"""Contracts for parent-registered GapCloser and Base Polish placeholders."""

from __future__ import annotations

import tempfile
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from app.control.workflow_plan import workflow_steps_from_run_config
from app.entrypoints.ask_planner import _recommended_stages_from_manifest
from app.entrypoints.tui_intake import deterministic_workflow_intent
from app.graph.builder import build_graph
from app.schemas.run_config import RunConfig
from app.schemas.state import (
    StateUpdate,
    TopLevelState,
    initial_state,
    validate_stage_outputs,
)
from app.stages.base_polish.state import (
    read_base_polish_stage_output,
    update_base_polish_stage_output,
)
from app.stages.gapcloser.state import (
    read_gapcloser_stage_output,
    update_gapcloser_stage_output,
)
from app.storage.ledger import Ledger
from app.workflow import (
    MANUAL_ONLY_STAGES,
    recommended_workflow_template,
    stages_through,
)
from tests.data_v2 import v2_run_dir


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish"])
def test_explicit_placeholder_request_fails_closed_with_durable_evidence(
    stage: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = v2_run_dir(tmp, f"explicit-{stage}")
        session_id = f"explicit-{stage}"
        ledger = Ledger(run_dir)
        ledger.create_session(session_id, "control/manifest.yaml")
        config = RunConfig.model_validate(
            {"requested_stages": [{"stage": stage, "allow_local_execution": False}]}
        )
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            workflow_steps=workflow_steps_from_run_config(config),
        )
        state["current_stage"] = stage  # type: ignore[typeddict-item]
        state["workflow_step_index"] = 1
        state["entered_workflow_step_index"] = 0

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
        assert f"step-001-{stage}:{stage}_stage" in result["route_trace"]

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
        assert terminals == []


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
    expected = [
        "read_qc",
        "genome_survey",
        "assembly",
        "assembly_dedup",
        "assembly_review",
        "hic_scaffolding",
        "gapcloser",
        "t2t",
        "base_polish",
        "assembly_review",
    ]
    assert recommended_workflow_template("t2t") == expected
    assert intent.requested_stages == expected

    parsed = RunConfig(
        requested_stages=[
            {"stage": "gapcloser", "allow_local_execution": False},
            {"stage": "base_polish", "allow_local_execution": False},
        ],
    )
    assert parsed.requested_stage_names() == ["gapcloser", "base_polish"]
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "gapcloser", "allow_local_execution": True}
                ]
            }
        )


def test_manifest_recommendation_includes_complete_placeholder_positions(
    tmp_path,
) -> None:
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
    assert recommended[-5:] == [
        "hic_scaffolding",
        "gapcloser",
        "t2t",
        "base_polish",
        "assembly_review",
    ]


def test_contig_only_route_does_not_enter_placeholders() -> None:
    assert stages_through("assembly") == [
        "read_qc",
        "genome_survey",
        "assembly",
    ]
    assert all(stage not in stages_through("assembly") for stage in MANUAL_ONLY_STAGES)
    assert recommended_workflow_template("assembly") == [
        "read_qc",
        "genome_survey",
        "assembly",
        "assembly_dedup",
        "assembly_review",
    ]
