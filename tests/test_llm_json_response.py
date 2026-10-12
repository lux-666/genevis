"""Contract tests for provider-neutral JSON response helpers."""

from __future__ import annotations

import pytest

from app.llm.json_response import (
    model_response_record,
    parse_json_object,
    response_text,
    validate_model_response_record,
)
from app.llm.parameters.assembly.recommender import (
    restore_assembly_parameter_decision,
)
from app.stages.assembly.decision import (
    AssemblyDecisionRequest,
    restore_assembly_decision,
)


def test_parse_json_object_accepts_one_fenced_object() -> None:
    assert parse_json_object('```json\n{"decision": "accept"}\n```') == {
        "decision": "accept"
    }


def test_model_response_record_round_trips_through_one_envelope() -> None:
    record = model_response_record(
        schema_version=2,
        raw_response='{"parameters": {}}',
        valid=True,
        error=None,
        model_id="model-v1",
    )

    assert record == {
        "schema_version": 2,
        "raw_response": '{"parameters": {}}',
        "validity": "parsed",
        "error": None,
        "model_id": "model-v1",
        "model_call": {
            "schema_version": 1,
            "configured_model": None,
            "returned_model": None,
            "client_implementation": None,
            "output_method": None,
            "sampling": {},
        },
    }
    assert (
        validate_model_response_record(
            record,
            expected_schema_version=2,
            label="persisted Assembly model response",
        )
        == record
    )


def test_assembly_restorers_preserve_model_identity() -> None:
    parameter_record = model_response_record(
        schema_version=2,
        raw_response="",
        valid=False,
        error="model unavailable",
        model_id=None,
    )
    route_record = model_response_record(
        schema_version=1,
        raw_response="",
        valid=False,
        error="model unavailable",
        model_id=None,
    )
    parameter_record["model_id"] = 42  # type: ignore[typeddict-item]
    route_record["model_id"] = 42  # type: ignore[typeddict-item]
    route_request = AssemblyDecisionRequest(
        kind="route_selection",
        session_id="s1",
        prompt_payload={},
        system_prompt="Select a route.",
    )

    with pytest.raises(ValueError, match="invalid model_id"):
        restore_assembly_parameter_decision(parameter_record, {})
    with pytest.raises(ValueError, match="invalid model_id"):
        restore_assembly_decision(route_record, route_request)


@pytest.mark.parametrize("content", [[42], [{"other": "value"}], [{"text": 42}]])
def test_response_blocks_cannot_stringify_arbitrary_payloads(content):
    with pytest.raises(ValueError, match="must contain text"):
        response_text(content)


def test_response_blocks_preserve_text_order():
    assert response_text([{"type": "text", "text": '{"ok":'}, "true}"]) == (
        '{"ok":true}'
    )
