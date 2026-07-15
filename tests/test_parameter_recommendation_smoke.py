from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import app.diagnostics.parameter_recommendation_smoke as smoke
from app.diagnostics.parameter_recommendation_smoke import (
    evaluate_parameter_prompt,
    load_replay_inputs,
)
from app.domains.assembly.parameter_recommendation import (
    PARAMETER_RECOMMENDATION_CONTRACT,
    normalize_parameter_recommendation,
)
from app.stages.assembly.decision import AssemblyDecisionResult


def _payload() -> dict:
    return {
        "recommendation_contract": PARAMETER_RECOMMENDATION_CONTRACT,
        "method_id": "hifiasm_ont",
        "task": "recommend_initial",
        "observations": [{"id": "pf_het", "name": "heterozygosity", "value": 0.01}],
        "method_guidance": [],
        "literature_cases": [],
        "adjustable_params": {"purge_level": {"default": 2}},
        "baseline_params": {"purge_level": 2},
        "baseline_overrides": {},
        "rules": {"rules": []},
    }


def _model_response(payload: dict) -> dict:
    return normalize_parameter_recommendation(
        {
            "sample_diagnosis": {"effect_areas": ["purging"]},
            "parameter_patch": {
                "purge_level": {
                    "value": 1,
                    "observation_ids": ["pf_het"],
                    "guidance_ids": [],
                    "literature_anchors": [],
                    "expected_effect": "Preserve divergent sequence.",
                    "tradeoffs": ["May retain alternate sequence."],
                    "confidence": "medium",
                    "reason": "Heterozygosity favors conservative purging.",
                }
            },
            "summary": "Use conservative purging.",
        },
        payload,
    )


def test_probe_uses_production_dto_and_catalog_validation() -> None:
    payload = _payload()

    def decide(_request):
        return AssemblyDecisionResult(
            kind="parameter_recommendation",
            raw_response="{}",
            parsed=_model_response(payload),
            valid=True,
            model_id="fake-live-model",
        )

    result = evaluate_parameter_prompt(payload, full_profile={}, decision_maker=decide)

    assert result["passed"] is True
    assert result["decision"]["model_id"] == "fake-live-model"
    assert result["validation"]["valid"] is True


def test_probe_preserves_model_parse_failure_for_diagnosis() -> None:
    payload = _payload()

    def decide(_request):
        return AssemblyDecisionResult(
            kind="parameter_recommendation",
            raw_response='{"sample_diagnosis": {}}',
            valid=False,
            error="ValueError: summary is required",
        )

    result = evaluate_parameter_prompt(payload, full_profile={}, decision_maker=decide)

    assert result["passed"] is False
    assert result["validation"]["violations"][0]["reason"] == "ValueError: summary is required"


def test_replay_inputs_find_sibling_profile_and_verify_hash(tmp_path: Path) -> None:
    profile = tmp_path / "input_profile.json"
    profile.write_text("{}", encoding="utf-8")
    payload = _payload()
    payload["input_profile_ref"] = "/stale/remote/input_profile.json"
    payload["input_profile_hash"] = hashlib.sha256(profile.read_bytes()).hexdigest()
    prompt = tmp_path / "prompt_payload.json"
    prompt.write_text(json.dumps(payload), encoding="utf-8")

    _, loaded_payload, resolved_profile, loaded_profile = load_replay_inputs(prompt)

    assert loaded_payload["method_id"] == "hifiasm_ont"
    assert resolved_profile == profile.resolve()
    assert loaded_profile == {}


def test_cli_loads_project_dotenv_before_live_probe(tmp_path: Path, monkeypatch, capsys) -> None:
    prompt = tmp_path / "prompt_payload.json"
    profile = tmp_path / "input_profile.json"
    prompt.write_text(json.dumps(_payload()), encoding="utf-8")
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "GENE_AGENT_LLM_API_KEY=smoke-secret\n"
        "GENE_AGENT_LLM_MODEL=smoke-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GENE_AGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("GENE_AGENT_LLM_MODEL", raising=False)
    observed: dict[str, str | None] = {}

    def fake_run_probe(**_kwargs):
        observed["api_key"] = os.environ.get("GENE_AGENT_LLM_API_KEY")
        return (
            {
                "method_id": "hifiasm_ont",
                "attempt_count": 1,
                "passed_count": 1,
                "failed_count": 0,
                "passed": True,
                "attempts": [],
            },
            tmp_path / "summary.json",
        )

    monkeypatch.setattr(smoke, "run_probe", fake_run_probe)

    code = smoke.main(
        [
            "--prompt-payload",
            str(prompt),
            "--input-profile",
            str(profile),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert code == 0
    assert observed["api_key"] == "smoke-secret"
    assert "llm_model: smoke-model" in capsys.readouterr().out
