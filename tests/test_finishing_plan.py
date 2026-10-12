"""Finishing occurrence, execution-consent and planning contracts."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

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
from app.storage.ledger import Ledger
from app.workflow import (
    MANUAL_ONLY_STAGES,
    recommended_workflow_for_manifest,
    recommended_workflow_template,
    stages_through,
)
from tests.data_v2 import v2_run_dir


@pytest.mark.parametrize("stage", ["base_polish"])
def test_unapproved_polishing_fails_closed_with_durable_evidence(
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
        state["entered_workflow_step_index"] = 1

        result = build_graph(backend="memory", data_dir=str(run_dir)).invoke(
            state,
            config={"configurable": {"thread_id": session_id}},
        )

        reason = "Base Polish local execution is not authorized"
        assert result["current_stage"] == stage
        assert result["supervision_state"] == "failed_terminal"
        assert result["last_error"] == reason
        assert result["stage_outputs"][stage] == {
            "status": "failed",
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
            "base_polish",
            read_base_polish_stage_output,
            update_base_polish_stage_output,
        ),
    ],
)
def test_polishing_cursor_schema_is_strict(
    stage: str,
    reader: Callable[[TopLevelState], object],
    updater: Callable[[TopLevelState, dict[str, object]], StateUpdate],
) -> None:
    state = initial_state(session_id="schema", input_manifest_ref="manifest.yaml")
    update = updater(
        state,
        {
            "status": "failed",
            "reason": "execution not authorized",
        },
    )
    state["stage_outputs"] = update["stage_outputs"]
    assert reader(state) == {
        "status": "failed",
        "reason": "execution not authorized",
    }

    state["stage_outputs"] = validate_stage_outputs(
        {
            stage: {
                "status": "failed",
                "reason": "execution not authorized",
                "unexpected": True,
            }
        }
    )
    with pytest.raises(ValidationError):
        reader(state)


def test_finishing_stages_are_requestable() -> None:
    assert MANUAL_ONLY_STAGES == set()
    assert stages_through("t2t") == [
        "read_qc",
        "genome_survey",
        "assembly",
        "assembly_dedup",
        "assembly_review",
        "hic_scaffolding",
        "gapcloser",
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
    assert RunConfig.model_validate(
        {"requested_stages": [{"stage": "base_polish", "allow_local_execution": True}]}
    ).any_occurrence_allows_local_execution("base_polish")


def test_manifest_recommendation_includes_complete_finishing_positions(
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


def test_manifest_recommendations_use_the_canonical_workflow_helper(tmp_path) -> None:
    assembly = tmp_path / "assembly.fasta"
    assembly.write_text(">contig\nACGT\n", encoding="utf-8")
    assembly_manifest = tmp_path / "assembly_manifest.yaml"
    assembly_manifest.write_text(
        "session_id: assembly-s1\nassets:\n"
        "  - asset_type: assembly_fasta_registered\n"
        f"    paths:\n      - {assembly}\n"
        "    format: fasta\n    platform: unknown\n",
        encoding="utf-8",
    )

    assert _recommended_stages_from_manifest(str(assembly_manifest)) == list(
        recommended_workflow_for_manifest({"assembly_fasta_registered"})
    )
    assert recommended_workflow_for_manifest({"assembly_fasta_registered"}) == [
        "assembly_review",
        "hic_scaffolding",
        "gapcloser",
        "t2t",
        "base_polish",
        "assembly_review",
    ]
    hic_r1 = tmp_path / "hic_R1.fastq"
    hic_r2 = tmp_path / "hic_R2.fastq"
    hic_r1.write_text("@r1/1\nACGT\n+\n!!!!\n", encoding="utf-8")
    hic_r2.write_text("@r1/2\nTGCA\n+\n!!!!\n", encoding="utf-8")
    hic_manifest = tmp_path / "hic_manifest.yaml"
    hic_manifest.write_text(
        "session_id: hic-s1\nassets:\n"
        "  - asset_type: hic_reads_registered\n"
        f"    paths:\n      - {hic_r1}\n      - {hic_r2}\n"
        "    format: fastq\n    platform: illumina\n    layout: paired\n",
        encoding="utf-8",
    )

    hic_recommendation = [
        "hic_scaffolding",
        "gapcloser",
        "t2t",
        "base_polish",
        "assembly_review",
    ]
    assert (
        recommended_workflow_for_manifest({"hic_reads_registered"})
        == hic_recommendation
    )
    assert _recommended_stages_from_manifest(str(hic_manifest)) == hic_recommendation


def test_contig_only_route_does_not_enter_finishing() -> None:
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


def test_documentation_and_simplification_policy_is_final_state_only() -> None:
    project = Path(__file__).resolve().parents[1]

    governance_path = project / "docs/architecture/reference/governance.md"
    governance = governance_path.read_text(encoding="utf-8")
    recommendation_policy = project.joinpath(
        "docs/architecture/reference/recommendation-policy.md"
    ).read_text(encoding="utf-8")
    parameter_contract = project.joinpath(
        "docs/architecture/reference/parameter-recommendation.md"
    ).read_text(encoding="utf-8")
    stage_contract = project.joinpath(
        "docs/architecture/reference/stage-contracts.md"
    ).read_text(encoding="utf-8")
    review_topic = project.joinpath(
        "docs/architecture/topics/review-and-resume.md"
    ).read_text(encoding="utf-8")
    server_test = project.joinpath("scripts/server_test.sh").read_text(encoding="utf-8")

    for label in (
        "exact clone",
        "semantic duplication",
        "duplicate representation",
        "dead surface",
        "intentional symmetry",
    ):
        assert label in governance
    assert "注释只解释不明显的理由" in governance
    assert "省略中间尝试" in governance
    assert "从未合并的状态" in governance
    assert "## 边界规则" not in governance
    assert "## 评审门禁策略" not in governance
    for symbol in (
        "candidate_ids_from_review_request_payload",
        "validate_sha256",
        "parse_json_object",
        "validate_model_response_record",
        "_load_reference_snapshot",
        "_parameter_evidence_location",
        "_review_request_decision_context",
        "Ledger.record_method_disposition_recorded",
    ):
        assert symbol not in governance
    assert "内容寻址 JSON 发布" not in governance
    assert "`model_call`" in recommendation_policy
    assert "不把客户端类名当作模型身份" in recommendation_policy
    assert "_parameter_evidence_location" in parameter_contract
    assert "_load_reference_snapshot" not in stage_contract
    assert "不把任意对象转成" in review_topic
    assert "看似模型输出的字符串" in review_topic
    assert "intentional symmetry" not in server_test.lower()
    assert "npx" not in server_test
