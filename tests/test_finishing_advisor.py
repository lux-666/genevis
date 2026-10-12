"""Finishing feedback loops with real storage/review/lifecycle and fake tools."""

import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from app.llm.finishing import FinishingRecommendation, FinishingRecommender
from app.review.application import (
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
)
from app.review.base import ResumeReviewRequest
from app.stages.finishing.advisor import history, loop, validate_proposal
from app.stages.finishing.review import apply_application, prepare_application
from app.stages.finishing.workflow import build_stage
from tests.test_dedup_recommendation_loop import Model


def proposal(stage, index=0, action="run", selected=None):
    parameters = (
        {
            "gapcloser": {
                "min_identity": 0.8 + index * 0.05,
                "min_match": 200,
                "gap_size_check": True,
            },
            "base_polish": {
                "window_length": 500 + index * 100,
                "error_threshold": 0.3,
                "match": 3,
                "mismatch": -5,
                "gap": -4,
            },
            "t2t": {
                "min_motif_copies": 4,
                "terminal_window_bp": 10000,
                "min_mapq": 20 + index,
                "max_target_terminal_distance_bp": 1000,
                "min_overlap_bp": 500,
                "min_supporting_reads": 1,
            },
        }[stage]
        if action == "run"
        else None
    )
    return {
        "action": action,
        "parameters": parameters,
        "selected_attempt": selected,
        "reason": "Test a distinct configuration using supplied observations.",
        "evidence_gaps": ["Independent accuracy unavailable"],
    }


def test_default_finishing_seeds_use_advisor_and_t2t_patch_proposals():
    from app.entrypoints.run import _stage_seeds_from_run_config
    from app.schemas.run_config import RunConfig

    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": stage, "allow_local_execution": True}
                for stage in ("gapcloser", "t2t", "base_polish")
            ]
        }
    )
    seeds = _stage_seeds_from_run_config(config, shared_parameters={})
    for stage in ("gapcloser", "t2t", "base_polish"):
        assert seeds[stage]["recommendation_mode"] == "advisor"
        assert seeds[stage]["max_attempts"] == 3
    assert seeds["t2t"]["mode"] == "auto_patch"


def quast_tool(binary):
    tool = binary / "quast.py"
    tool.write_text("""#!/usr/bin/env python3
import sys
from pathlib import Path
args=sys.argv
out=Path(args[args.index('-o')+1]); out.mkdir(parents=True, exist_ok=True)
labels=args[args.index('--labels')+1].split(',')
(out/'report.tsv').write_text('Assembly\\t'+'\\t'.join(labels)+'\\nN50\\t'+'\\t'.join(['1000']*len(labels))+'\\n')
""")
    tool.chmod(0o755)


def state_for(stage, tmp_path):
    if stage == "gapcloser":
        from tests.test_gapcloser import _fake_tool, _state

        state = _state(tmp_path)
        path = _fake_tool(tmp_path)
    elif stage == "base_polish":
        from tests.test_base_polish import fake_tools, state_for_polish

        state = state_for_polish(tmp_path)
        path = fake_tools(tmp_path)
    else:
        from tests.test_t2t import T2TStageTests

        fixture = T2TStageTests()
        assembly, reads = fixture._write_inputs(str(tmp_path))
        state = fixture._state(str(tmp_path), assembly, reads)
        state["shared_parameters"] = {"species_facts": {"telomere_motif": "TTAGGG"}}
        binary = tmp_path / "bin"
        binary.mkdir()
        mm = binary / "minimap2"
        # Empty PAF is a valid no-placement outcome; read motif scanning still runs.
        mm.write_text("#!/bin/sh\nexit 0\n")
        mm.chmod(0o755)
        path = str(binary) + os.pathsep + os.environ["PATH"]
    binary = Path(path.split(os.pathsep)[0])
    quast_tool(binary)
    state["workflow_steps"][state["workflow_step_index"]]["selected_method_names"] = [
        {
            "gapcloser": "gapcloser.tgs_gapcloser",
            "base_polish": "base_polish.racon",
            "t2t": "t2t.alignment.minimap2",
        }[stage],
        f"{stage}.eval.quast",
    ]
    state["stage_outputs"] = {
        stage: {
            "recommendation_mode": "advisor",
            "max_attempts": 2,
            "execution_mode": "local",
            "env": {"PATH": path},
            **({"mode": "auto_patch"} if stage == "t2t" else {}),
        }
    }
    return state


def approve(state, decision="follow_recommendation", candidate_id=None):
    active = state["active_interrupt"]
    request = ResumeReviewRequest(
        session_id=state["session_id"],
        stage=active["stage"],
        gate_id=active["gate_id"],
        review_request_id=active["review_request_id"],
        review_request_hash=active["request_hash"],
        decision=decision,
        candidate_id=candidate_id,
    )
    intent = persist_review_intent(state, request, source="test")
    prepared = prepare_application(state, intent.intent)
    ref = persist_review_application(state, intent=intent, prepared=prepared)
    return {
        **state,
        **apply_application(
            state, load_verified_review_application(state["data_dir"], ref)
        ),
    }


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_feedback_budget_history_and_fresh_graph_resume(tmp_path, stage):
    state = state_for(stage, tmp_path)
    prior = deepcopy(state["resource_refs"]["assembly"])
    model = Model(
        [AIMessage(content=json.dumps(proposal(stage, i))) for i in range(2)]
        + [
            AIMessage(
                content=json.dumps(
                    proposal(
                        stage,
                        action="accept" if stage != "t2t" else "keep_source",
                        selected=0 if stage != "t2t" else None,
                    )
                )
            )
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
        assert blocked["active_interrupt"]["gate_id"] == "finishing_recommendation"
        assert blocked["resource_refs"]["assembly"] == prior
        for index in range(2):
            approved = approve(blocked)
            # Simulate checkpoint JSON plus a different graph/process instance.
            approved = json.loads(json.dumps(approved))
            blocked = build_stage(stage).invoke(approved)
            assert (
                blocked["active_interrupt"]["gate_id"] == "finishing_recommendation"
            ), blocked
            records = history(blocked, stage)
            assert len(records) == index + 1
            assert (
                f"/attempts/{index:03d}/"
                in records[index]["evidence_refs"][0]["path_ref"]
            )
        records = history(blocked, stage)
        assert len({r["candidate_ref"]["path_ref"] for r in records}) == (
            2 if stage != "t2t" else 1
        )
        evidence = json.loads(model.messages[-1][-1]["content"])
        assert evidence["remaining_attempts"] == 0
        assert len(evidence["attempts"]) == 2
        if stage == "base_polish":
            assert evidence["attempts"][0]["evidence"]["independent_qv"] is None
            assert "-w 500" in records[0]["commands"][0]
            assert "-w 600" in records[1]["commands"][0]
        if stage == "t2t":
            assert evidence["source"]["terminal_observations"]["count"] > 0
            assert "reads_telomere_scan_ref" in evidence["attempts"][0]["evidence"]
        final = build_stage(stage).invoke(approve(blocked))
        assert loop(final, stage)["phase"] == "done"
        if stage != "t2t":
            assert (
                final["resource_refs"]["assembly"]["metadata"]["candidate_bundle_ref"]
                == records[0]["candidate_ref"]
            )
        else:
            assert final["resource_refs"]["assembly"]["paths"] == prior["paths"]
        assert len(model.messages) == 3


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_no_execution_keep_source_and_invalid_budget(tmp_path, stage):
    state = state_for(stage, tmp_path)
    state["stage_outputs"][stage]["max_attempts"] = 0
    model = Model([AIMessage(content=json.dumps(proposal(stage)))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
    from app.review.persistence import load_review_request

    request = load_review_request(
        blocked["data_dir"],
        blocked["active_interrupt"]["request_ref"],
        session_id=blocked["session_id"],
    )
    assert "follow_recommendation" not in request["payload"]["decisions_allowed"]
    final = build_stage(stage).invoke(approve(blocked, "keep_prior_assembly"))
    assert not history(final, stage)
    assert (
        final["resource_refs"]["assembly"]["paths"]
        == state["resource_refs"]["assembly"]["paths"]
    )


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_duplicate_invalid_selection_and_stage_shape(stage):
    parsed = FinishingRecommendation.model_validate(proposal(stage))
    with pytest.raises(ValueError, match="repeats"):
        validate_proposal(
            stage, parsed, [{"parameters": parsed.parameters.model_dump()}], 2
        )
    with pytest.raises(ValueError, match="unavailable"):
        validate_proposal(
            stage,
            FinishingRecommendation.model_validate(
                proposal(stage, action="accept", selected=0)
            ),
            [],
            2,
        )
    other = "t2t" if stage != "t2t" else "gapcloser"
    with pytest.raises(ValueError, match="shape"):
        validate_proposal(
            stage, FinishingRecommendation.model_validate(proposal(other)), [], 2
        )


@pytest.mark.parametrize("method", ["prompt_json", "json_schema", "function_calling"])
def test_structured_advisor_boundary(method):
    message = AIMessage(content=json.dumps(proposal("gapcloser")))
    if method == "function_calling":
        message = AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "1",
                        "type": "function",
                        "function": {
                            "name": "submit_recommendation",
                            "arguments": json.dumps(proposal("gapcloser")),
                        },
                    }
                ]
            },
        )
    result = FinishingRecommender("gapcloser", method).invoke(
        [], model_factory=lambda: Model([message])
    )
    assert result.valid


@pytest.mark.parametrize(
    "decision", ["follow_recommendation", "keep_prior_assembly", "accept_candidate"]
)
def test_t2t_supported_patch_adoption_uses_selected_attempt_evidence(
    tmp_path, decision
):
    from tests.test_t2t import T2TStageTests

    state = state_for("t2t", tmp_path)
    T2TStageTests()._fake_minimap2(str(tmp_path))
    model = Model(
        [AIMessage(content=json.dumps(proposal("t2t", i))) for i in range(2)]
        + [AIMessage(content=json.dumps(proposal("t2t", action="accept", selected=0)))]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage("t2t").invoke(state)
        for _ in range(2):
            blocked = build_stage("t2t").invoke(approve(blocked))
        records = history(blocked, "t2t")
        assert all(r["materialized"] for r in records)
        evidence = json.loads(model.messages[-1][-1]["content"])
        assert (
            evidence["attempts"][0]["evidence"]["telomere_extension_candidates_ref"][
                "counts"
            ]["decision"]["auto_patch"]
            > 0
        )
        assert (
            evidence["attempts"][0]["evidence"]["telomere_extension_candidates_ref"][
                "numeric"
            ]["supporting_read_count"]["mean"]
            == 2
        )
        final = build_stage("t2t").invoke(
            approve(
                blocked,
                decision,
                "attempt_0" if decision == "accept_candidate" else None,
            )
        )
    if decision == "keep_prior_assembly":
        assert (
            final["resource_refs"]["assembly"]["paths"]
            == state["resource_refs"]["assembly"]["paths"]
        )
    else:
        assert (
            final["resource_refs"]["assembly"]["metadata"]["candidate_bundle_ref"]
            == records[0]["candidate_ref"]
        )
        assert "/attempts/000/" in final["stage_outputs"]["t2t"]["bundle_ref"]
    report = json.loads(Path(final["stage_outputs"]["t2t"]["bundle_ref"]).read_text())
    assert report["evidence_phase"] == "final"
    assert (
        report["output_assembly_ref"] == final["resource_refs"]["assembly"]["paths"][0]
    )


@pytest.mark.parametrize(
    "threshold,value,reason",
    [
        ("min_mapq", 61, "mapq_below_threshold"),
        ("min_overlap_bp", 20000, "overlap_below_threshold"),
        ("max_target_terminal_distance_bp", 0, "target_distance_above_threshold"),
    ],
)
def test_t2t_filtered_placements_reach_next_advisor_request(
    tmp_path, threshold, value, reason
):
    from tests.test_t2t import T2TStageTests

    state = state_for("t2t", tmp_path)
    fixture = T2TStageTests()
    if threshold == "max_target_terminal_distance_bp":
        # Two real fixture reads; placement ends one base inside the assembly.
        # Obtain canonical IDs from the fake aligner instead of relying on naming.
        fixture._fake_minimap2(str(tmp_path))
        mm = tmp_path / "bin" / "minimap2"
        mm.write_text(
            mm.read_text().replace(
                "\\t{target_len}\\t{target_len}\\t",
                "\\t{target_len - 1}\\t{target_len}\\t",
            )
        )
    else:
        fixture._fake_minimap2(str(tmp_path))
    first = proposal("t2t")
    first["parameters"][threshold] = value
    model = Model(
        [
            AIMessage(content=json.dumps(first)),
            AIMessage(content=json.dumps(proposal("t2t", action="keep_source"))),
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage("t2t").invoke(state)
        blocked = build_stage("t2t").invoke(approve(blocked))
    assert history(blocked, "t2t")[0]["cursor"]["candidate_count"] == 0
    evidence = json.loads(model.messages[-1][-1]["content"])
    observed = evidence["attempts"][0]["evidence"]["alignment_observations_ref"]
    assert observed["numeric"]["mapq"]["min"] == 60
    assert observed["numeric_distributions"]["mapq"]["60"] == 4
    assert observed["numeric"]["overlap_bp"]["max"] > 0
    assert observed["counts"]["rejection_reasons"][reason] >= 2
    assert len(observed["examples"]) == 4
    assert all(row["execution_ref"] for row in observed["examples"])


def test_support_counts_include_all_rows_beyond_example_limit(tmp_path):
    from app.stages.finishing.advisor import table_summary

    table = tmp_path / "candidates.tsv"
    table.write_text("supporting_reads\tdecision\n" + "r1,r2,r2\trejected\n" * 105)
    summary = table_summary(table)
    assert summary["examples_truncated"]
    assert len(summary["examples"]) == 100
    assert summary["numeric"]["supporting_read_count"] == {
        "min": 2,
        "max": 2,
        "sum": 210,
        "count": 105,
        "mean": 2,
    }
    assert summary["numeric_distributions"]["supporting_read_count"]["1..9"] == 105


@pytest.mark.parametrize("mode", ["manual_review", "auto_patch", "propose_only"])
def test_t2t_explicit_baseline_never_calls_model_and_freezes_catalog(tmp_path, mode):
    from app.stages.t2t.builder import build_stage as build_t2t
    from app.stages.t2t.parameters.execution import load_t2t_frozen_execution_inputs
    from app.storage.parameter_evidence import load_approved_parameter_chain_v3
    from tests.test_t2t import T2TStageTests

    state = state_for("t2t", tmp_path)
    state["stage_outputs"]["t2t"].update(
        recommendation_mode="deterministic_baseline", mode=mode
    )
    state["workflow_steps"][state["workflow_step_index"]]["selected_method_names"] = [
        "t2t.alignment.minimap2"
    ]
    T2TStageTests()._fake_minimap2(str(tmp_path))
    with (
        patch(
            "app.llm.recommender.build_chat_model",
            side_effect=AssertionError("model disabled"),
        ),
    ):
        final = build_t2t().invoke(state)
    cursor = final["stage_outputs"]["t2t"]
    assert cursor["status"] not in {"failed", "failed_terminal"}, cursor
    assert (
        not final.get("active_interrupt")
        or final["active_interrupt"]["gate_id"] == "t2t_review"
    )
    # Stage may advance; validate its immutable chain in the originating occurrence.
    bound = {
        **final,
        "workflow_step_index": state["workflow_step_index"],
        "current_stage": "t2t",
    }
    chain = load_approved_parameter_chain_v3(
        bound,
        attempt=cursor["parameter_attempts"][0],
        expected_stage="t2t",
        expected_method_id="t2t.alignment.minimap2",
    )
    assert chain.approved_l2 == {}
    assert chain.chain.manifest.summary["model_invoked"] is False
    assert chain.approval_decision["decision"]["decided_by"] == "auto_rule"
    frozen = load_t2t_frozen_execution_inputs(bound)
    assert frozen.mode == mode
    assert frozen.thresholds.min_mapq == 20


def test_t2t_baseline_without_motif_fails_without_model(tmp_path):
    from app.schemas.run_config import RunConfig
    from app.stages.t2t.builder import build_stage as build_t2t

    state = state_for("t2t", tmp_path)
    state["shared_parameters"] = {}
    state["stage_outputs"]["t2t"]["recommendation_mode"] = "deterministic_baseline"
    with patch(
        "app.llm.recommender.build_chat_model",
        side_effect=AssertionError("model disabled"),
    ):
        final = build_t2t().invoke(state)
    assert (
        "requires a supplied telomere motif" in final["stage_outputs"]["t2t"]["reason"]
    )
    with pytest.raises(ValueError, match="deterministic_baseline requires"):
        RunConfig.model_validate(
            {
                "requested_stages": [{"stage": "t2t", "allow_local_execution": True}],
                "t2t": {"recommendation_mode": "deterministic_baseline"},
            }
        )


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_approval_tampering_and_response_recovery(tmp_path, stage):
    from app.stages.finishing.advisor import recommend
    from app.stages.finishing.review import approved_proposal

    state = state_for(stage, tmp_path)
    model = Model([AIMessage(content=json.dumps(proposal(stage)))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
        repeated = recommend(blocked, stage)
    assert len(model.messages) == 1
    assert (
        repeated["stage_outputs"][stage]["finishing"]["recommendation_ref"]
        == loop(blocked, stage)["recommendation_ref"]
    )
    approved = approve(blocked)
    approved_proposal(approved, stage)
    changed = deepcopy(approved)
    changed["stage_outputs"][stage]["finishing"]["attempt"] = 1
    with pytest.raises(ValueError, match="history"):
        approved_proposal(changed, stage)
    changed = deepcopy(approved)
    first = next(iter(changed["stage_outputs"][stage]["finishing"]["parameters"]))
    changed["stage_outputs"][stage]["finishing"]["parameters"][first] = 999
    with pytest.raises(ValueError, match="approval"):
        approved_proposal(changed, stage)
    # Mutable budget copies cannot enlarge the frozen initial settings budget.
    changed = deepcopy(approved)
    changed["stage_outputs"][stage]["max_attempts"] = 10
    from app.stages.finishing.advisor import maximum_attempts

    assert maximum_attempts(changed, stage) == 2


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_stop_before_execution_settles_roster(tmp_path, stage):
    from app.control.occurrence_settlement import settle_workflow_step_outcome
    from app.storage.ledger import Ledger

    state = state_for(stage, tmp_path)
    model = Model([AIMessage(content=json.dumps(proposal(stage)))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
    final = build_stage(stage).invoke(approve(blocked, "stop_human_review"))
    settle_workflow_step_outcome(final, data_dir=final["data_dir"])
    dispositions = [
        r
        for r in Ledger(final["data_dir"]).read_event_rows(final["session_id"])
        if r["event_type"] == "method_disposition_recorded"
    ]
    assert len(dispositions) == 2
    assert final["workflow_step_outcome"]["outcome"] == "failed"


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
def test_tool_failure_settles_roster_without_delivery(tmp_path, stage):
    from app.control.occurrence_settlement import settle_workflow_step_outcome
    from app.storage.ledger import Ledger

    state = state_for(stage, tmp_path)
    model = Model([AIMessage(content=json.dumps(proposal(stage)))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage(stage).invoke(state)
    tool = (
        tmp_path
        / "bin"
        / {
            "gapcloser": "tgsgapcloser",
            "base_polish": "racon",
            "t2t": "minimap2",
        }[stage]
    )
    tool.write_text("#!/bin/sh\nexit 7\n")
    final = build_stage(stage).invoke(approve(blocked))
    settle_workflow_step_outcome(final, data_dir=final["data_dir"])
    rows = Ledger(final["data_dir"]).read_event_rows(final["session_id"])
    dispositions = [r for r in rows if r["event_type"] == "method_disposition_recorded"]
    assert len(dispositions) == 2
    assert {
        r["summary"]["method_name"]: r["summary"]["disposition"] for r in dispositions
    } == {
        {
            "gapcloser": "gapcloser.tgs_gapcloser",
            "base_polish": "base_polish.racon",
            "t2t": "t2t.alignment.minimap2",
        }[stage]: "failed",
        f"{stage}.eval.quast": "completed",
    }
    assert final["workflow_step_outcome"]["outcome"] == "failed"
    assert final["resource_refs"]["assembly"] == state["resource_refs"]["assembly"]
    assert not history(final, stage)


@pytest.mark.parametrize("fail_evaluation", [False, True])
def test_t2t_without_telomeric_reads_records_alignment_skip(tmp_path, fail_evaluation):
    from app.control.occurrence_settlement import settle_workflow_step_outcome
    from app.storage.ledger import Ledger

    state = state_for("t2t", tmp_path)
    # A higher supported motif threshold gives no matching reads, without
    # mutating any registered biological input after publication.
    first = proposal("t2t")
    first["parameters"]["min_motif_copies"] = 100
    model = Model(
        [
            AIMessage(content=json.dumps(first)),
            AIMessage(content=json.dumps(proposal("t2t", action="keep_source"))),
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage("t2t").invoke(state)
        if fail_evaluation:
            (tmp_path / "bin" / "quast.py").write_text("#!/bin/sh\nexit 7\n")
        blocked = build_stage("t2t").invoke(approve(blocked))
        final = (
            blocked if fail_evaluation else build_stage("t2t").invoke(approve(blocked))
        )
    settle_workflow_step_outcome(final, data_dir=final["data_dir"])
    rows = Ledger(final["data_dir"]).read_event_rows(final["session_id"])
    assert not any(
        r["event_type"] == "attempt_started"
        and r["summary"].get("method_name") == "t2t.alignment.minimap2"
        for r in rows
    )
    assert any(
        r["event_type"] == "method_disposition_recorded"
        and r["summary"].get("method_name") == "t2t.alignment.minimap2"
        and r["summary"].get("disposition") == "skipped"
        for r in rows
    )
    if fail_evaluation:
        assert final["workflow_step_outcome"]["outcome"] == "failed"
        assert any(
            r["event_type"] == "method_disposition_recorded"
            and r["summary"].get("method_name") == "t2t.eval.quast"
            and r["summary"].get("disposition") == "failed"
            for r in rows
        )


@pytest.mark.parametrize("execute_polish", [True, False])
def test_finishing_handoff_and_final_review(tmp_path, execute_polish):
    from app.control.occurrence_settlement import settle_workflow_step_outcome
    from app.diagnostics.route_acceptance import final_comparison
    from app.schemas.artifacts import ArtifactRef
    from app.stages.assembly_review.builder import build_stage as build_review
    from app.storage.artifacts import read_artifact_records
    from app.storage.paths import SessionPaths
    from tests.test_assembly_review import AssemblyReviewStageTests

    state = state_for("gapcloser", tmp_path)
    biological = {
        "gapcloser": "gapcloser.tgs_gapcloser",
        "t2t": "t2t.alignment.minimap2",
        "base_polish": "base_polish.racon",
    }
    template = deepcopy(state["workflow_steps"][2])
    review_step = deepcopy(state["workflow_steps"][3])
    state["workflow_steps"] = (
        state["workflow_steps"][:2]
        + [
            {
                **template,
                "step_id": f"step-{i:03d}-{stage}",
                "stage": stage,
                "selected_method_names": [biological[stage], f"{stage}.eval.quast"],
            }
            for i, stage in enumerate(biological, 2)
        ]
        + [
            {
                **review_step,
                "step_id": "step-005-assembly_review",
                "allow_local_execution": True,
                "review_target": {
                    **review_step["review_target"],
                    "producer_step_id": "step-004-base_polish",
                },
            }
        ]
    )
    path = state["stage_outputs"]["gapcloser"]["env"]["PATH"]
    for stage in ("t2t", "base_polish"):
        state["stage_outputs"][stage] = {
            "recommendation_mode": "advisor",
            "max_attempts": 2,
            "execution_mode": "local",
            "env": {"PATH": path},
            **({"mode": "auto_patch"} if stage == "t2t" else {}),
        }
    state["shared_parameters"] = {"species_facts": {"telomere_motif": "TTAGGG"}}
    mm = tmp_path / "bin" / "minimap2"
    mm.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then echo 2.28; exit; fi\n'
        "printf 'member_0_read_0\\t10\\t0\\t10\\t+\\tchr1\\t10\\t0\\t10"
        "\\t10\\t10\\t60\\ttp:A:P\\tcg:Z:10=\\n'\n"
    )
    mm.chmod(0o755)
    racon = tmp_path / "bin" / "racon"
    racon.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then echo 1.5.0; exit; fi\n'
        "printf '>chr1\\nAAAATTCCCC\\n'\n"
    )
    racon.chmod(0o755)
    messages = []
    for stage in biological:
        if stage == "base_polish" and not execute_polish:
            messages.append(proposal(stage, action="keep_source"))
            continue
        messages.append(proposal(stage))
        if stage == "base_polish":
            messages.append(proposal(stage, 1))
        messages.append(
            proposal(
                stage,
                action="keep_source" if stage == "t2t" else "accept",
                selected=None if stage == "t2t" else 0,
            )
        )
    model = Model([AIMessage(content=json.dumps(p)) for p in messages])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        for stage in biological:
            state["current_stage"] = stage
            state["entered_workflow_step_index"] = state["workflow_step_index"]
            prior = state["resource_refs"]["assembly"]["metadata"][
                "candidate_bundle_ref"
            ]
            state = build_stage(stage).invoke(state)
            assert loop(state, stage)["source_ref"] == prior
            for _ in range(
                (2 if execute_polish else 0) if stage == "base_polish" else 1
            ):
                state = build_stage(stage).invoke(approve(state))
            if stage == "base_polish":
                records = history(state, stage)
            state = build_stage(stage).invoke(approve(state))
            settle_workflow_step_outcome(state, data_dir=state["data_dir"])
            # The next stage resolves the actual delivery, without path injection.
            assert (
                state["resource_refs"]["assembly"]["metadata"]["candidate_bundle_ref"]
                == loop(state, stage)["candidate_ref"]
            )
    fixture = AssemblyReviewStageTests()
    fixture._fake_quast_bin(str(tmp_path))
    fixture._fake_busco_bin(str(tmp_path))
    from tests.busco_fixture import ensure_busco_lineage, isolated_frozen_busco_resolver

    lineage = ensure_busco_lineage(tmp_path)
    state["current_stage"] = "assembly_review"
    state["entered_workflow_step_index"] = state["workflow_step_index"]
    state["stage_outputs"]["assembly_review"] = {
        "execution_mode": "local",
        "env": {"PATH": path},
        "related_species_reference_dir": str(fixture._test_reference_dir(state)),
        "quast_parameters": {"threads": 1},
        "busco_parameters": {
            "threads": 1,
            "lineage_path": str(lineage.resolve()),
            "lineage_name": lineage.name,
            "lineage_version": "odb10",
            "lineage_source": "test_fixture",
            "mode": "genome",
        },
    }
    if not execute_polish:
        from app.stages.assembly_review.nodes import resolve_candidates

        invalid = deepcopy(state)
        invalid["resource_refs"]["assembly"]["metadata"].pop("finishing_decision_ref")
        rejected = resolve_candidates(invalid)["stage_outputs"]["assembly_review"]
        assert rejected["status"] == "failed"
        invalid = deepcopy(state)
        invalid["resource_refs"]["assembly"]["metadata"]["finishing_history_refs"] = [
            "unverified attempt"
        ]
        rejected = resolve_candidates(invalid)["stage_outputs"]["assembly_review"]
        assert rejected["status"] == "failed"
    with (
        patch(
            "app.stages.assembly_review.method_application.busco."
            "load_frozen_dependency_resolver",
            return_value=isolated_frozen_busco_resolver(
                state["stage_outputs"]["assembly_review"]["busco_parameters"]
            ),
        ),
        patch(
            "app.stages.assembly_review.statistics._related_species_reference_dir",
            return_value=fixture._test_reference_dir(state),
        ),
    ):
        evaluated = build_review().invoke(state)
    assert evaluated["active_interrupt"], evaluated["stage_outputs"]["assembly_review"]
    paths = SessionPaths.from_run_dir(state["data_dir"])
    comparison = final_comparison(
        paths,
        read_artifact_records(paths.run_dir, "s1"),
        "step-005-assembly_review",
        ArtifactRef.model_validate(evaluated["active_interrupt"]["request_ref"]),
    )
    if execute_polish:
        assert (
            comparison["polishing_comparison"]["report_ref"]
            == records[0]["cursor"]["comparison_ref"]
        )
        assert len(comparison["candidates"]) == 2
    else:
        assert comparison["comparison_status"] == "not_executed_source_retained"
        assert len(comparison["candidates"]) == 1


@pytest.mark.parametrize("stage", ["gapcloser", "base_polish", "t2t"])
@pytest.mark.parametrize("malformed_report", [False, True])
def test_frozen_parent_sqlite_cli_resume_does_not_repeat_tools(
    tmp_path, stage, malformed_report
):
    import argparse
    import shlex

    from app.control.lifecycle import inspect_session
    from app.control.workflow_plan import workflow_steps_from_run_config
    from app.entrypoints.run import _run_resume, _stage_seeds_from_run_config
    from app.graph.builder import build_graph
    from app.schemas.state import initial_state
    from app.storage.frozen_inputs import freeze_session_inputs
    from app.storage.ledger import Ledger
    from app.storage.paths import SessionPaths

    fixture = state_for(stage, tmp_path)
    # Read-only fixture tooling; the frozen parent independently imports these files.
    raw_source = tmp_path / ("final.fa" if stage == "t2t" else "source.fa")
    raw_reads = tmp_path / ("reads.fq" if stage == "t2t" else "reads with space.fq")
    conda = tmp_path / "conda"
    conda.write_text(
        "#!/bin/sh\nwhile [ $# -gt 0 ]; do\n"
        'case "$1" in run|--live-stream|--no-capture-output) shift;; '
        "-p|-n) shift 2;; *) break;; esac\ndone\n"
        + f'export PATH={shlex.quote(str(tmp_path / "bin"))}:"$PATH"\nexec "$@"\n'
    )
    conda.chmod(0o755)
    biological = {
        "gapcloser": "gapcloser.tgs_gapcloser",
        "base_polish": "base_polish.racon",
        "t2t": "t2t.alignment.minimap2",
    }[stage]
    tools = {
        "gapcloser": ["tgsgapcloser"],
        "base_polish": ["minimap2", "racon"],
        "t2t": ["minimap2"],
    }[stage]
    envs = tmp_path / "envs.yaml"
    envs.write_text(
        json.dumps(
            {
                "default": {"conda_bin": str(conda), "fallback_prefix": str(tmp_path)},
                "methods": {
                    biological: {
                        "conda_prefix": str(tmp_path),
                        "required_tools": tools,
                    },
                    f"{stage}.eval.quast": {
                        "conda_prefix": str(tmp_path),
                        "required_tools": ["quast.py"],
                    },
                },
            }
        )
    )
    paths = SessionPaths.from_run_dir(tmp_path / "parent" / "runs" / "sqlite-advisor")
    frozen = freeze_session_inputs(
        paths,
        manifest_draft={
            "session_id": paths.session_id,
            "assets": [
                {
                    "asset_type": "assembly_fasta_registered",
                    "paths": [str(raw_source)],
                    "format": "fasta",
                    "compression": "none",
                },
                {
                    "asset_type": "raw_long_reads",
                    "paths": [str(raw_reads)],
                    "format": "fastq",
                    "platform": "ont" if stage == "t2t" else "pacbio_hifi",
                    "compression": "none",
                },
            ],
        },
        run_config_draft={
            "requested_stages": [{"stage": stage, "allow_local_execution": True}],
            stage: {
                "recommendation_mode": "advisor",
                "max_attempts": 1,
                **({"mode": "auto_patch"} if stage == "t2t" else {"threads": 1}),
            },
            "shared_parameters": {"species_facts": {"telomere_motif": "TTAGGG"}},
            "review_policy": {"max_blocked_seconds": -1},
        },
        config_path=envs,
        probe_environments=False,
    )
    Ledger(paths.run_dir).create_session(
        paths.session_id, paths.to_ref(paths.input_manifest_path)
    )
    shared = frozen.run_config.shared_parameters.model_dump(
        mode="json", exclude_none=True
    )
    initial = initial_state(
        session_id=paths.session_id,
        input_manifest_ref=paths.to_ref(paths.input_manifest_path),
        data_dir=str(paths.run_dir),
        workflow_steps=workflow_steps_from_run_config(frozen.run_config),
        stage_seeds=_stage_seeds_from_run_config(
            frozen.run_config, shared_parameters=shared
        ),
    )
    initial["shared_parameters"] = shared
    graph = build_graph(
        backend="sqlite", data_dir=str(paths.run_dir), verified_inputs=frozen
    )
    model = Model(
        [
            AIMessage(content=json.dumps(proposal(stage))),
            AIMessage(content=json.dumps(proposal(stage, action="keep_source"))),
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        try:
            blocked = graph.invoke(
                initial, {"configurable": {"thread_id": paths.session_id}}
            )
        finally:
            graph.checkpointer.conn.close()
        assert blocked["active_interrupt"]["gate_id"] == "finishing_recommendation"

        def resume(state):
            gate = state["active_interrupt"]
            decision = tmp_path / "decision.json"
            decision.write_text(
                json.dumps(
                    {
                        "session_id": paths.session_id,
                        "stage": stage,
                        "gate_id": gate["gate_id"],
                        "review_request_id": gate["review_request_id"],
                        "review_request_hash": gate["request_hash"],
                        "decision": "follow_recommendation",
                    }
                )
            )
            return _run_resume(
                argparse.Namespace(
                    session_id=paths.session_id,
                    decision_file=str(decision),
                    data_dir=str(paths.run_dir),
                    checkpointer="sqlite",
                )
            )

        if malformed_report:
            tool = tmp_path / "bin" / "quast.py"
            tool.write_text(
                tool.read_text().replace(
                    "labels=args[args.index('--labels')+1].split(',')",
                    "labels=['source']",
                )
            )
        assert resume(blocked) == (1 if malformed_report else 0)
        graph = build_graph(
            backend="sqlite", data_dir=str(paths.run_dir), verified_inputs=frozen
        )
        try:
            blocked = graph.get_state(
                {"configurable": {"thread_id": paths.session_id}}
            ).values
        finally:
            graph.checkpointer.conn.close()
        assert len(history(blocked, stage)) == 1
        if malformed_report:
            assert blocked["supervision_state"] == "failed_terminal"
            assert "collect failed" in blocked["stage_outputs"][stage]["reason"]
            outcomes = {
                event["method_name"]: event["disposition"]
                for event in Ledger(paths.run_dir).read_events(paths.session_id)
                if event["event_type"] == "method_disposition_recorded"
            }
            assert outcomes == {
                biological: "completed",
                f"{stage}.eval.quast": "failed",
            }
            assert len(model.messages) == 1
            return
        before = [
            r["subject_id"]
            for r in Ledger(paths.run_dir).read_event_rows(paths.session_id)
            if r["event_type"] == "attempt_started"
        ]
        assert resume(blocked) == 0
    after = [
        r["subject_id"]
        for r in Ledger(paths.run_dir).read_event_rows(paths.session_id)
        if r["event_type"] == "attempt_started"
    ]
    assert before == after
    assert (
        inspect_session(paths.session_id, paths.run_dir)["supervision_state"]
        == "completed"
    )
    assert len(model.messages) == 2
    assert fixture["current_stage"] == stage


def test_t2t_baseline_graph_rejects_advisor_before_motif_resolution(tmp_path):
    from app.stages.t2t.builder import build_baseline_stage

    state = state_for("t2t", tmp_path)
    state["shared_parameters"]["species_facts"].pop("telomere_motif", None)
    with patch("app.llm.parameters.t2t.motif.build_chat_model") as model:
        failed = build_baseline_stage().invoke(state)
    model.assert_not_called()
    assert failed["supervision_state"] == "failed_terminal"
    assert (
        "single-pass execution requires deterministic_baseline" in failed["last_error"]
    )


def test_reentry_restores_exact_request_before_rendering(tmp_path):
    from app.stages.finishing.advisor import recommend

    state = state_for("gapcloser", tmp_path)
    model = Model([AIMessage(content=json.dumps(proposal("gapcloser")))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage("gapcloser").invoke(state)
    expected = loop(blocked, "gapcloser")["recommendation_ref"]
    with (
        patch.object(
            FinishingRecommender,
            "messages",
            side_effect=AssertionError("must restore exact request"),
        ),
        patch.object(
            FinishingRecommender,
            "invoke",
            side_effect=AssertionError("must restore response"),
        ),
    ):
        replay = recommend(blocked, "gapcloser")
    assert (
        replay["stage_outputs"]["gapcloser"]["finishing"]["recommendation_ref"]
        == expected
    )
