"""Exercise the structured boundary and real durable dedup graph with fake tools."""

import json
import os
from copy import deepcopy
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from app.control.lifecycle import normalize_resume_review_request
from app.llm.dedup import PurgeRecommendation, PurgeRecommender
from app.review.application import (
    load_verified_review_application,
    persist_review_application,
    persist_review_intent,
)
from app.review.base import ResumeReviewRequest
from app.stages.assembly_dedup.advisor import (
    history,
    load_json,
    recommend_dedup,
    validate_proposal,
)
from app.stages.assembly_dedup.builder import build_stage
from app.stages.assembly_dedup.review import apply_application, prepare_application
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests import test_assembly_dedup as fixtures


def proposal(action="run", preset="asm5", selected=None):
    return {
        "action": action,
        "parameters": {
            "coverage_mode": "self_alignment",
            "self_alignment_preset": preset,
            "end_only": True,
        }
        if action == "run"
        else None,
        "selected_attempt": selected,
        "reason": "Compare complete QUAST reports.",
        "evidence_gaps": ["BUSCO unavailable"],
    }


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.bindings = []
        self.messages = []

    def bind(self, **kwargs):
        self.bindings.append(kwargs)
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        return next(self.responses)


@pytest.mark.parametrize("method", ["prompt_json", "json_schema", "function_calling"])
def test_structured_modes_validate_and_keep_raw(method):
    raw = json.dumps(proposal())
    message = AIMessage(content=raw)
    if method == "function_calling":
        message = AIMessage(
            content="",
            additional_kwargs={
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "submit_recommendation",
                            "arguments": raw,
                        },
                    }
                ]
            },
        )
    model = Model([message])
    result = PurgeRecommender(method).invoke([], model_factory=lambda: model)
    assert result.valid and result.parsed.parameters.end_only
    assert result.raw_response == raw
    assert result.raw_message["content"] == message.content
    if method == "function_calling":
        assert (
            model.bindings[0]["tool_choice"]["function"]["name"]
            == "submit_recommendation"
        )
    bad = PurgeRecommender("prompt_json").invoke(
        [],
        model_factory=lambda: Model(
            [
                AIMessage(content=json.dumps({**proposal(), "shell": "rm -rf /"})),
            ]
        ),
    )
    assert not bad.valid and bad.raw_response


def test_budget_and_duplicate_config_stop_new_attempts():
    parsed = PurgeRecommendation.model_validate(proposal())
    with pytest.raises(ValueError, match="budget"):
        validate_proposal(parsed, [], 0)
    with pytest.raises(ValueError, match="repeats"):
        validate_proposal(
            parsed, [{"attempt": 0, "parameters": parsed.parameters.model_dump()}], 2
        )


def _state(tmp_path):
    helper = fixtures.AssemblyDedupStageTests()
    state = helper._state(str(tmp_path))
    source = tmp_path / "source.fasta"
    source.write_text(">c1\n" + "ACGT" * 300 + "\n")
    bundle, primary = helper._publish_candidate(
        state,
        candidate_id="source",
        method_name="assembly.candidate.hifiasm",
        draft=source,
        member_id="hifi-source",
        platform="pacbio_hifi",
    )
    path = SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(primary["path_ref"])
    helper._set_assembly_resource(
        state,
        primary_path=path,
        candidate_bundle_ref=bundle,
        primary_output_ref=primary,
        source_stage="assembly",
        workflow_step_id="step-001-assembly",
    )
    bins = tmp_path / "bin"
    bins.mkdir()
    scripts = {
        "minimap2": "printf 'alignment\\n'\n",
        "split_fa": 'cat "$1"\n',
        "purge_dups": ": # Empty BED is a valid no-duplication result.\n",
        "get_seqs": """prefix=''
last=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = -p ]; then shift; prefix="$1"; fi
  last="$1"
  shift
done
cat "$last" > "${prefix}.purged.fa"
: > "${prefix}.hap.fa"
""",
        "quast.py": """while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift; out="$1";;
    --labels) shift; labels="$1";;
  esac
  shift
done
mkdir -p "$out"
report="$out/report.tsv"
printf 'Assembly\\t%s\\nN50\\t1200\\nTotal length\\t1200\\n' "$labels" > "$report"
""",
    }
    for name, script in scripts.items():
        path = bins / name
        path.write_text("#!/bin/sh\nset -eu\n" + script)
        path.chmod(0o755)
    state["stage_outputs"]["assembly_dedup"] = {
        "execution_mode": "local",
        "automatic_purge_approved": True,
        "recommendation_mode": "advisor",
        "recommendation_output_method": "json_schema",
        "max_attempts": 2,
        "review_policy": {"max_blocked_seconds": 0},
        "env": {"PATH": f"{bins}{os.pathsep}{os.environ['PATH']}"},
    }
    return state, source


def _approve(state, *, automatic=False):
    active = state["active_interrupt"]
    request = ResumeReviewRequest(
        session_id=state["session_id"],
        stage="assembly_dedup",
        gate_id="dedup_recommendation",
        review_request_id=active["review_request_id"],
        review_request_hash=active["request_hash"],
        decision="follow_recommendation",
        decided_by="auto_rule" if automatic else "human",
        metadata={
            "source": "foreground_countdown",
            "configured_seconds": 0,
            "elapsed_seconds": 0,
        }
        if automatic
        else {},
        rationale_artifact_ref=active["request_ref"] if automatic else None,
    )
    kwargs = (
        {"blocked_timeout_seconds": 0, "decision_source": "foreground_countdown"}
        if automatic
        else {}
    )
    canonical = normalize_resume_review_request(state, request, **kwargs)
    intent = persist_review_intent(state, canonical, source="test")
    prepared = prepare_application(state, intent.intent)
    ref = persist_review_application(state, intent=intent, prepared=prepared)
    verified = load_verified_review_application(state["data_dir"], ref)
    return {**state, **apply_application(state, verified)}


def test_two_rounds_countdown_replay_and_select_earlier_candidate(tmp_path):
    state, source = _state(tmp_path)
    raw_source = source.read_bytes()
    model = Model(
        [
            AIMessage(content=json.dumps(item))
            for item in [
                proposal(),
                proposal(preset="asm10"),
                proposal("accept", selected=0),
            ]
        ]
    )
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        graph = build_stage()
        first = graph.invoke(state)
        assert first["supervision_state"] == "blocked_human"
        assert len(model.messages) == 1
        # Recover a model response after persistence but before checkpoint commit.
        replay = deepcopy(first)
        replay["stage_outputs"]["assembly_dedup"]["status"] = "dedup_advice_ready"
        recommend_dedup(replay)
        assert len(model.messages) == 1
        second = graph.invoke(_approve(first, automatic=True))
        assert len(history(second)) == 1
        third = graph.invoke(_approve(second))
        assert len(history(third)) == 2
        assert len(model.messages) == 3
        evidence = json.loads(model.messages[-1][1]["content"])
        assert evidence["remaining_attempts"] == 0
        assert "N50" in evidence["quast_report_tsv"]
        assert "purge_dups " in evidence["attempts"][0]["command"]
        assert "pbcstat " not in evidence["attempts"][0]["command"]
        final = graph.invoke(_approve(third, automatic=True))
    cursor = final["stage_outputs"]["assembly_dedup"]
    assert cursor["status"] == "completed"
    assert cursor["adopted_attempt"] == 0
    assert "/attempts/000/" in cursor["purged_assembly_ref"]
    assert source.read_bytes() == raw_source
    assert len(cursor["history_refs"]) == 2
    assert load_json(final, cursor["history_refs"][0])["parameters"]["end_only"]
    dispositions = [
        event
        for event in Ledger(state["data_dir"]).read_events("s1")
        if event["event_type"] == "method_disposition_recorded"
    ]
    assert len(dispositions) == 2


def test_countdown_cannot_change_approved_settings_or_bypass_budget(tmp_path):
    from app.stages.assembly_dedup.nodes import freeze_purge_dups_attempt
    from app.stages.assembly_dedup.review import validate_approved_attempt

    state, _ = _state(tmp_path)
    model = Model([AIMessage(content=json.dumps(proposal()))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = build_stage().invoke(state)
    approved = _approve(blocked, automatic=True)
    validate_approved_attempt(approved)
    changed = deepcopy(approved)
    changed["stage_outputs"]["assembly_dedup"]["get_seqs_exclude_haplotigs"] = False
    refused = freeze_purge_dups_attempt(changed)["stage_outputs"]["assembly_dedup"]
    assert refused["status"] == "failed"
    assert "approved recommendation" in refused["reason"]
    exhausted = deepcopy(approved)
    exhausted["stage_outputs"]["assembly_dedup"]["max_attempts"] = 0
    with pytest.raises(ValueError, match="budget"):
        validate_approved_attempt(exhausted)


def test_disabled_execution_does_not_invoke_quast_or_model(tmp_path):
    state, _ = _state(tmp_path)
    state["stage_outputs"]["assembly_dedup"]["automatic_purge_approved"] = False
    with patch("app.llm.recommender.build_chat_model") as model:
        result = build_stage().invoke(state)
    model.assert_not_called()
    assert result["stage_outputs"]["assembly_dedup"]["purge_dups_status"] == "skipped"
    assert result["workflow_step_outcome"]["deliveries"][0]["delivery_mode"] == "reused"


def test_invalid_model_response_requires_human_result_selection(tmp_path):
    from app.control.lifecycle import ResumeValidationError
    from app.stages.assembly_dedup.review import normalize_timeout_request

    state, _ = _state(tmp_path)
    model = Model([AIMessage(content="not JSON")])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        result = build_stage().invoke(state)
    active = result["active_interrupt"]
    record = load_json(result, active["request_ref"])
    assert "follow_recommendation" not in record["payload"]["decisions_allowed"]
    assert "keep_prior_assembly" in record["payload"]["decisions_allowed"]
    request = ResumeReviewRequest(
        session_id="s1",
        stage="assembly_dedup",
        gate_id="dedup_recommendation",
        decision="keep_prior_assembly",
        decided_by="auto_rule",
        rationale_artifact_ref=active["request_ref"],
        metadata={
            "source": "foreground_countdown",
            "configured_seconds": 0,
            "elapsed_seconds": 0,
        },
    )
    with pytest.raises(ResumeValidationError, match="expired countdown"):
        normalize_timeout_request(
            result, request, {"configured_seconds": 0, "expired": True}
        )


def test_advisor_configuration_freezes_quast_roster_and_existing_wait_policy():
    from app.control.workflow_plan import workflow_steps_from_run_config
    from app.entrypoints.run import _stage_seeds_from_run_config
    from app.execution.run_readiness import effective_method_closure, run_requires_llm
    from app.schemas.run_config import RunConfig

    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "assembly_dedup", "allow_local_execution": True}
            ],
            "assembly_dedup": {
                "automatic_purge_approved": True,
                "recommendation_mode": "advisor",
                "max_attempts": 4,
            },
            "review_policy": {
                "stages": {"assembly_dedup": {"max_blocked_seconds": -1}}
            },
        }
    )
    assert effective_method_closure(config)["assembly_dedup"] == (
        "assembly_dedup.purge_dups",
        "assembly_dedup.eval.quast",
    )
    steps = workflow_steps_from_run_config(config)
    assert steps[-1]["selected_method_names"] == [
        "assembly_dedup.purge_dups",
        "assembly_dedup.eval.quast",
    ]
    seeds = _stage_seeds_from_run_config(config, shared_parameters=None)
    assert seeds["assembly_dedup"]["review_policy"]["max_blocked_seconds"] == -1
    assert seeds["assembly_dedup"]["max_attempts"] == 4
    assert run_requires_llm(config)
