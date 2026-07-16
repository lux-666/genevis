"""Focused tests for the direct assembly parameter request DTO."""

from __future__ import annotations

import json

from app.domains.assembly.parameter_recommendation import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.parameter_request import (
    build_parameter_basis_snapshot,
    build_parameter_request_dto,
    previous_execution_overrides,
)


def _full_profile(*, previous_attempt: dict | None = None) -> dict:
    return {
        "sample": {
            "sample_id": "sample-1",
            "organism": "novel species",
            "ploidy": 2,
            "target_level": "chromosome",
        },
        "assets": {
            "assembly_route": "hifi",
            "read_type": "hifi",
            "has_hifi": True,
        },
        "read_qc": {
            "total_bases": 32_000_000_000,
            "read_n50": 20_000,
            "raw_coverage": 32,
        },
        "genome_survey": {
            "effective_genome_size": 1_000_000_000,
            "genome_size_source": "survey",
            "heterozygosity": 0.012,
            "repeat_fraction": 0.45,
        },
        "previous_attempt": previous_attempt or {},
        "assembly_review": {},
    }


def _initial_request() -> tuple[dict, dict]:
    catalog = load_parameter_catalog("hifiasm")
    profile = _full_profile()
    basis = build_parameter_basis_snapshot(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=profile,
    )
    request = build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=profile,
        task="recommend_initial",
        basis_snapshot=basis,
    )
    return basis, request


def test_initial_builder_returns_only_the_strict_model_dto() -> None:
    _, request = _initial_request()

    assert request["recommendation_contract"] == PARAMETER_RECOMMENDATION_CONTRACT
    assert request["literature_cases"]
    assert request["method_guidance"]
    assert set(request) == {
        "recommendation_contract",
        "method_id",
        "task",
        "observations",
        "rules",
        "recommendation_policy",
        "method_guidance",
        "readonly_params",
        "adjustable_params",
        "baseline_params",
        "literature_selection",
        "literature_cases",
    }
    assert {
        "input_profile_hash",
        "prompt_profile",
        "evidence_registry",
        "baseline_overrides",
        "payload_hash",
    }.isdisjoint(request)
    parameter_specs = [
        *request["readonly_params"].values(),
        *request["adjustable_params"].values(),
    ]
    for spec in parameter_specs:
        assert set(spec).issubset(
            {
                "value_type",
                "allowed_values",
                "min",
                "max",
                "default",
                "description",
                "notes",
            }
        )


def test_full_basis_and_model_case_have_separate_explicit_owners() -> None:
    basis, request = _initial_request()

    assert len(request["literature_cases"]) == len(basis["literature_cases"])
    assert [case["case_id"] for case in request["literature_cases"]] == [
        case["case_id"] for case in basis["literature_cases"]
    ]
    for case in request["literature_cases"]:
        assert set(case).issubset(
            {
                "schema_version",
                "case_id",
                "case_type",
                "source",
                "applicability",
                "observed_facts",
                "intervention",
                "rationale_claims",
                "outcomes",
                "source_excerpts",
                "numeric_match",
            }
        )
    assert "methods_path" not in json.dumps(request, ensure_ascii=False)


def test_retry_reuses_basis_and_keeps_execution_overrides_out_of_model_input() -> None:
    catalog = load_parameter_catalog("hifiasm")
    initial_profile = _full_profile()
    basis = build_parameter_basis_snapshot(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=initial_profile,
        session_id="session-1",
        recommendation_id=0,
    )
    previous = {
        "attempt_id": 0,
        "method_id": "hifiasm",
        "effective_params": {"purge_level": 1, "kmer_length": 51},
        "execution_params": {"purge_level": 1},
        "params": {"purge_level": 1, "kmer_length": 51},
        "execution_status": "failed",
        "failure_class": "memory_exhausted",
    }
    retry_profile = _full_profile(previous_attempt=previous)

    retry = build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=retry_profile,
        task="retry_after_execution_failure",
        basis_snapshot=basis,
    )

    assert retry["literature_cases"] == build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=initial_profile,
        task="recommend_initial",
        basis_snapshot=basis,
    )["literature_cases"]
    assert retry["baseline_params"]["purge_level"] == 1
    assert "baseline_overrides" not in retry
    assert previous_execution_overrides(
        retry_profile,
        task="retry_after_execution_failure",
    ) == {"purge_level": 1}
    assert retry["retry_context"]["previous_attempt"]["failure_class"] == (
        "memory_exhausted"
    )


def test_basis_keeps_selection_identity_but_request_omits_audit_locators() -> None:
    catalog = load_parameter_catalog("hifiasm")
    profile = _full_profile()
    basis = build_parameter_basis_snapshot(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=profile,
        session_id="session-2",
        recommendation_id=0,
    )
    request = build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=profile,
        task="recommend_initial",
        basis_snapshot=basis,
    )

    assert basis["literature_selection"]["query_id"] == (
        "session-2:parameter_attempt_0"
    )
    assert "query_id" not in request["literature_selection"]
    request_text = json.dumps(request, ensure_ascii=False)
    assert "retrieval_query_ref" not in request_text
    assert "retrieval_trace_ref" not in request_text


def test_methods_without_guidance_or_cases_produce_serializable_dtos() -> None:
    profile = _full_profile()
    for method_id in ("flye", "nextdenovo"):
        catalog = load_parameter_catalog(method_id)
        basis = build_parameter_basis_snapshot(
            method_id=method_id,
            catalog=catalog,
            full_profile=profile,
        )
        request = build_parameter_request_dto(
            method_id=method_id,
            catalog=catalog,
            full_profile=profile,
            task="recommend_initial",
            basis_snapshot=basis,
        )

        json.dumps(request)
        assert "method_guidance" not in request
        assert "literature_cases" not in request
