"""Focused tests for the direct assembly parameter request DTO."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.domains.assembly.constants import (
    FLYE_METHOD,
    HIFIASM_METHOD,
    NEXTDENOVO_METHOD,
)
from app.llm.parameters.assembly.request import (
    AssemblyParameterRequestDTO,
    build_parameter_request_dto,
)
from app.llm.parameters.assembly.response import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalAssetPresence,
    CanonicalGenomeFacts,
    CanonicalReadFacts,
    CanonicalRecommendationFacts,
    CanonicalSequencingFacts,
    CanonicalSpeciesFacts,
    ExecutionFailureTrigger,
    ParameterAttemptEvidenceV3,
    ParameterExecutionEvidence,
    QuantityFact,
    QuantityOrigin,
    RecommendationRetryContext,
)
from app.stages.assembly.parameters.basis import build_parameter_basis_snapshot
from app.stages.assembly.parameters.catalog import load_parameter_catalog


def _quantity(
    value: int | float,
    unit: str,
    *,
    kind: str,
    producer: str,
) -> QuantityFact:
    return QuantityFact(
        value=value,
        unit=unit,
        origin=QuantityOrigin(kind=kind, producer=producer),
    )


def _facts() -> CanonicalRecommendationFacts:
    return CanonicalRecommendationFacts(
        species=CanonicalSpeciesFacts(
            scientific_name="novel species",
            ploidy=_quantity(2, "count", kind="provided", producer="run_config"),
        ),
        reads=CanonicalReadFacts(
            read_type="pacbio_hifi",
            total_bases=_quantity(
                32_000_000_000,
                "bp",
                kind="measured",
                producer="read_qc",
            ),
            read_n50=_quantity(
                20_000,
                "bp",
                kind="measured",
                producer="read_qc",
            ),
        ),
        genome=CanonicalGenomeFacts(
            effective_genome_size=_quantity(
                1_000_000_000,
                "bp",
                kind="resolved",
                producer="genome_survey",
            ),
            heterozygosity=_quantity(
                0.012,
                "fraction",
                kind="measured",
                producer="genome_survey",
            ),
            repeat_fraction=_quantity(
                0.45,
                "fraction",
                kind="measured",
                producer="genome_survey",
            ),
        ),
        sequencing=CanonicalSequencingFacts(
            raw_coverage=_quantity(
                32,
                "x",
                kind="derived",
                producer="parameter_recommendation",
            )
        ),
        asset_presence=CanonicalAssetPresence(has_hifi=True),
    )


def _context(
    method_id: str = HIFIASM_METHOD,
    *,
    retry: RecommendationRetryContext | None = None,
) -> AssemblyRecommendationContext:
    if method_id == HIFIASM_METHOD:
        route, read_type = "hifi", "pacbio_hifi"
    else:
        route, read_type = "cyclone", "ont_hq"
    return AssemblyRecommendationContext(
        stage="assembly",
        assembly_route=route,
        read_type=read_type,
        target_level="draft",
        retry=retry,
    )


def _initial_request() -> tuple[dict, dict]:
    catalog = load_parameter_catalog(HIFIASM_METHOD)
    facts = _facts()
    context = _context()
    basis = build_parameter_basis_snapshot(
        method_id=HIFIASM_METHOD,
        catalog=catalog,
        facts=facts,
        context=context,
    )
    request = build_parameter_request_dto(
        method_id=HIFIASM_METHOD,
        catalog=catalog,
        facts=facts,
        context=context,
        task="recommend_initial",
        basis_snapshot=basis,
    )
    return basis, request


def _retry_context(
    *, include_locator_fields: bool = False
) -> RecommendationRetryContext:
    execution = ParameterExecutionEvidence(
        status="failed",
        failure_class="memory_exhausted",
        stderr_tail=(
            "unknown option --purge-level" if include_locator_fields else None
        ),
    )
    previous = ParameterAttemptEvidenceV3(
        attempt_id=0,
        task="recommend_initial",
        status="execution_failed",
        recommended_parameters={"purge_level": 1},
        approved_parameters={"purge_level": 1},
        effective_parameters={"purge_level": 1},
        effective_sources={"purge_level": "l2"},
        execution=execution,
    )
    return RecommendationRetryContext(
        trigger=ExecutionFailureTrigger(
            status="failed",
            failure_class="memory_exhausted",
        ),
        attempt_history=(previous,),
    )


def test_initial_builder_returns_only_the_strict_model_dto() -> None:
    _, request = _initial_request()

    assert request["schema_version"] == 3
    assert request["recommendation_contract"] == PARAMETER_RECOMMENDATION_CONTRACT
    assert request["literature_cases"]
    assert request["method_guidance"]
    assert set(request) == {
        "schema_version",
        "recommendation_contract",
        "method_id",
        "task",
        "facts",
        "assembly_context",
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
        "dependency_refs",
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


def test_retry_reuses_basis_and_exposes_typed_attempt_history() -> None:
    catalog = load_parameter_catalog(HIFIASM_METHOD)
    facts = _facts()
    initial_context = _context()
    basis = build_parameter_basis_snapshot(
        method_id=HIFIASM_METHOD,
        catalog=catalog,
        facts=facts,
        context=initial_context,
    )

    retry = build_parameter_request_dto(
        method_id=HIFIASM_METHOD,
        catalog=catalog,
        facts=facts,
        context=_context(retry=_retry_context()),
        task="retry_after_execution_failure",
        basis_snapshot=basis,
    )
    initial = build_parameter_request_dto(
        method_id=HIFIASM_METHOD,
        catalog=catalog,
        facts=facts,
        context=initial_context,
        task="recommend_initial",
        basis_snapshot=basis,
    )

    assert retry["literature_cases"] == initial["literature_cases"]
    assert "baseline_params" not in retry
    history = retry["assembly_context"]["retry"]["attempt_history"]
    assert history[0]["recommended_parameters"] == {"purge_level": 1}
    assert history[0]["execution"]["failure_class"] == "memory_exhausted"


def test_retry_dto_exposes_only_bounded_attempt_evidence() -> None:
    catalog = load_parameter_catalog(HIFIASM_METHOD)
    facts = _facts()
    initial_context = _context()
    basis = build_parameter_basis_snapshot(
        method_id=HIFIASM_METHOD,
        catalog=catalog,
        facts=facts,
        context=initial_context,
    )

    request = build_parameter_request_dto(
        method_id=HIFIASM_METHOD,
        catalog=catalog,
        facts=facts,
        context=_context(retry=_retry_context(include_locator_fields=True)),
        task="retry_after_execution_failure",
        basis_snapshot=basis,
    )

    serialized = json.dumps(request, ensure_ascii=False)
    assert "command_argv" not in serialized
    assert "/private/run/work/out" not in serialized
    assert "stderr_ref" not in serialized
    assert "stdout_ref" not in serialized
    assert "unknown option --purge-level" in serialized
    attempt = request["assembly_context"]["retry"]["attempt_history"][0]
    assert set(attempt) == {
        "attempt_id",
        "task",
        "status",
        "recommended_parameters",
        "approved_parameters",
        "effective_parameters",
        "effective_sources",
        "execution",
    }


def test_basis_and_request_omit_redundant_selection_identity_and_audit_locators() -> (
    None
):
    basis, request = _initial_request()

    assert "query_id" not in basis["literature_selection"]
    assert "query_id" not in request["literature_selection"]
    request_text = json.dumps(request, ensure_ascii=False)
    assert "retrieval_query_ref" not in request_text
    assert "retrieval_trace_ref" not in request_text


def test_methods_without_guidance_or_cases_produce_serializable_dtos() -> None:
    facts = _facts()
    for method_id in (FLYE_METHOD, NEXTDENOVO_METHOD):
        catalog = load_parameter_catalog(method_id)
        context = _context(method_id)
        basis = build_parameter_basis_snapshot(
            method_id=method_id,
            catalog=catalog,
            facts=facts,
            context=context,
        )
        request = build_parameter_request_dto(
            method_id=method_id,
            catalog=catalog,
            facts=facts,
            context=context,
            task="recommend_initial",
            basis_snapshot=basis,
        )

        json.dumps(request)
        assert "method_guidance" not in request
        assert "literature_cases" not in request
