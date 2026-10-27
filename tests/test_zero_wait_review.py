"""Zero-wait policy through real immutable review/application storage."""

import json
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from app.control.errors import ResumeValidationError
from app.control.inspection import inspect_state
from app.control.resume import (
    build_foreground_auto_resume_request,
    normalize_resume_review_request,
)
from app.review.application import (
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
)
from app.review.gates import get_review_gate
from app.review.persistence import load_review_request, persist_review_request
from app.stages.finishing.advisor import history
from app.stages.finishing.workflow import build_stage
from app.stages.sequences import validate_assembly_fasta
from tests.test_dedup_recommendation_loop import Model
from tests.test_finishing_advisor import proposal, state_for


def automatic(state):
    summary = inspect_state(state)
    request = build_foreground_auto_resume_request(
        summary,
        configured_seconds=0,
        elapsed_seconds=0,
        data_dir=state["data_dir"],
    )
    canonical = normalize_resume_review_request(
        state,
        request,
        blocked_timeout_seconds=0,
        decision_source="foreground_countdown",
    )
    intent = persist_review_intent(state, canonical, source="foreground_countdown")
    gate = get_review_gate(request.stage, request.gate_id)
    prepared = gate.load_prepare_application()(state, intent.intent)
    ref = persist_review_application(state, intent=intent, prepared=prepared)
    verified = load_verified_review_application(state["data_dir"], ref)
    return {**state, **gate.load_apply_application()(state, verified)}, request


def zero_state(stage, tmp_path):
    state = state_for(stage, tmp_path)
    state["stage_outputs"][stage]["review_policy"] = {"max_blocked_seconds": 0}
    return state


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_zero_wait_runs_parameters_then_adopts_or_retains_input(tmp_path, stage):
    state = zero_state(stage, tmp_path)
    prior = state["resource_refs"]["assembly"]["paths"]
    model = Model(
        [
            AIMessage(content=json.dumps(proposal(stage))),
            # Missing independent quality evidence must not veto a valid product.
            AIMessage(content=json.dumps(proposal(stage, action="human_review"))),
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
        approved, decision = automatic(blocked)
        assert decision.decision == "follow_recommendation"
        assert decision.decided_by == "auto_rule"
        blocked = build_stage(stage).invoke(approved)
        assert len(history(blocked, stage)) == 1
        approved, decision = automatic(blocked)
        assert decision.decision == (
            "keep_prior_assembly" if stage == "t2t" else "accept_candidate"
        )
        final = build_stage(stage).invoke(approved)
    assert len(model.messages) == 1
    assert not final.get("active_interrupt")
    assert (
        final["resource_refs"]["assembly"]["paths"] == prior
        if stage == "t2t"
        else (final["resource_refs"]["assembly"]["paths"] != prior)
    )
    assert len(final["stage_outputs"][stage]["finishing"]["history_refs"]) == 1


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_zero_wait_preserves_budget_and_rejects_forged_action(tmp_path, stage):
    state = zero_state(stage, tmp_path)
    state["stage_outputs"][stage]["max_attempts"] = 0
    model = Model([AIMessage(content=json.dumps(proposal(stage)))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
    request = build_foreground_auto_resume_request(
        inspect_state(blocked),
        configured_seconds=0,
        elapsed_seconds=0,
        data_dir=blocked["data_dir"],
    )
    assert request.decision == "keep_prior_assembly"
    with pytest.raises(ResumeValidationError):
        normalize_resume_review_request(
            blocked,
            request.model_copy(update={"decision": "follow_recommendation"}),
            blocked_timeout_seconds=0,
            decision_source="foreground_countdown",
        )
    approved, _ = automatic(blocked)
    final = build_stage(stage).invoke(approved)
    assert not history(final, stage)
    assert not model.messages


def rebind_zero_wait(state):
    active = state["active_interrupt"]
    record = load_review_request(
        state["data_dir"], active["request_ref"], session_id=state["session_id"]
    )
    stage, gate = active["stage"], active["gate_id"]
    state["stage_outputs"][stage]["review_policy"] = {"max_blocked_seconds": 0}
    payload = {**record["payload"], "max_blocked_seconds": 0}
    persisted = persist_review_request(
        state,
        stage=stage,
        gate_id=gate,
        reason=record["reason"],
        payload=payload,
    )
    state["active_interrupt"] = persisted.compact_interrupt(
        stage=stage, gate_id=gate, reason=record["reason"]
    )
    return state


def test_zero_wait_final_review_accepts_current_with_no_quality_evidence():
    from tests.test_review_resume import _assembly_blocked_state

    state = rebind_zero_wait(_assembly_blocked_state())
    with patch("app.review.recommender.recommend_review_decision") as recommend:
        final, request = automatic(state)
    recommend.assert_not_called()
    assert request.candidate_id == "flye"
    assert final["stage_outputs"]["assembly_review"]["decision"] == "accept"
    assert final["supervision_state"] == "running_stage"
    assert not final["active_interrupt"]


@pytest.mark.parametrize(
    "content", ["", ">x\n", "not fasta", ">x\nXYZ\n", ">x\nACG\n>x\nTAA\n"]
)
def test_basic_validity_rejects_unusable_output(tmp_path, content):
    path = tmp_path / "assembly.fa"
    path.write_text(content)
    with pytest.raises(ValueError):
        validate_assembly_fasta(path)


def test_zero_wait_hic_adopts_valid_product(tmp_path):
    import os

    from app.stages.hic_scaffolding.builder import build_stage as build_hic
    from tests.test_hic_scaffolding import HiCScaffoldingStageTests

    fixture = HiCScaffoldingStageTests()
    assembly, reads, error_model, chromosome_model = fixture._inputs(str(tmp_path))
    envs = fixture._fake_autohic_environment(
        str(tmp_path), error_model, chromosome_model
    )
    state = fixture._configured_state(str(tmp_path), assembly, reads)
    state["stage_outputs"]["hic_scaffolding"]["review_policy"] = {
        "max_blocked_seconds": 0
    }
    blocked = fixture._run_to_parameter_gate(state)
    approved, request = automatic(blocked)
    assert request.decision == "approve_parameters"
    with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs)}):
        blocked = build_hic().invoke(approved)
    assert blocked["active_interrupt"]["gate_id"] == "hic_scaffolding"
    final, request = automatic(blocked)
    assert request.decision == "accept_candidate"
    assert final["resource_refs"]["assembly"]["paths"] != [str(assembly)]
    assert not final["active_interrupt"]


def test_zero_wait_dedup_adopts_instead_of_obeying_quality_veto(tmp_path):
    from app.stages.assembly_dedup.builder import build_stage as build_dedup
    from tests.test_dedup_recommendation_loop import _state
    from tests.test_dedup_recommendation_loop import proposal as dedup_proposal

    state, _ = _state(tmp_path)
    state["stage_outputs"]["assembly_dedup"]["review_policy"] = {
        "max_blocked_seconds": 0
    }
    model = Model(
        [
            AIMessage(content=json.dumps(dedup_proposal())),
            AIMessage(content=json.dumps(dedup_proposal("human_review"))),
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_dedup().invoke(state)
        approved, request = automatic(blocked)
        assert request.decision == "follow_recommendation"
        blocked = build_dedup().invoke(approved)
        approved, request = automatic(blocked)
        assert request.decision == "accept_candidate"
        final = build_dedup().invoke(approved)
    assert len(model.messages) == 1
    assert final["stage_outputs"]["assembly_dedup"]["adopted_attempt"] == 0
    assert not final.get("active_interrupt")


def test_zero_wait_rejects_corrupt_candidate_without_falling_back(tmp_path):
    from app.storage.paths import SessionPaths

    stage = "gapcloser"
    state = zero_state(stage, tmp_path)
    model = Model(
        [
            AIMessage(content=json.dumps(proposal(stage))),
            AIMessage(content=json.dumps(proposal(stage, action="keep_source"))),
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
        approved, _ = automatic(blocked)
        blocked = build_stage(stage).invoke(approved)
    product = history(blocked, stage)[0]["primary_ref"]
    SessionPaths.from_run_dir(blocked["data_dir"]).resolve_ref(
        product["path_ref"]
    ).write_text("corrupted\n")
    with pytest.raises(ValueError, match="integrity"):
        automatic(blocked)
    assert blocked["active_interrupt"]
    request_dir = (
        SessionPaths.from_run_dir(blocked["data_dir"])
        .resolve_ref(blocked["active_interrupt"]["request_ref"]["path_ref"])
        .parent
    )
    assert not (request_dir / "intent.json").exists()


def test_zero_wait_parameter_model_failure_is_not_successful_retention(tmp_path):
    stage = "gapcloser"
    state = zero_state(stage, tmp_path)
    model = Model([AIMessage(content="invalid json")])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
    with pytest.raises(ResumeValidationError, match="recommendation failed"):
        automatic(blocked)
    assert not history(blocked, stage)


def test_llm_stop_cannot_be_committed_as_final_failure():
    from app.review.base import ResumeReviewRequest
    from app.stages.assembly_review.nodes import validate_assembly_review_intent_request
    from tests.test_review_resume import _assembly_blocked_state

    state = _assembly_blocked_state()
    active = state["active_interrupt"]
    request = ResumeReviewRequest(
        session_id=state["session_id"],
        stage="assembly_review",
        gate_id="assembly_review",
        decision="stop_human_review",
        decided_by="llm_provisional",
        rationale_artifact_ref=active["request_ref"],
    )
    with pytest.raises(ResumeValidationError, match="explicit human stop"):
        validate_assembly_review_intent_request(state, request, {})
    assert state["supervision_state"] == "blocked_human"
    assert state["active_interrupt"] == active


def test_all_zero_run_can_start_without_terminal(tmp_path):
    import argparse

    from app.entrypoints.run import _run
    from tests.test_run_command import _prepared_inputs

    prepared = _prepared_inputs(requested_stages=["read_qc"])
    prepared.run_config.review_policy.max_blocked_seconds = 0
    with (
        patch(
            "app.storage.frozen_inputs.prepare_session_inputs", return_value=prepared
        ),
        patch("app.storage.frozen_inputs.freeze_session_inputs") as freeze,
        patch("app.entrypoints.run._foreground_review_available", return_value=False),
        patch("app.execution.run_readiness.preflight_run_readiness"),
        patch("app.entrypoints.run.execute_frozen_session", return_value=0) as execute,
    ):
        result = _run(
            argparse.Namespace(
                manifest="manifest.yaml", run_config="run.json", data_dir=str(tmp_path)
            )
        )
    assert result == 0
    freeze.assert_called_once()
    execute.assert_called_once()


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_zero_wait_semantic_recommendation_error_is_persisted_and_rejected(
    tmp_path, stage
):
    state = zero_state(stage, tmp_path)
    wrong_stage = "base_polish" if stage != "base_polish" else "gapcloser"
    model = Model([AIMessage(content=json.dumps(proposal(wrong_stage)))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
    record = load_review_request(
        blocked["data_dir"],
        blocked["active_interrupt"]["request_ref"],
        session_id=blocked["session_id"],
    )
    assert record["payload"]["recommendation_error"] == "wrong stage parameter shape"
    with pytest.raises(ResumeValidationError, match="wrong stage parameter shape"):
        automatic(blocked)
    assert not history(blocked, stage)


def test_zero_wait_dedup_invalid_recommendation_is_not_successful_retention(tmp_path):
    from app.stages.assembly_dedup.builder import build_stage as build_dedup
    from tests.test_dedup_recommendation_loop import _state
    from tests.test_dedup_recommendation_loop import proposal as dedup_proposal

    state, _ = _state(tmp_path)
    state["stage_outputs"]["assembly_dedup"]["review_policy"] = {
        "max_blocked_seconds": 0
    }
    model = Model(
        [AIMessage(content=json.dumps(dedup_proposal("accept", selected=99)))]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_dedup().invoke(state)
    with pytest.raises(ResumeValidationError, match="unavailable attempt"):
        automatic(blocked)


@pytest.mark.parametrize("content", [">bad\nXYZ\n", ">x\nACG\n>x\nTAA\n"])
def test_dedup_rejects_invalid_fasta_before_candidate_adoption(tmp_path, content):
    import shlex

    from app.stages.assembly_dedup.builder import build_stage as build_dedup
    from tests.test_dedup_recommendation_loop import _state
    from tests.test_dedup_recommendation_loop import proposal as dedup_proposal

    state, _ = _state(tmp_path)
    state["stage_outputs"]["assembly_dedup"]["review_policy"] = {
        "max_blocked_seconds": 0
    }
    prior = state["resource_refs"]["assembly"]
    tool = tmp_path / "bin/get_seqs"
    tool.write_text(
        tool.read_text().replace(
            'cat "$last" > "${prefix}.purged.fa"',
            "printf %s " + shlex.quote(content) + ' > "${prefix}.purged.fa"',
        )
    )
    model = Model([AIMessage(content=json.dumps(dedup_proposal()))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_dedup().invoke(state)
        approved, _ = automatic(blocked)
        failed = build_dedup().invoke(approved)
    assert failed["stage_outputs"]["assembly_dedup"]["status"] == "failed"
    assert (
        "invalid purge_dups FASTA"
        in failed["stage_outputs"]["assembly_dedup"]["reason"]
    )
    assert failed["resource_refs"]["assembly"] == prior
    assert len(model.messages) == 1


@pytest.mark.parametrize("tool_name", ["quast", "busco"])
def test_final_review_rejects_successful_tool_with_corrupt_report(tmp_path, tool_name):
    import os

    from app.stages.assembly_review.builder import build_stage as build_review
    from tests.busco_fixture import ensure_busco_lineage
    from tests.test_assembly_review import AssemblyReviewStageTests

    fixture = AssemblyReviewStageTests()
    try:
        draft = tmp_path / "draft.fasta"
        draft.write_text(">ctg1\nACGTACGT\n")
        candidate = fixture._write_candidate_bundle(
            str(tmp_path),
            candidate_id="flye_01",
            method_name="assembly.candidate.flye",
            draft_assembly_fasta=str(draft),
        )
        bins = fixture._fake_quast_bin(str(tmp_path))
        fixture._fake_busco_bin(str(tmp_path))
        tool = bins / ("quast.py" if tool_name == "quast" else "busco")
        target = '"$outdir/report.tsv"' if tool_name == "quast" else '"$summary"'
        tool.write_text(
            tool.read_text() + f"\nprintf 'malformed report\\n' > {target}\n"
        )
        lineage = ensure_busco_lineage(str(tmp_path))
        state = fixture._build_state(entry_mode="raw_inputs", data_dir=str(tmp_path))
        state["stage_outputs"]["assembly"] = {
            "status": "draft_ready",
            "selected_candidate_refs": [str(candidate)],
        }
        fixture._set_candidate_refs(state, [str(candidate)])
        state["stage_outputs"]["assembly_review"] = {
            "execution_mode": "local",
            "review_policy": {"max_blocked_seconds": 0},
            "related_species_reference_dir": str(fixture._test_reference_dir(state)),
            "env": {"PATH": f"{bins}{os.pathsep}{os.environ['PATH']}"},
            "quast_parameters": {"threads": 1},
            "busco_parameters": {
                "lineage_path": str(lineage.resolve()),
                "lineage_name": "embryophyta_odb10",
                "lineage_version": "odb10",
                "lineage_source": "test_fixture",
                "mode": "genome",
                "threads": 1,
            },
        }
        failed = build_review().invoke(state)
        cursor = failed["stage_outputs"]["assembly_review"]
        assert cursor["status"] == "failed"
        assert cursor["failure_kind"] == "collect"
        assert "collect failed" in cursor["reason"]
        assert not failed.get("active_interrupt")
        assert not any(
            s["signal_name"] == tool_name and s["status"] == "available"
            for s in cursor.get("signals", [])
        )
    finally:
        fixture.doCleanups()
