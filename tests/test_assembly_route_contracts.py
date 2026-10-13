"""Exercise route contracts from recommendation through durable approval."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.control.inspection import inspect_state
from app.control.resume import foreground_automatic_decision_label
from app.domains.assembly.selection import (
    ROUTE_TO_READ_TYPE,
    default_primary_method,
    route_from_read_type,
)
from app.entrypoints.review_interactive import build_decision_brief
from app.entrypoints.run import _print_inspect_text
from app.knowledge.packs import load_knowledge_pack
from app.schemas.run_config import RunConfig
from app.stages.assembly.nodes import (
    _validated_recommended_method,
    block_assembly_for_human,
    infer_input_profile,
    resolve_extended_inputs,
    select_route,
)
from app.stages.assembly.parameters.context import build_assembly_recommendation_context
from tests.test_phase8_slice1 import InputProfilerTests
from tests.test_route_selection_timeout import (
    _apply_route_review,
    _auto_request,
    _human_request,
    _request_payload,
)

READ_TYPES = [
    ("cyclone", "cycloneseq"),
    ("ont_raw", "ont"),
    ("ont_hq", "ont"),
    ("pacbio_clr", "clr"),
    ("pacbio_hifi", "pacbio_hifi"),
]


@pytest.mark.parametrize("method", ["flye", "nextdenovo"])
def test_zero_qc_quality_reaches_method_advisor_and_countdown(tmp_path, method):
    reads = tmp_path / "clr.fastq"
    reads.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
    state = InputProfilerTests()._state(
        str(tmp_path),
        assets=[
            {
                "asset_id": "clr",
                "asset_type": "raw_long_reads",
                "path": str(reads),
                "platform": "clr",
            }
        ],
    )
    state["workflow_steps"] = [
        state["workflow_steps"][0],
        {
            "step_id": "step-001-read_qc",
            "stage": "read_qc",
            "allow_local_execution": True,
            "selected_method_names": ["read_qc.long_read_stats.seqkit_stats"],
        },
        {
            "step_id": "step-002-assembly",
            "stage": "assembly",
            "allow_local_execution": True,
        },
    ]
    state["workflow_step_index"] = state["entered_workflow_step_index"] = 2
    state["shared_parameters"] = {"read_type": "pacbio_clr"}
    state["stage_outputs"] = {
        "read_qc": {
            "summary": {"num_seqs": 1, "total_bases": 4, "avg_qual": 0.0},
        }
    }
    state.update(resolve_extended_inputs(state))
    state["stage_outputs"]["assembly"]["review_policy"] = {"max_blocked_seconds": 1}
    primary_method = f"assembly.candidate.{method}"

    class Model:
        def invoke(self, messages):
            facts = json.loads(messages[1]["content"])["facts"]
            assert facts["reads"]["mean_quality"]["value"] == 0.0
            return json.dumps(
                {
                    "recommended_route": "clr",
                    "recommended_read_type": "pacbio_clr",
                    "recommended_method": primary_method,
                    "reasoning": "compatible CLR method",
                    "uncertainty_notes": [],
                }
            )

    with patch(
        "app.stages.assembly.decision.build_chat_model", return_value=Model()
    ) as factory:
        state.update(infer_input_profile(state))
    factory.assert_called_once()
    state.update(select_route(state))
    state.update(block_assembly_for_human(state))
    summary = inspect_state(state)
    assert foreground_automatic_decision_label(summary) == "select_clr"
    assert summary["decision_requirements"]["max_blocked_seconds"] == 1
    assert (
        summary["decision_requirements"]["recommended_assembly_selection"][
            "primary_method"
        ]
        == primary_method
    )
    state["active_interrupt"]["blocked_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=2)
    ).isoformat()
    state.update(
        _apply_route_review(
            state,
            _auto_request(state),
            blocked_timeout_seconds=1,
            decision_source="foreground_countdown",
        )
    )
    assert (
        state["stage_outputs"]["assembly"]["assembly_selection"]["primary_method"]
        == primary_method
    )


def test_request_failure_is_visible_without_loading_unverified_artifact(
    tmp_path, capsys
):
    state = InputProfilerTests()._state(str(tmp_path))
    error = (
        "assembly method request recovery failed: "
        "read_qc.summary.avg_qual must be positive"
    )
    state["stage_outputs"] = {
        "assembly": {
            "status": "profile_ready",
            "normalized_read_type": "pacbio_clr",
            "input_profile": {
                "recommended_route": "insufficient_data",
                "recommendation_reasoning": error,
                "uncertainty_notes": [error],
            },
        }
    }
    state.update(select_route(state))
    state.update(block_assembly_for_human(state))
    summary = inspect_state(state)
    assert not summary.get("latest_review_request_artifact", {}).get("file_path")
    assert summary.get("decision_requirements"), summary
    assert summary["decision_requirements"].get("notes"), summary[
        "decision_requirements"
    ]
    brief = build_decision_brief(summary, state["data_dir"])
    assert any(error in note for note in brief["evidence"]["notes"])
    _print_inspect_text(
        {
            **summary,
            "checkpoint_persistence": "sqlite",
            "durable_resume_available": True,
        },
        {},
    )
    assert error in capsys.readouterr().out


@pytest.mark.parametrize("read_type,platform", READ_TYPES)
@pytest.mark.parametrize("policy", ["advisor", "deterministic_baseline"])
def test_input_type_survives_method_choice_countdown_and_parameter_context(
    tmp_path, read_type, platform, policy
):
    route = route_from_read_type(read_type)
    method = default_primary_method(read_type)
    state = InputProfilerTests()._state(str(tmp_path))
    state["shared_parameters"] = {"read_type": read_type}
    state["stage_seeds"] = {"assembly": {"method_route_advisor": policy}}
    cursor = {
        "status": "resolved_extended",
        "target_level": "draft",
        "normalized_read_type": read_type,
        "primary_long_read_platform": platform,
        "assembly_input_member_platforms": {"primary": [platform]},
    }
    state["stage_outputs"] = {"assembly": cursor}
    payload = {
        "recommended_route": route,
        "recommended_read_type": read_type,
        "recommended_method": method,
        "reasoning": "fixture method choice",
        "uncertainty_notes": [],
        "source": "llm",
        "model_request_ref": None,
        "model_response_ref": None,
        "recommendation_ref": None,
    }
    assert _validated_recommended_method(payload, cursor, [method], route) == method
    with patch(
        "app.stages.assembly.nodes._recommend_route_with_decision_boundary",
        return_value=payload,
    ) as model:
        state.update(infer_input_profile(state))
    if policy == "advisor":
        model.assert_called_once()
    else:
        model.assert_not_called()
    assert (
        state["stage_outputs"]["assembly"]["recommended_assembly_selection"][
            "read_type"
        ]
        == read_type
    )
    state.update(select_route(state))
    if policy == "advisor":
        state.update(block_assembly_for_human(state))
        assert _request_payload(state)["decisions_allowed"] == [
            f"select_{route}",
            "stop_human_review",
        ]
        state["active_interrupt"]["blocked_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        ).isoformat()
        state.update(
            _apply_route_review(
                state,
                _auto_request(state),
                blocked_timeout_seconds=300,
                decision_source="foreground_countdown",
            )
        )
    assert (
        state["stage_outputs"]["assembly"]["assembly_selection"]["read_type"]
        == read_type
    )
    context = build_assembly_recommendation_context(state)
    assert context.read_type == read_type
    assert context.assembly_route == route


@pytest.mark.parametrize("raw_type", ["unsupported", "pacbio_hifi"])
def test_recommendation_cannot_fall_back_or_borrow_an_unavailable_input_type(raw_type):
    payload = {
        "recommended_route": "hifi",
        "recommended_read_type": raw_type,
        "recommended_method": "assembly.candidate.flye",
    }
    with pytest.raises(ValueError, match="recommended_read_type"):
        _validated_recommended_method(
            payload,
            {"normalized_read_type": "pacbio_clr"},
            ["assembly.candidate.flye"],
            "hifi",
        )


def test_existing_clr_request_can_resume_without_rewriting_its_frozen_menu(tmp_path):
    state = InputProfilerTests()._state(str(tmp_path))
    state["stage_outputs"] = {
        "assembly": {
            "status": "profile_ready",
            "normalized_read_type": "pacbio_clr",
            "input_profile": {
                "recommended_route": "insufficient_data",
                "recommendation_reasoning": "old selector has no CLR route",
            },
        }
    }
    with patch(
        "app.stages.assembly.nodes._available_routes",
        return_value=["cyclone", "ont", "hifi"],
    ):
        state.update(select_route(state))
        state.update(block_assembly_for_human(state))
    original = _request_payload(state)
    result = _apply_route_review(state, _human_request(state, "select_cyclone"))
    assembly = result["stage_outputs"]["assembly"]
    assert assembly["selected_route"] == "clr"
    assert assembly["assembly_selection"]["read_type"] == "pacbio_clr"
    assert assembly["assembly_selection"]["primary_method"] == "assembly.candidate.flye"
    assert _request_payload(state) == original


def test_config_and_model_route_catalog_share_the_canonical_routes():
    actions = load_knowledge_pack("route_selector", include_actions=True)[
        "allowed_actions"
    ]
    assert set(actions) == {*ROUTE_TO_READ_TYPE, "insufficient_data"}
    for route in ROUTE_TO_READ_TYPE:
        config = RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "assembly", "allow_local_execution": True}
                ],
                "assembly": {"assembly_route": route},
            }
        )
        assert config.assembly.assembly_route == route
