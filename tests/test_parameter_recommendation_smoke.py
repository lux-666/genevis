from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.diagnostics.parameter_recommendation_smoke import (
    evaluate_parameter_request,
    load_request_artifact,
)
from app.llm.config import load_llm_profile
from app.stages.assembly.parameters.workflow import recommend_assembly_parameters
from tests.test_parameter_persistence_v2 import _Model, _state


def _make_request(tmp_path: Path, *, minimal_facts: bool = False) -> Path:
    state = _state(tmp_path)
    model = _Model(
        {"parameters": {"purge_level": 1}, "summary": "Use conservative purging."}
    )
    with patch(
        "app.llm.parameters.assembly.recommender.build_chat_model",
        return_value=model,
    ):
        update = recommend_assembly_parameters(state)
    assembly = update["stage_outputs"]["assembly"]
    cursor = assembly["current_parameter_recommendation"]
    manifest_ref = cursor["manifest_ref"]
    from app.schemas.artifacts import ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST
    from app.storage.parameter_evidence import read_parameter_json
    from app.storage.paths import SessionPaths

    merged_state = {**state, "stage_outputs": update["stage_outputs"]}
    manifest = read_parameter_json(
        merged_state,
        manifest_ref,
        expected_type=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    )
    paths = SessionPaths.from_run_dir(state["data_dir"])
    request_ref = manifest["refs"]["model_request_ref"]
    request_path = paths.resolve_ref(request_ref["path_ref"])

    if not minimal_facts:
        return request_path

    # Replace fact origins that require dependency refs with a minimal typed
    # request. This isolates response parsing and catalog validation from the
    # dependency-ref invariant covered elsewhere.
    request_payload = json.loads(request_path.read_text(encoding="utf-8"))
    user_message = request_payload["messages"][1]["content"]
    request_dto = json.loads(user_message)
    request_dto["facts"] = {
        "species": {"scientific_name": "Example species"},
        "reads": {},
        "genome": {},
        "sequencing": {},
        "asset_presence": {"has_ont": True},
    }
    minimal_path = tmp_path / "minimal_model_request.json"
    request_payload["messages"][1]["content"] = json.dumps(
        request_dto, ensure_ascii=False, separators=(",", ":")
    )
    minimal_path.write_text(
        json.dumps(request_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return minimal_path


def test_live_smoke_replays_request_without_approval_or_execution(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GENE_AGENT_LLM_API_KEY", "smoke-key")
    request_path = _make_request(tmp_path, minimal_facts=True)
    request_dto = load_request_artifact(request_path)

    class LiveLikeModel:
        def invoke(self, messages):
            assert messages == [
                {
                    "role": "system",
                    "content": messages[0]["content"],
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        request_dto, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            ]
            return SimpleNamespace(
                content=json.dumps({"parameters": {}, "summary": "Tool defaults."})
            )

    def decide(request):
        from app.llm.parameters.assembly.recommender import (
            recommend_assembly_parameters,
        )

        with patch(
            "app.llm.parameters.assembly.recommender.build_chat_model",
            return_value=LiveLikeModel(),
        ):
            return recommend_assembly_parameters(request)

    result = evaluate_parameter_request(request_dto, decision_maker=decide)

    assert result["method_id"] == "assembly.candidate.hifiasm_ont"
    assert result["passed"] is True
    assert result["validation"]["valid"] is True


def test_live_smoke_rejects_unknown_parameter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GENE_AGENT_LLM_API_KEY", "smoke-key")
    request_path = _make_request(tmp_path, minimal_facts=True)
    request_dto = load_request_artifact(request_path)

    class Model:
        def invoke(self, _messages):
            return SimpleNamespace(
                content=json.dumps({"parameters": {"not_a_parameter": 1}})
            )

    def decide(request):
        from app.llm.parameters.assembly.recommender import (
            recommend_assembly_parameters,
        )

        with patch(
            "app.llm.parameters.assembly.recommender.build_chat_model",
            return_value=Model(),
        ):
            return recommend_assembly_parameters(request)

    result = evaluate_parameter_request(request_dto, decision_maker=decide)

    assert result["passed"] is False
    assert (
        "is not cataloged as L2-adjustable"
        in (result["validation"]["violations"][0]["reason"])
    )


def test_load_llm_profile_sees_dotenv_value(tmp_path: Path, monkeypatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("GENE_AGENT_LLM_MODEL=dotenv-model\n", encoding="utf-8")
    monkeypatch.delenv("GENE_AGENT_LLM_MODEL", raising=False)

    from app.entrypoints.environment import load_dotenv_without_override

    loaded = load_dotenv_without_override([dotenv])

    try:
        assert loaded == dotenv
        assert os.environ["GENE_AGENT_LLM_MODEL"] == "dotenv-model"
        assert load_llm_profile().model == "dotenv-model"
    finally:
        monkeypatch.delenv("GENE_AGENT_LLM_MODEL", raising=False)
