"""Provider identity survives binding, parse failures and response persistence."""

import pytest
from langchain_core.messages import AIMessage

from app.llm.json_response import model_response_record, validate_model_response_record
from app.llm.recommender import StructuredRecommender


class Client:
    temperature = 0.2
    max_tokens = 500
    api_key = "DO_NOT_RECORD"

    def __init__(self, model, returned=None, content='{"ok": true}'):
        self.model_name, self.returned, self.content = model, returned, content

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        return AIMessage(
            content=self.content, response_metadata={"model_name": self.returned}
        )


@pytest.mark.parametrize("method", ["prompt_json", "json_schema"])
@pytest.mark.parametrize("content", ['{"ok": true}', "not JSON"])
def test_actual_model_and_sampling_survive_all_outcomes(method, content):
    result = StructuredRecommender(
        lambda obj: obj, schema={"type": "object"}, method=method
    ).invoke(
        [],
        model_factory=lambda: Client("configured-model", "returned-revision", content),
    )
    assert result.model_id == "returned-revision"
    call = result.model_call
    assert call.configured_model == "configured-model"
    assert call.returned_model == "returned-revision"
    assert call.output_method == method
    assert call.sampling == {"temperature": 0.2, "max_tokens": 500}
    assert "DO_NOT_RECORD" not in call.model_dump_json()
    record = model_response_record(
        schema_version=2,
        raw_response=result.raw_response,
        valid=result.valid,
        error=result.error,
        model_id=result.model_id,
        model_call=call.model_dump(mode="json"),
    )
    assert validate_model_response_record(
        record, expected_schema_version=2, label="test"
    )["model_call"] == call.model_dump(mode="json")


def test_same_class_different_model_different_identity():
    results = [
        StructuredRecommender(lambda obj: obj).invoke(
            [], model_factory=lambda name=name: Client(name)
        )
        for name in ("alpha", "beta")
    ]
    assert [item.model_id for item in results] == ["alpha", "beta"]
    assert (
        results[0].model_call.client_implementation
        == results[1].model_call.client_implementation
    )


def test_call_failure_still_records_configured_model():
    class Failing(Client):
        def invoke(self, messages):
            raise RuntimeError("connection failed")

    result = StructuredRecommender(lambda obj: obj).invoke(
        [], model_factory=lambda: Failing("alpha")
    )
    assert not result.valid
    assert result.model_call.configured_model == "alpha"
    assert result.model_call.returned_model is None
