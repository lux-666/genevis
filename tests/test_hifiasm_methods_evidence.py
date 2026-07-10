"""Task 4 tests for hifiasm Methods-aware recommendation contracts."""

from __future__ import annotations

import pytest

from app.domains.assembly.methods_evidence import (
    HIFIASM_EFFECT_AREAS,
    build_hifiasm_evidence_payload,
    normalize_hifiasm_methods_response,
)


def _case(case_id: str = "case_a") -> dict:
    return {
        "case_id": case_id,
        "source": {"kind": "paper", "title": "Example"},
        "numeric_match": {"distance": 0.25, "weak_evidence": False},
        "observed_facts": {"coverage": {"value": 30}},
        "intervention": {
            "tool": "hifiasm",
            "parameters": {
                "purge_level": {
                    "value": 1,
                    "reported": True,
                    "evidence_span_ids": ["methods.rationale"],
                }
            },
        },
        "rationale_claims": [
            {
                "claim": "Reduced purging preserved divergent haplotypes.",
                "explicitness": "explicit",
                "evidence_span_ids": ["methods.rationale"],
            }
        ],
        "source_excerpts": [
            {
                "span_id": "methods.rationale",
                "document": "methods",
                "quote": "We used purge level 1 to preserve divergent haplotypes.",
            }
        ],
        "outcomes": {"assembly": {"value": "phased"}},
    }


def _prompt_payload() -> dict:
    return {
        "task": "recommend_initial",
        "prompt_profile": {"genome_survey": {"heterozygosity": 0.012}},
        "adjustable_params": {
            "purge_level": {"level": 2, "default": 0},
            "kmer_length": {"level": 2, "default": 51},
        },
        "case_retrieval": {
            "retrieval_backend": "hifiasm_numeric",
            "query_id": "q1",
            "candidate_case_ids": ["case_a"],
        },
        "evidence_cases": [_case()],
    }


def _response() -> dict:
    return {
        "case_assessments": [
            {
                "case_id": "case_a",
                "relevance": "strong",
                "transferable_findings": [
                    {
                        "effect_area": "purging",
                        "direction": "decrease_aggressiveness",
                        "evidence_span_ids": ["methods.rationale"],
                        "explanation": "The Methods rationale directly addresses purging.",
                    }
                ],
                "conflicts_or_limits": [],
                "accepted_as_support": True,
            }
        ],
        "recommendation": {
            "adjustable_params": {
                "purge_level": {
                    "action": "set",
                    "value": 1,
                    "basis": "case",
                    "supporting_case_ids": ["case_a"],
                    "supporting_span_ids": ["methods.rationale"],
                    "confidence": "strong",
                    "reason": "The accepted case directly supports less aggressive purging.",
                },
                "kmer_length": {
                    "action": "keep_default",
                    "value": 51,
                    "basis": "catalog_default",
                    "supporting_case_ids": [],
                    "supporting_span_ids": [],
                    "confidence": "weak",
                    "reason": "No visible evidence supports changing the default.",
                },
            }
        },
        "selected_case_ids": ["case_a"],
        "summary": "Use the accepted Methods evidence only for purge_level.",
    }


def test_evidence_payload_contains_only_numeric_shortlist_and_exact_excerpts():
    payload = build_hifiasm_evidence_payload(
        evidence_cases=[_case()],
        case_retrieval={
            "retrieval_backend": "hifiasm_numeric",
            "query_id": "q1",
            "candidate_case_ids": ["case_a"],
            "warning": None,
        },
    )

    assert payload["candidate_case_ids"] == ["case_a"]
    assert [case["case_id"] for case in payload["cases"]] == ["case_a"]
    assert payload["effect_areas"] == list(HIFIASM_EFFECT_AREAS)
    assert payload["cases"][0]["source_excerpts"][0]["quote"].startswith("We used")
    assert "source_documents" not in str(payload)
    assert "body.md" not in str(payload)


def test_evidence_payload_rejects_case_outside_numeric_shortlist():
    with pytest.raises(ValueError, match="numeric shortlist"):
        build_hifiasm_evidence_payload(
            evidence_cases=[_case("invented")],
            case_retrieval={
                "retrieval_backend": "hifiasm_numeric",
                "query_id": "q1",
                "candidate_case_ids": ["case_a"],
            },
        )


def test_methods_response_normalizes_assessments_and_parameter_decisions():
    normalized = normalize_hifiasm_methods_response(_response(), _prompt_payload())

    assert normalized["params"] == {"purge_level": 1}
    assert normalized["omitted_parameters"] == ["kmer_length"]
    assert normalized["selected_case_ids"] == ["case_a"]
    assert normalized["parameter_evidence"]["purge_level"]["bases"] == ["case"]
    assert normalized["case_assessments"][0]["accepted_as_support"] is True
    assert normalized["parameter_decisions"]["kmer_length"]["action"] == "keep_default"
    assert normalized["reasoning"] == normalized["summary"]


def test_methods_response_rejects_legacy_params_only_contract():
    with pytest.raises(ValueError, match="case_assessments"):
        normalize_hifiasm_methods_response(
            {"params": {"purge_level": 1}},
            _prompt_payload(),
        )


def test_methods_response_requires_one_decision_for_every_l2_parameter():
    response = _response()
    response["recommendation"]["adjustable_params"].pop("kmer_length")

    with pytest.raises(ValueError, match="exactly one decision.*kmer_length"):
        normalize_hifiasm_methods_response(response, _prompt_payload())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda response: response["case_assessments"][0].update(case_id="invented"), "numeric shortlist"),
        (lambda response: response["case_assessments"][0]["transferable_findings"][0].update(effect_area="magic"), "effect_area"),
        (lambda response: response["case_assessments"][0]["transferable_findings"][0].update(evidence_span_ids=["missing"]), "visible span"),
        (lambda response: response.update(selected_case_ids=["invented"]), "accepted assessment"),
        (lambda response: response["recommendation"]["adjustable_params"].update(free_form={"action": "set", "value": 1, "basis": "catalog_default", "confidence": "weak", "reason": "x"}), "adjustable L2"),
        (lambda response: response["recommendation"]["adjustable_params"]["purge_level"].update(supporting_case_ids=["invented"]), "selected accepted case"),
        (lambda response: response["recommendation"]["adjustable_params"]["purge_level"].update(supporting_span_ids=["invented"]), "non-visible span"),
        (lambda response: response["recommendation"]["adjustable_params"]["purge_level"].pop("basis"), "evidence basis"),
    ],
)
def test_methods_response_rejects_out_of_scope_evidence(mutate, message):
    response = _response()
    mutate(response)

    with pytest.raises(ValueError, match=message):
        normalize_hifiasm_methods_response(response, _prompt_payload())


def test_no_case_response_can_use_profile_skill_or_catalog_default():
    prompt = _prompt_payload()
    prompt["case_retrieval"]["candidate_case_ids"] = []
    prompt["evidence_cases"] = []
    response = {
        "case_assessments": [],
        "recommendation": {
            "adjustable_params": {
                "purge_level": {
                    "action": "set",
                    "value": 1,
                    "bases": ["profile_fact", "skill_heuristic"],
                    "fact_paths": ["genome_survey.heterozygosity"],
                    "heuristic_labels": ["heterozygosity-aware purging"],
                    "supporting_case_ids": [],
                    "supporting_span_ids": [],
                    "confidence": "medium",
                    "reason": "Use readonly heterozygosity and conservative SKILL guidance.",
                },
                "kmer_length": {
                    "action": "keep_default",
                    "value": 51,
                    "basis": "catalog_default",
                    "supporting_case_ids": [],
                    "supporting_span_ids": [],
                    "confidence": "weak",
                    "reason": "Keep the catalog default without case evidence.",
                },
            }
        },
        "selected_case_ids": [],
        "summary": "No compatible paper case was available.",
    }

    normalized = normalize_hifiasm_methods_response(response, prompt)

    assert normalized["params"] == {"purge_level": 1}
    assert normalized["selected_case_ids"] == []


def test_case_basis_requires_a_span_from_each_supporting_case():
    prompt = _prompt_payload()
    case_b = _case("case_b")
    case_b["source_excerpts"][0]["span_id"] = "methods.case_b"
    prompt["case_retrieval"]["candidate_case_ids"] = ["case_a", "case_b"]
    prompt["evidence_cases"].append(case_b)

    response = _response()
    response["case_assessments"].append(
        {
            "case_id": "case_b",
            "relevance": "medium",
            "transferable_findings": [],
            "conflicts_or_limits": [],
            "accepted_as_support": True,
        }
    )
    response["selected_case_ids"] = ["case_a", "case_b"]
    purge = response["recommendation"]["adjustable_params"]["purge_level"]
    purge["supporting_case_ids"] = ["case_a", "case_b"]
    purge["supporting_span_ids"] = ["methods.rationale"]

    with pytest.raises(ValueError, match="cases without a span.*case_b"):
        normalize_hifiasm_methods_response(response, prompt)
