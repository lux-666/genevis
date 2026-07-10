"""Tests for optional, auditable parameter evidence bases."""

from app.stages.assembly.parameter_validation import validate_parameter_evidence


def _payload() -> dict:
    return {
        "task": "recommend_initial",
        "prompt_profile": {"genome_survey": {"heterozygosity": 0.012}},
        "case_selection": {
            "retrieval_backend": "hifiasm_numeric",
            "selected_case_ids": ["case_a"],
        },
        "evidence_cases": [
            {
                "case_id": "case_a",
                "source_excerpts": [
                    {"span_id": "methods.rationale", "document": "methods", "quote": "Exact rationale."}
                ],
            }
        ],
    }


def test_profile_fact_basis_does_not_require_a_case():
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "profile_fact",
                "fact_paths": ["genome_survey.heterozygosity"],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=_payload(),
    )

    assert result["valid"] is True


def test_case_basis_requires_visible_case_and_span():
    result = validate_parameter_evidence(
        parameter_evidence={
            "purge_level": {
                "basis": "case",
                "case_ids": ["case_a"],
                "span_ids": ["methods.not_visible"],
            }
        },
        recommended_params={"purge_level": 1},
        prompt_payload=_payload(),
    )

    assert result["valid"] is False
    assert "absent from the prompt" in result["violations"][0]["reason"]


def test_l0_or_l1_output_is_still_rejected_by_catalog_validation():
    from app.schemas.parameter_catalog import MethodParameterCatalog, ParameterSpec
    from app.stages.assembly.parameter_validation import validate_recommended_parameters

    catalog = MethodParameterCatalog(
        method_id="hifiasm",
        tool="hifiasm",
        read_type="hifi",
        parameters={
            "output_prefix": ParameterSpec(cli=None, level=0, source="run_config", value_type="path"),
            "hg_size": ParameterSpec(cli=None, level=1, source="genome_survey", value_type="int"),
            "purge_level": ParameterSpec(
                cli=None,
                level=2,
                source="parameter_recommender",
                value_type="int",
            ),
        },
    )

    result = validate_recommended_parameters(
        method_id="hifiasm",
        catalog=catalog,
        recommended_params={"output_prefix": "/tmp/x", "hg_size": 1000, "purge_level": 1},
        readonly_facts={},
        rules={},
    )

    assert result["valid"] is False
    assert {violation["param"] for violation in result["violations"]} == {"output_prefix", "hg_size"}


def test_selected_case_ids_cannot_invent_non_shortlisted_case_without_parameter_evidence():
    result = validate_parameter_evidence(
        parameter_evidence=None,
        recommended_params={},
        prompt_payload=_payload(),
        declared_selected_case_ids=["invented_case"],
    )

    assert result["valid"] is False
    assert "non-shortlisted" in result["violations"][0]["reason"]
