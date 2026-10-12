"""Public intake, persistence and planning contracts for supplied material."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.entrypoints.tui import _planner_state_from_tui_draft, _replace_draft_asset
from app.entrypoints.tui_intake import (
    apply_asset_answer,
    create_rerun_tui_session_draft,
    create_tui_session_draft,
    load_tui_session_draft,
    materialize_manifest_draft,
    next_intake_clarification,
    parse_workflow_intent,
    save_tui_session_draft,
)


def draft(tmp_path, goal):
    return create_tui_session_draft(
        session_id="midstage", data_dir=str(tmp_path), request_text=goal
    )


@pytest.mark.parametrize(
    ("goal", "mode", "stages"),
    [
        ("已有组装需要评估", "existing_assembly", ["assembly_review"]),
        (
            "使用已质控长读段组装",
            "clean_long_reads",
            ["assembly", "assembly_dedup", "assembly_review"],
        ),
        (
            "使用已清洗 ONT 读段组装",
            "clean_long_reads",
            ["assembly", "assembly_dedup", "assembly_review"],
        ),
        ("已有组装和clean reads", "existing_assembly", ["assembly_review"]),
    ],
)
def test_explicit_modes_own_requirements_and_initial_scope(
    tmp_path, goal, mode, stages
):
    current = draft(tmp_path, goal)
    assert current.intent.entry_mode == mode
    assert current.intake.requirements == [mode]
    assert current.intent.requested_stages == stages
    assert next_intake_clarification(current).role == mode


def test_model_cannot_invent_midstage_input():
    intent = parse_workflow_intent(
        "做组装",
        llm_parser=lambda _: {"goal_text": "组装", "entry_mode": "existing_assembly"},
    )
    assert intent.entry_mode == "raw_reads"


def test_filenames_do_not_select_entry_mode_or_clean_platform(tmp_path):
    intent = parse_workflow_intent("做组装 /reads/existing_assembly.fa")
    assert intent.entry_mode == "raw_reads"
    current = draft(tmp_path, "使用clean reads /hifi/reads.fq")
    assert current.intent.clean_reads_platform is None


def test_initial_goal_can_supply_existing_assembly_and_incomplete_draft_can_resume(
    tmp_path,
):
    sequence = tmp_path / "assembly.fa"
    sequence.write_text(">a\nACGT\n")
    current = draft(tmp_path, "已有组装，以 Homo sapiens 为目标物种")
    save_tui_session_draft(current)
    current = load_tui_session_draft(tmp_path, current.session_id)
    assert next_intake_clarification(current).role == "existing_assembly"
    result = apply_asset_answer(
        current, answer=f"已有组装 {sequence}", focused_role=None
    )
    assert result.accepted_roles == ["existing_assembly"]


def test_clean_conflicting_platform_labels_do_not_reuse_goal_default(tmp_path):
    sequence = tmp_path / "reads.fq"
    sequence.write_text("@a\nACGT\n+\nIIII\n")
    current = draft(tmp_path, "clean ONT reads")
    result = apply_asset_answer(
        current, answer=f"ONT HiFi {sequence}", focused_role="clean_long_reads"
    )
    assert result.issues
    assert result.draft.intake.clean_long_reads is None


def test_clean_platform_is_explicit_and_survives_save_plan_and_rerun(tmp_path):
    reads = tmp_path / "pretend_hifi.fq"
    reads.write_text("@read\nACGT\n+\nIIII\n")
    current = draft(tmp_path, "用clean long reads组装，以 Homo sapiens 为目标物种")
    rejected = apply_asset_answer(
        current, answer=str(reads), focused_role="clean_long_reads"
    )
    assert rejected.draft.intake.clean_long_reads is None
    accepted = apply_asset_answer(
        rejected.draft, answer=f"ONT {reads}", focused_role="clean_long_reads"
    )
    assert accepted.accepted_roles == ["clean_long_reads"]
    assert accepted.draft.intake.clean_long_reads.platform == "ont"
    assert not accepted.unresolved_candidates
    save_tui_session_draft(accepted.draft)
    restored = load_tui_session_draft(tmp_path, current.session_id)
    manifest = materialize_manifest_draft(restored)
    assert [(a.asset_type, a.platform) for a in manifest.assets] == [
        ("clean_long_reads", "ont")
    ]
    plan = _planner_state_from_tui_draft(restored)
    assert plan.entry_request["platform"] == "ont"
    assert plan.entry_request["requested_stages"][0] == "assembly"
    child = create_rerun_tui_session_draft(restored, session_id="child")
    assert child.intake.clean_long_reads == restored.intake.clean_long_reads
    reads.unlink()
    missing = create_rerun_tui_session_draft(restored, session_id="missing")
    assert missing.intake.clean_long_reads is None
    assert missing.intake.unresolved_candidates


def test_existing_assembly_requires_one_readable_nonempty_fasta(tmp_path):
    import gzip

    current = draft(tmp_path, "评估已有组装，以 Homo sapiens 为目标物种")
    first = tmp_path / "first.fa"
    second = tmp_path / "second.fa"
    empty = tmp_path / "empty.fa"
    fastq = tmp_path / "reads.fq"
    first.write_text(">a\nACGT\n")
    second.write_text(">b\nACGT\n")
    empty.touch()
    fastq.write_text("@a\nACGT\n+\nIIII\n")
    compressed_empty = tmp_path / "empty.fa.gz"
    compressed_empty.write_bytes(gzip.compress(b""))
    wrong_format = tmp_path / "fake.fa"
    wrong_format.write_text("@a\nACGT\n+\nIIII\n")
    for answer in (
        f"{first} {second}",
        str(empty),
        str(fastq),
        str(compressed_empty),
        str(wrong_format),
        str(tmp_path / "missing.fa"),
        str(tmp_path),
    ):
        result = apply_asset_answer(
            current, answer=answer, focused_role="existing_assembly"
        )
        assert result.issues
        assert result.draft.intake.existing_assembly is None
    result = apply_asset_answer(
        current, answer=str(first), focused_role="existing_assembly"
    )
    assert next_intake_clarification(result.draft) is None
    manifest = materialize_manifest_draft(result.draft)
    assert manifest.assets[0].asset_type == "assembly_fasta_registered"
    assert manifest.assets[0].paths == [str(first.resolve())]
    with (
        patch("builtins.input", return_value=str(second)),
        patch(
            "app.entrypoints.tui._parse_assets_with_configured_llm",
            side_effect=AssertionError,
        ),
    ):
        replaced = _replace_draft_asset(result.draft, role="existing_assembly")
    assert replaced.intake.existing_assembly.paths == [str(second.resolve())]
    assert replaced.selected_stage_occurrences is None
    assert result.draft.intake.existing_assembly.paths == [str(first.resolve())]


def test_raw_facts_are_not_reinterpreted_as_midstage_material(tmp_path):
    from app.schemas.tui_intake import OntReadsFact

    current = draft(tmp_path, "使用clean ONT组装")
    current.intake.ont_reads = OntReadsFact(paths=[str(tmp_path / "raw.fq")])
    assert materialize_manifest_draft(current).assets == []
    save_tui_session_draft(current)
    assert (
        load_tui_session_draft(tmp_path, current.session_id).intake.clean_long_reads
        is None
    )


@pytest.mark.parametrize("mode", ["clean_long_reads", "existing_assembly"])
def test_intake_manifest_freezes_and_inventory_registers_current_material(
    tmp_path, mode
):
    from app.control.workflow_plan import workflow_steps_from_run_config
    from app.schemas.run_config import RunConfig
    from app.schemas.state import initial_state
    from app.stages.inventory.builder import build_stage
    from app.storage.frozen_inputs import freeze_session_inputs, validate_frozen_inputs
    from app.storage.paths import SessionPaths
    from tests.busco_fixture import ensure_test_host_envs

    sequence = tmp_path / "material.fa"
    sequence.write_text(">a\nACGTACGT\n")
    goal = "已有组装" if mode == "existing_assembly" else "clean ONT reads"
    current = draft(tmp_path / "data", goal + "，以 Fixture species 为目标物种")
    current = apply_asset_answer(current, answer=str(sequence), focused_role=mode).draft
    manifest = materialize_manifest_draft(current)
    stage = "assembly_review" if mode == "existing_assembly" else "assembly"
    config = RunConfig.model_validate(
        {"requested_stages": [{"stage": stage, "allow_local_execution": False}]}
    )
    paths = SessionPaths(current.data_dir, current.session_id)
    freeze_session_inputs(
        paths,
        manifest_draft=manifest,
        run_config_draft=config,
        config_path=ensure_test_host_envs(tmp_path),
        probe_environments=False,
    )
    bundle = validate_frozen_inputs(paths)
    state = initial_state(
        session_id=current.session_id,
        data_dir=str(paths.run_dir),
        input_manifest_ref=paths.to_ref(paths.input_manifest_path),
        workflow_steps=workflow_steps_from_run_config(bundle.run_config),
    )
    state["entered_workflow_step_index"] = 0
    result = build_stage(verified_inputs=bundle).invoke(state)
    assert result["stage_outputs"]["inventory"]["status"] == "completed"
    resource = "assembly" if mode == "existing_assembly" else "long_reads"
    delivered = result["resource_refs"][resource]["paths"]
    assert len(delivered) == 1
    assert Path(delivered[0]).read_text() == sequence.read_text()
    if mode == "clean_long_reads":
        assert result["resource_refs"][resource]["metadata"]["asset_types"] == [
            "clean_long_reads"
        ]
    assert [step["stage"] for step in result["workflow_steps"]] == ["inventory", stage]
