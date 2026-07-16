"""Focused tests for the literature-patch prompt payload."""

from __future__ import annotations

import json

from app.domains.assembly.parameter_recommendation import (
    PARAMETER_RECOMMENDATION_CONTRACT,
)
from app.llm.assembly_parameter_context import build_assembly_parameter_context
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.prompt_payload import (
    build_prompt_payload,
    generate_payload_ref,
    generate_profile_ref,
    recommendation_basis_snapshot,
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


def test_initial_payload_uses_one_contract_and_one_case_surface() -> None:
    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=load_parameter_catalog("hifiasm"),
        full_profile=_full_profile(),
        task="recommend_initial",
    )

    assert payload["recommendation_contract"] == PARAMETER_RECOMMENDATION_CONTRACT
    assert payload["literature_cases"]
    assert payload["method_guidance"]
    assert payload["baseline_overrides"] == {}
    assert "selected_cases" not in payload
    assert "evidence_cases" not in payload
    assert "methods_evidence" not in payload
    assert "skill_instructions" not in payload
    assert "case_selection" not in payload
    assert all(case["source"]["kind"] == "paper" for case in payload["literature_cases"])


def test_complete_literature_card_is_not_duplicated_in_model_dto() -> None:
    payload = build_prompt_payload(
        method_id="hifiasm",
        catalog=load_parameter_catalog("hifiasm"),
        full_profile=_full_profile(),
        task="recommend_initial",
    )
    context = build_assembly_parameter_context(payload)

    assert len(context["literature_cases"]) == len(payload["literature_cases"])
    assert [case["case_id"] for case in context["literature_cases"]] == [
        case["case_id"] for case in payload["literature_cases"]
    ]
    assert "prompt_profile" not in context
    assert "evidence_registry" not in context
    assert "baseline_overrides" not in context
    for case in context["literature_cases"]:
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


def test_retry_inherits_paper_cards_and_uses_previous_effective_baseline() -> None:
    catalog = load_parameter_catalog("hifiasm")
    initial = build_prompt_payload(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=_full_profile(),
        task="recommend_initial",
        session_id="session-1",
        attempt_id=0,
    )
    initial_basis = recommendation_basis_snapshot(
        catalog=catalog,
        prompt_payload=initial,
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

    retry = build_prompt_payload(
        method_id="hifiasm",
        catalog=catalog,
        full_profile=_full_profile(previous_attempt=previous),
        task="retry_after_execution_failure",
        session_id="session-1",
        attempt_id=1,
        initial_basis_snapshot=initial_basis,
    )

    assert retry["literature_cases"] == initial["literature_cases"]
    assert retry["literature_selection"] == initial["literature_selection"]
    assert retry["method_guidance"] == initial["method_guidance"]
    assert retry["baseline_params"]["purge_level"] == 1
    assert retry["baseline_overrides"] == {"purge_level": 1}
    assert retry["retry_context"]["previous_attempt"]["failure_class"] == "memory_exhausted"


def test_hash_helpers_are_canonical_and_payload_hash_is_semantic() -> None:
    profile = _full_profile()
    assert generate_profile_ref(profile) == generate_profile_ref(json.loads(json.dumps(profile)))

    payload = build_prompt_payload(
        method_id="flye",
        catalog=load_parameter_catalog("flye"),
        full_profile=profile,
        task="recommend_initial",
        session_id="session-2",
        attempt_id=0,
    )
    semantic = {
        key: value
        for key, value in payload.items()
        if key != "payload_hash"
    }
    assert payload["payload_hash"] == generate_payload_ref(semantic)
