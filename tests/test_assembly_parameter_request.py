"""Focused tests for the direct assembly parameter request DTO."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.llm.parameters.assembly.response import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.llm.parameters.assembly.request import (
    AssemblyParameterRequestDTO,
    build_parameter_request_dto,
)
from app.stages.assembly.parameters.basis import build_parameter_basis_snapshot
from app.stages.assembly.parameters.catalog import load_parameter_catalog
from app.stages.assembly.parameters.profile import build_prompt_profile


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
        prompt_profile=build_prompt_profile(profile, "recommend_initial"),
    )
    request = build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(profile, "recommend_initial"),
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
        "method_guidance",
        "adjustable_parameters",
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
    parameter_specs = list(request["adjustable_parameters"].values())
    for spec in parameter_specs:
        assert set(spec).issubset(
            {
                "value_type",
                "allowed_values",
                "min",
                "max",
                "description",
                "notes",
            }
        )


def test_request_dto_rejects_accidental_audit_fields() -> None:
    _, request = _initial_request()

    with pytest.raises(ValidationError):
        AssemblyParameterRequestDTO.model_validate(
            {**request, "session_id": "must-not-enter-model-input"}
        )
    assert "sample-1" not in json.dumps(request, ensure_ascii=False)


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


def test_retry_reuses_basis_and_exposes_previous_recommendation_as_context_only() -> None:
    catalog = load_parameter_catalog("hifiasm")
    initial_profile = _full_profile()
    basis = build_parameter_basis_snapshot(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(initial_profile, "recommend_initial"),
    )
    previous = {
        "attempt_id": 0,
        "method_id": "hifiasm",
        "parameters": {"purge_level": 1},
        "execution_status": "failed",
        "failure_class": "memory_exhausted",
    }
    retry_profile = _full_profile(previous_attempt=previous)

    retry = build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(
            retry_profile,
            "retry_after_execution_failure",
        ),
        task="retry_after_execution_failure",
        basis_snapshot=basis,
    )

    assert retry["literature_cases"] == build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(initial_profile, "recommend_initial"),
        task="recommend_initial",
        basis_snapshot=basis,
    )["literature_cases"]
    assert "baseline_params" not in retry
    assert retry["retry_context"]["previous_attempt"]["parameters"] == {
        "purge_level": 1
    }
    assert retry["retry_context"]["previous_attempt"]["failure_class"] == (
        "memory_exhausted"
    )


def test_retry_dto_never_receives_attempt_or_command_audit_fields() -> None:
    catalog = load_parameter_catalog("hifiasm")
    initial_profile = _full_profile()
    basis = build_parameter_basis_snapshot(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(initial_profile, "recommend_initial"),
    )
    retry_profile = _full_profile(
        previous_attempt={
            "attempt_id": 9,
            "method_id": "hifiasm",
            "tool_version": "0.25",
            "command_argv": ["hifiasm", "-o", "/private/run/work/out"],
            "stderr_ref": "artifacts/assembly/attempts/009/stderr.log",
            "stdout_ref": "artifacts/assembly/attempts/009/stdout.log",
            "execution_status": "failed",
            "failure_class": "memory_exhausted",
        }
    )

    request = build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(
            retry_profile,
            "retry_after_execution_failure",
        ),
        task="retry_after_execution_failure",
        basis_snapshot=basis,
    )

    serialized = json.dumps(request, ensure_ascii=False)
    assert "command_argv" not in serialized
    assert "/private/run/work/out" not in serialized
    assert "stderr_ref" not in serialized
    assert "stdout_ref" not in serialized
    assert '"attempt_id"' not in serialized


def test_basis_and_request_omit_redundant_selection_identity_and_audit_locators() -> None:
    catalog = load_parameter_catalog("hifiasm")
    profile = _full_profile()
    basis = build_parameter_basis_snapshot(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(profile, "recommend_initial"),
    )
    request = build_parameter_request_dto(
        method_id="hifiasm",
        catalog=catalog,
        prompt_profile=build_prompt_profile(profile, "recommend_initial"),
        task="recommend_initial",
        basis_snapshot=basis,
    )

    assert "query_id" not in basis["literature_selection"]
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
            prompt_profile=build_prompt_profile(profile, "recommend_initial"),
        )
        request = build_parameter_request_dto(
            method_id=method_id,
            catalog=catalog,
            prompt_profile=build_prompt_profile(profile, "recommend_initial"),
            task="recommend_initial",
            basis_snapshot=basis,
        )

        json.dumps(request)
        assert "method_guidance" not in request
        assert "literature_cases" not in request
