"""Task 4 tests for hifiasm Methods-aware recommendation contracts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path

import pytest

from app.domains.assembly import methods_evidence as methods_evidence_module
from app.domains.assembly import numeric_case_retriever as numeric_retriever_module
from app.domains.assembly.evidence_case import (
    _rank_hifiasm_prompt_case,
    project_hifiasm_prompt_safe_case,
    validate_hifiasm_evidence_cases,
)
from app.domains.assembly.methods_evidence import (
    HIFIASM_EFFECT_AREAS,
    HIFIASM_METHODS_EVIDENCE_SERIALIZED_BYTES,
    HIFIASM_METHODS_PROMPT_SCHEMA_VERSION,
    build_hifiasm_evidence_payload,
    normalize_hifiasm_methods_response,
    sanitize_hifiasm_methods_prompt_payload,
)
from app.knowledge.evidence_cases import EvidenceCaseValidationError
from app.knowledge.packs import load_knowledge_pack
from app.stages.assembly.decision import AssemblyDecisionRequest, make_assembly_decision
from app.stages.assembly.prompt_payload import generate_payload_ref


_SOURCE_DIR = tempfile.TemporaryDirectory(prefix="hifiasm_methods_evidence_")
_SOURCE_ROOT = Path(_SOURCE_DIR.name)


def _case(
    case_id: str = "case_a",
    *,
    quote: str | None = None,
    spans: list[dict[str, str]] | None = None,
    parameter_span_ids: list[str] | None = None,
    curation: dict | None = None,
    rationale_claims: list[dict] | None = None,
    numeric_match: dict | None = None,
) -> dict:
    if spans is None:
        spans = [
            {
                "span_id": f"methods.{case_id}",
                "document": "methods",
                "quote": quote or "We used purge level 1 to preserve divergent haplotypes.",
            }
        ]
    span_ids = [span["span_id"] for span in spans]
    cited_span_ids = [span["span_id"] for span in spans if span["document"] == "methods"]
    cited_span_ids = cited_span_ids or span_ids

    case_root = _SOURCE_ROOT / case_id
    case_root.mkdir(parents=True, exist_ok=True)
    documents = {}
    for document_name in ("body", "methods"):
        document_spans = [span for span in spans if span["document"] == document_name]
        excerpt_chars = sum(len(span["quote"]) for span in document_spans)
        content = "\n".join(
            f"BEGIN {index} {span['span_id']}\n{span['quote']}\nEND {index} {span['span_id']}"
            for index, span in enumerate(document_spans)
        ) or f"No cited {document_name} evidence for {case_id}."
        padding = "0123456789" * (excerpt_chars // 40 + 30)
        content = (
            f"Private {document_name} context for {case_id}.\n{content}\n"
            f"Unquoted trailing context: {padding}"
        )
        path = case_root / f"{document_name}.md"
        path.write_text(content, encoding="utf-8")
        documents[document_name] = {
            "path": str(path.relative_to(_SOURCE_ROOT)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    raw = {
        "schema_version": 2,
        "case_id": case_id,
        "case_type": "initial",
        "source": {"kind": "paper", "title": f"Example {case_id}"},
        "applicability": {
            "read_type": "hifi",
            "source_tool": "hifiasm",
            "target_methods": ["hifiasm"],
        },
        "observed_facts": {
            "coverage": {"value": 30, "evidence_span_ids": cited_span_ids},
        },
        "intervention": {
            "tool": "hifiasm",
            "parameters": {
                "purge_level": {
                    "value": 1,
                    "reported": True,
                    "evidence_span_ids": parameter_span_ids or cited_span_ids,
                }
            },
        },
        "rationale_claims": rationale_claims
        if rationale_claims is not None
        else [
            {
                "claim_id": "purge",
                "statement": "Reduced purging preserved divergent haplotypes.",
                "explicitness": "explicit",
                "effect_area": "purging",
                "evidence_span_ids": cited_span_ids,
            }
        ],
        "outcomes": {},
        "curation": curation or {"status": "approved", "curator": "test"},
        "source_documents": {**documents, "spans": spans},
    }
    if not spans:
        raw["observed_facts"] = {}
        raw["intervention"]["parameters"]["purge_level"].update(
            {"reported": False, "evidence_span_ids": []}
        )
        if rationale_claims is None:
            raw["rationale_claims"] = []
    validated = validate_hifiasm_evidence_cases([raw], pack_root=_SOURCE_ROOT)[0]
    return _rank_hifiasm_prompt_case(
        validated,
        numeric_match=numeric_match or {"distance": 0.25, "weak_evidence": False},
    )


def _case_retrieval(cases: list[dict], *, query_id: str = "q1") -> dict:
    case_ids = [case["case_id"] for case in cases]
    trace = [
        {
            "case_id": case["case_id"],
            "decision": "candidate",
            "hard_filter_reasons": [],
            **deepcopy(case["numeric_match"]),
        }
        for case in cases
    ]
    metadata = {
        "retrieval_backend": "hifiasm_numeric",
        "query_id": query_id,
        "selected_case_ids": case_ids,
        "candidate_case_ids": case_ids,
        "evidence_case_ids": case_ids,
        "selected_count": len(case_ids),
        "eligible_case_count": len(case_ids),
        "total_cases": len(case_ids),
        "evidence_available": bool(case_ids),
        "case_evidence_required": False,
    }
    metadata[numeric_retriever_module._RANKED_SELECTION_FIELD] = (
        numeric_retriever_module._attest_hifiasm_ranked_selection(
            selected_cases=cases,
            metadata=metadata,
            trace=trace,
        )
    )
    return metadata


def _prompt_payload(*, cases: list[dict] | None = None) -> dict:
    evidence_cases = [_case()] if cases is None else cases
    return {
        "method_id": "hifiasm",
        "task": "recommend_initial",
        "prompt_profile": {"genome_survey": {"heterozygosity": 0.012}},
        "rules": {"rules": []},
        "recommendation_policy": {
            "allowed_evidence_bases": [
                "case",
                "profile_fact",
                "skill_heuristic",
                "retry_failure",
                "catalog_default",
            ]
        },
        "adjustable_params": {
            "purge_level": {"level": 2, "default": 0},
            "kmer_length": {"level": 2, "default": 51},
        },
        "case_retrieval": _case_retrieval(evidence_cases),
        "evidence_cases": evidence_cases,
    }


def _seal_canonical(canonical: dict) -> dict:
    payload_hash = generate_payload_ref(canonical)
    return methods_evidence_module._seal_attested_hifiasm_methods_prompt_payload(
        canonical,
        prompt_payload_ref=payload_hash,
        prompt_payload_hash=payload_hash,
    )


def _canonical_prompt(*, cases: list[dict] | None = None) -> dict:
    return _seal_canonical(
        sanitize_hifiasm_methods_prompt_payload(_prompt_payload(cases=cases))
    )


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
                        "evidence_span_ids": ["methods.case_a"],
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
                    "supporting_span_ids": ["methods.case_a"],
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


def test_evidence_payload_uses_task2_projection_and_exact_excerpts():
    case = _case()
    cases = [case]
    original = deepcopy(cases)

    payload = build_hifiasm_evidence_payload(
        evidence_cases=cases,
        case_retrieval=_case_retrieval(cases),
    )

    assert cases == original
    assert payload["candidate_case_ids"] == ["case_a"]
    assert payload["effect_areas"] == list(HIFIASM_EFFECT_AREAS)
    assert payload["cases"][0]["source_excerpts"][0]["quote"].startswith("We used")
    assert "source_documents" not in payload["cases"][0]
    assert "private/body.md" not in json.dumps(payload)
    assert payload["budget"]["used"]["cases"] == 1


def test_task2_projection_rejects_unknown_nested_case_fields():
    case = _case()
    retrieval = _case_retrieval([case])
    case["source"]["debug_blob"] = "must fail closed"

    with pytest.raises(EvidenceCaseValidationError, match="Task 2 validation-owned attestation"):
        build_hifiasm_evidence_payload(
            evidence_cases=[case],
            case_retrieval=retrieval,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda case: case["source_excerpts"].__setitem__(
            0,
            {"span_id": "forged", "document": "methods", "quote": "Replacement quote."},
        ),
        lambda case: case["source_excerpts"].append(
            {"span_id": "added", "document": "methods", "quote": "Added quote."}
        ),
    ],
)
def test_forged_source_excerpts_fail_before_canonical_prompt(mutate):
    case = _case()
    mutate(case)

    with pytest.raises(EvidenceCaseValidationError, match="Task 2 validation-owned attestation"):
        sanitize_hifiasm_methods_prompt_payload(_prompt_payload(cases=[case]))


def test_canonical_prompt_omits_all_curation_audit_fields():
    case = _case(
        curation={
            "status": "approved",
            "curator": "CURATOR_SENTINEL",
            "reviewed_at": "REVIEWED_AT_SENTINEL",
            "notes": "CURATION_NOTES_SENTINEL",
        }
    )

    canonical = sanitize_hifiasm_methods_prompt_payload(_prompt_payload(cases=[case]))
    rendered = json.dumps(canonical, sort_keys=True)

    assert "curation" not in canonical["evidence_cases"][0]
    assert "CURATOR_SENTINEL" not in rendered
    assert "REVIEWED_AT_SENTINEL" not in rendered
    assert "CURATION_NOTES_SENTINEL" not in rendered


def test_evidence_payload_drops_whole_legal_spans_without_truncating_quotes():
    spans = [
        {"span_id": f"s{index}", "document": "methods", "quote": char * 2_000}
        for index, char in enumerate("abcd")
    ]
    case = _case(spans=spans, parameter_span_ids=["s3"])

    payload = build_hifiasm_evidence_payload(
        evidence_cases=[case],
        case_retrieval=_case_retrieval([case]),
    )

    spans = payload["cases"][0]["source_excerpts"]
    assert [span["span_id"] for span in spans] == ["s0", "s1", "s2"]
    assert [span["quote"] for span in spans] == ["a" * 2_000, "b" * 2_000, "c" * 2_000]
    assert payload["budget"]["omitted_spans"] == [
        {"case_id": "case_a", "span_id": "s3", "reason": "excerpt_chars_per_case"}
    ]
    assert payload["cases"][0]["intervention"]["parameters"]["purge_level"]["evidence_span_ids"] == []


def test_evidence_payload_enforces_span_and_total_excerpt_limits_in_stable_order():
    span_case = _case(
        "span_case",
        spans=[
            {"span_id": f"s{index}", "document": "methods", "quote": f"quote-{index:02d}"}
            for index in range(13)
        ],
    )
    full_cases = []
    for index in range(5):
        case = _case(
            f"case_{index}",
            spans=[
                {
                    "span_id": f"case_{index}.{span}",
                    "document": "methods",
                    "quote": chr(97 + span) * 2_000,
                }
                for span in range(3)
            ],
        )
        full_cases.append(case)

    span_payload = build_hifiasm_evidence_payload(
        evidence_cases=[span_case],
        case_retrieval=_case_retrieval([span_case]),
    )
    total_payload = build_hifiasm_evidence_payload(
        evidence_cases=full_cases,
        case_retrieval=_case_retrieval(full_cases),
    )

    assert len(span_payload["cases"][0]["source_excerpts"]) == 12
    assert span_payload["budget"]["omitted_spans"][-1]["reason"] == "span_limit_per_case"
    assert total_payload["budget"]["used"]["excerpt_chars"] == 24_000
    assert [item["reason"] for item in total_payload["budget"]["omitted_spans"][-3:]] == [
        "excerpt_chars_total",
        "excerpt_chars_total",
        "excerpt_chars_total",
    ]


def test_evidence_payload_records_case_limit_and_drops_serialized_tail_cases():
    cases = []
    for index in range(9):
        case = _case(
            f"case_{index}",
            spans=[],
            rationale_claims=[
                {
                    "claim_id": f"claim_{claim_index}",
                    "statement": f"{index}:{claim_index}:" + ("x" * 900),
                    "explicitness": "inferred",
                    "effect_area": "purging",
                    "evidence_span_ids": [],
                }
                for claim_index in range(8)
            ],
        )
        cases.append(case)
    candidate_ids = [case["case_id"] for case in cases]

    retrieval = _case_retrieval(cases)
    retrieval["evidence_case_ids"] = candidate_ids[:8]
    payload = build_hifiasm_evidence_payload(
        evidence_cases=cases[:8],
        case_retrieval=retrieval,
    )

    assert payload["budget"]["used"]["serialized_bytes"] <= HIFIASM_METHODS_EVIDENCE_SERIALIZED_BYTES
    omitted = payload["budget"]["omitted_cases"]
    assert omitted[-1] == {"case_id": "case_8", "reason": "case_limit"}
    assert any(item["reason"] == "serialized_evidence_bytes" for item in omitted[:-1])
    assert payload["candidate_case_ids"] == candidate_ids[: len(payload["cases"])]


def test_evidence_payload_rejects_case_outside_numeric_shortlist():
    case = _case("case_b")
    retrieval = _case_retrieval([case])
    retrieval["candidate_case_ids"] = ["case_a"]
    with pytest.raises(EvidenceCaseValidationError, match="candidate_case_ids differ"):
        build_hifiasm_evidence_payload(
            evidence_cases=[case],
            case_retrieval=retrieval,
        )


def test_task2_public_projection_cannot_recast_or_replace_task3_numeric_match():
    ranked = _case()

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        project_hifiasm_prompt_safe_case(
            ranked,
            numeric_match={"distance": 999.0},
        )
    with pytest.raises(EvidenceCaseValidationError, match="Task 2 validation-owned attestation"):
        project_hifiasm_prompt_safe_case(ranked)


def test_task4_rejects_reordered_ranked_cases_even_when_outer_ids_are_swapped_too():
    first = _case("case_a")
    second = _case("case_b")
    prompt = _prompt_payload(cases=[first, second])
    prompt["evidence_cases"] = [second, first]
    prompt["case_retrieval"]["candidate_case_ids"] = ["case_b", "case_a"]
    prompt["case_retrieval"]["selected_case_ids"] = ["case_b", "case_a"]
    prompt["case_retrieval"]["evidence_case_ids"] = ["case_b", "case_a"]

    with pytest.raises(EvidenceCaseValidationError, match="candidate_case_ids differ"):
        sanitize_hifiasm_methods_prompt_payload(prompt)


def test_task4_rejects_changed_evidence_id_order_before_budgeting():
    prompt = _prompt_payload(cases=[_case("case_a"), _case("case_b")])
    prompt["case_retrieval"]["evidence_case_ids"] = ["case_b", "case_a"]

    with pytest.raises(EvidenceCaseValidationError, match="evidence_case_ids differ"):
        sanitize_hifiasm_methods_prompt_payload(prompt)


def test_task4_rejects_empty_attested_prefix_before_budgeting():
    prompt = _prompt_payload(cases=[_case("case_a"), _case("case_b")])
    prompt["evidence_cases"] = []
    prompt["case_retrieval"]["evidence_case_ids"] = []

    with pytest.raises(EvidenceCaseValidationError, match="complete attested shortlist prefix"):
        sanitize_hifiasm_methods_prompt_payload(prompt)


def test_task4_rejects_exchanged_attested_prefix_before_budgeting():
    first = _case("case_a")
    second = _case("case_b")
    prompt = _prompt_payload(cases=[first, second])
    prompt["evidence_cases"] = [second, first]
    prompt["case_retrieval"]["evidence_case_ids"] = ["case_b", "case_a"]

    with pytest.raises(EvidenceCaseValidationError, match="shortlist order"):
        sanitize_hifiasm_methods_prompt_payload(prompt)


def test_task3_attestation_rejects_non_contiguous_candidate_rank():
    case = _case(
        numeric_match={"distance": 0.25, "weak_evidence": False, "rank_numeric": 2}
    )

    with pytest.raises(EvidenceCaseValidationError, match="candidate ranks"):
        _case_retrieval([case])


def test_task4_validates_unattested_tail_entries_that_would_be_budget_omitted():
    cases = [_case(f"case_{index}") for index in range(9)]
    prompt = _prompt_payload(cases=cases)
    prompt["evidence_cases"][-1] = dict(prompt["evidence_cases"][-1])

    with pytest.raises(EvidenceCaseValidationError, match="Task 2 validation-owned attestation"):
        sanitize_hifiasm_methods_prompt_payload(prompt)


def test_legitimate_ranked_selection_survives_deepcopy_into_task4():
    prompt = deepcopy(_prompt_payload(cases=[_case("case_a"), _case("case_b")]))

    canonical = sanitize_hifiasm_methods_prompt_payload(prompt)

    assert canonical["case_retrieval"]["candidate_case_ids"] == ["case_a", "case_b"]
    assert [case["case_id"] for case in canonical["evidence_cases"]] == ["case_a", "case_b"]


def test_closed_prompt_projection_drops_alternate_containers_and_debug_payloads():
    prompt = _prompt_payload()
    prompt.update(
        {
            "selected_cases": [{"case_id": "shadow", "source_documents": ["secret"]}],
            "methods_evidence": {
                "effect_areas": ["forged"],
                "debug_blob": {"secret": "methods"},
            },
            "retry_evidence": {"signals": [{"signal_id": "secret", "raw_log": "leak"}]},
            "source_documents": [{"body": "top-level secret"}],
            "debug_blob": {"secret": "top-level"},
        }
    )
    prompt["case_retrieval"].update(
        {
                "query": {"raw": "secret query"},
                "trace": [{"raw": "secret trace"}],
                "debug_blob": {"raw": "secret debug"},
                "hifiasm_numeric_gate": {
                "production_cutover_enabled": True,
                "reason": {"raw": "secret gate"},
            },
        }
    )

    canonical = sanitize_hifiasm_methods_prompt_payload(prompt)
    rendered = json.dumps(canonical, sort_keys=True)

    assert canonical["selected_cases"] == []
    assert set(canonical) <= {
        "hifiasm_methods_prompt_schema_version",
        "method_id",
        "task",
        "prompt_profile",
        "rules",
        "recommendation_policy",
        "adjustable_params",
        "selected_cases",
        "case_selection",
        "case_retrieval",
        "evidence_cases",
        "methods_evidence",
    }
    assert "skill_heuristic" not in canonical["recommendation_policy"]["allowed_evidence_bases"]
    for leaked in (
        "secret query",
        "secret trace",
        "secret debug",
        "secret gate",
        "top-level secret",
        "raw_log",
    ):
        assert leaked not in rendered
    assert canonical["methods_evidence"]["effect_areas"] == list(HIFIASM_EFFECT_AREAS)


def test_canonical_prompt_is_idempotent_with_omitted_spans_and_stable_hash():
    case = _case(
        spans=[
            {"span_id": f"s{index}", "document": "methods", "quote": char * 2_000}
            for index, char in enumerate("abcd")
        ],
        parameter_span_ids=["s3"],
    )

    first = sanitize_hifiasm_methods_prompt_payload(_prompt_payload(cases=[case]))
    first_hash = generate_payload_ref(
        {
            key: value
            for key, value in first.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )
    second = sanitize_hifiasm_methods_prompt_payload(first)
    second_hash = generate_payload_ref(
        {
            key: value
            for key, value in second.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    assert first["hifiasm_methods_prompt_schema_version"] == HIFIASM_METHODS_PROMPT_SCHEMA_VERSION
    assert first["methods_evidence"]["evidence_budget"]["omitted_spans"] == [
        {"case_id": "case_a", "span_id": "s3", "reason": "excerpt_chars_per_case"}
    ]
    assert first["evidence_cases"][0]["intervention"]["parameters"]["purge_level"][
        "evidence_span_ids"
    ] == []
    assert second == first
    assert second_hash == first_hash


def test_self_signed_canonical_prompt_cannot_replace_task2_quote_provenance():
    canonical = _canonical_prompt()
    assert not hasattr(methods_evidence_module, "_attest_canonical_prompt")
    forged = json.loads(json.dumps(canonical))
    original_budget = deepcopy(forged["methods_evidence"]["evidence_budget"])
    quote = forged["evidence_cases"][0]["source_excerpts"][0]["quote"]
    forged["evidence_cases"][0]["source_excerpts"][0]["quote"] = "X" + quote[1:]
    forged["prompt_payload_hash"] = generate_payload_ref(
        {
            key: value
            for key, value in forged.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    assert len(forged["evidence_cases"][0]["source_excerpts"][0]["quote"]) == len(quote)
    assert forged["methods_evidence"]["evidence_budget"] == original_budget
    with pytest.raises(ValueError, match="validation-owned attestation"):
        sanitize_hifiasm_methods_prompt_payload(forged)


def test_self_signed_canonical_prompt_cannot_forge_omission_audit():
    case = _case(
        spans=[
            {"span_id": f"s{index}", "document": "methods", "quote": char * 2_000}
            for index, char in enumerate("abcd")
        ]
    )
    forged = json.loads(json.dumps(_canonical_prompt(cases=[case])))
    original_usage = deepcopy(forged["methods_evidence"]["evidence_budget"]["used"])
    omitted = forged["methods_evidence"]["evidence_budget"]["omitted_spans"]
    assert omitted == [
        {"case_id": "case_a", "span_id": "s3", "reason": "excerpt_chars_per_case"}
    ]
    omitted[0]["span_id"] = "x3"
    forged["prompt_payload_hash"] = generate_payload_ref(
        {
            key: value
            for key, value in forged.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    assert forged["methods_evidence"]["evidence_budget"]["used"] == original_usage
    with pytest.raises(ValueError, match="validation-owned attestation"):
        sanitize_hifiasm_methods_prompt_payload(forged)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda prompt: prompt.update({"debug_blob": "bypass"}),
        lambda prompt: prompt["case_retrieval"].update({"debug_blob": "bypass"}),
        lambda prompt: prompt["methods_evidence"]["evidence_budget"]["used"].update(
            {"cases": 99}
        ),
        lambda prompt: prompt.update({"selected_cases": [{"case_id": "bypass"}]}),
        lambda prompt: prompt.update({"hifiasm_methods_prompt_schema_version": True}),
        lambda prompt: prompt.update({"prompt_payload_hash": "0" * 64}),
    ],
)
def test_marked_canonical_prompt_is_strictly_validated(mutate):
    canonical = _canonical_prompt()
    mutate(canonical)

    with pytest.raises((ValueError, EvidenceCaseValidationError)):
        sanitize_hifiasm_methods_prompt_payload(canonical)


def test_methods_response_normalizes_assessments_and_parameter_decisions():
    normalized = normalize_hifiasm_methods_response(_response(), _canonical_prompt())

    assert normalized["params"] == {"purge_level": 1}
    assert normalized["parameter_evidence"]["purge_level"]["case_ids"] == ["case_a"]
    assert normalized["case_assessments"][0]["accepted_as_support"] is True


def test_methods_response_rejects_legacy_params_only_contract():
    with pytest.raises(ValueError, match="case_assessments"):
        normalize_hifiasm_methods_response({"params": {"purge_level": 1}}, _canonical_prompt())


def test_methods_response_requires_one_decision_for_every_l2_parameter():
    response = _response()
    response["recommendation"]["adjustable_params"].pop("kmer_length")

    with pytest.raises(ValueError, match="exactly one decision"):
        normalize_hifiasm_methods_response(response, _canonical_prompt())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda response: response["case_assessments"][0].update({"case_id": "unknown"}), "shortlist"),
        (
            lambda response: response["case_assessments"][0]["transferable_findings"][0].update(
                {"effect_area": "invented"}
            ),
            "effect_area",
        ),
        (
            lambda response: response["recommendation"]["adjustable_params"]["purge_level"].update(
                {"supporting_span_ids": ["missing"]}
            ),
            "non-visible span",
        ),
    ],
)
def test_methods_response_rejects_out_of_scope_evidence(mutate, message):
    response = _response()
    mutate(response)

    with pytest.raises(ValueError, match=message):
        normalize_hifiasm_methods_response(response, _canonical_prompt())


def test_hard_rule_ids_cannot_be_used_as_skill_heuristics():
    pack = load_knowledge_pack("parameter_recommender")
    hard_rule_id = pack["rules"]["rules"][0]["rule_id"]
    prompt = _canonical_prompt()
    prompt["rules"] = pack["rules"]
    response = _response()
    decision = response["recommendation"]["adjustable_params"]["purge_level"]
    decision["basis"] = "skill_heuristic"
    decision["supporting_case_ids"] = []
    decision["supporting_span_ids"] = []
    decision["heuristic_labels"] = [hard_rule_id]
    response["case_assessments"] = []
    response["selected_case_ids"] = []

    with pytest.raises(ValueError, match="structured prompt-visible heuristic IDs"):
        normalize_hifiasm_methods_response(response, prompt)


def test_retry_failure_basis_fails_closed_without_structured_retry_evidence():
    prompt = _canonical_prompt()
    prompt["task"] = "retry_after_execution_failure"
    response = _response()
    decision = response["recommendation"]["adjustable_params"]["purge_level"]
    decision["bases"] = ["case", "retry_failure"]
    decision["retry_signals"] = ["previous_attempt.failure_class"]

    with pytest.raises(ValueError, match="absent from structured prompt evidence"):
        normalize_hifiasm_methods_response(response, prompt)


def test_no_case_response_can_use_profile_fact_and_catalog_default():
    prompt = _canonical_prompt(cases=[])
    response = {
        "case_assessments": [],
        "recommendation": {
            "adjustable_params": {
                "purge_level": {
                    "action": "set",
                    "value": 1,
                    "basis": "profile_fact",
                    "fact_paths": ["genome_survey.heterozygosity"],
                    "supporting_case_ids": [],
                    "supporting_span_ids": [],
                    "confidence": "medium",
                    "reason": "Use the readonly heterozygosity fact conservatively.",
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
    prompt = _canonical_prompt(cases=[_case(), _case("case_b")])
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
    decision = response["recommendation"]["adjustable_params"]["purge_level"]
    decision["supporting_case_ids"] = ["case_a", "case_b"]

    with pytest.raises(ValueError, match="cases without a span.*case_b"):
        normalize_hifiasm_methods_response(response, prompt)


def test_decision_boundary_uses_the_existing_canonical_prompt_without_rebuilding(monkeypatch):
    raw_prompt = _prompt_payload(cases=[_case(spans=[
        {"span_id": f"s{index}", "document": "methods", "quote": char * 2_000}
        for index, char in enumerate("abcd")
    ])])
    canonical = _seal_canonical(sanitize_hifiasm_methods_prompt_payload(raw_prompt))
    expected_budget = deepcopy(canonical["methods_evidence"]["evidence_budget"])
    captured = {}
    response = _response()
    response["case_assessments"] = []
    response["selected_case_ids"] = []
    purge = response["recommendation"]["adjustable_params"]["purge_level"]
    purge.update(
        {
            "action": "keep_default",
            "value": 0,
            "basis": "catalog_default",
            "supporting_case_ids": [],
            "supporting_span_ids": [],
            "confidence": "weak",
        }
    )

    class _Response:
        content = json.dumps(response)

    class _Model:
        def invoke(self, messages):
            captured["messages"] = messages
            return _Response()

    monkeypatch.setattr("app.stages.assembly.decision.build_chat_model", lambda: _Model())

    request = AssemblyDecisionRequest(
        kind="parameter_recommendation",
        session_id="s1",
        prompt_contract="hifiasm_methods_v1",
        prompt_payload=canonical,
        system_prompt="Return JSON.",
    )
    assert request.prompt_payload is canonical

    result = make_assembly_decision(request)

    rendered = json.loads(captured["messages"][1]["content"])
    assert result.valid is True
    assert rendered == canonical
    assert "prompt_contract" not in rendered
    assert rendered["methods_evidence"]["evidence_budget"] == expected_budget


def test_decision_request_rejects_json_roundtrip_with_recomputed_self_hash():
    forged = json.loads(json.dumps(_canonical_prompt()))
    quote = forged["evidence_cases"][0]["source_excerpts"][0]["quote"]
    forged["evidence_cases"][0]["source_excerpts"][0]["quote"] = "X" + quote[1:]
    forged["prompt_payload_hash"] = generate_payload_ref(
        {
            key: value
            for key, value in forged.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    with pytest.raises(ValueError, match="validation-owned attestation"):
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="s1",
            prompt_contract="hifiasm_methods_v1",
            prompt_payload=forged,
            system_prompt="Return JSON.",
        )


def test_decision_request_rejects_json_roundtrip_downgraded_by_mutable_markers():
    forged = json.loads(json.dumps(_canonical_prompt()))
    forged.pop("hifiasm_methods_prompt_schema_version")
    forged["case_retrieval"]["retrieval_backend"] = "structured_default"
    forged["case_selection"]["retrieval_backend"] = "structured_default"
    forged["prompt_payload_hash"] = generate_payload_ref(
        {
            key: value
            for key, value in forged.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    with pytest.raises(ValueError, match="validation-owned attestation"):
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="s1",
            prompt_contract="hifiasm_methods_v1",
            prompt_payload=forged,
            system_prompt="Return JSON.",
        )


def test_explicit_hifiasm_contract_rejects_no_case_prompt_with_all_recognizers_removed():
    canonical = _seal_canonical(
        sanitize_hifiasm_methods_prompt_payload(_prompt_payload(cases=[]))
    )
    forged = json.loads(json.dumps(canonical))
    for field in (
        "hifiasm_methods_prompt_schema_version",
        "methods_evidence",
        "evidence_payload_ref",
        "evidence_payload_hash",
        "evidence_cases",
    ):
        forged.pop(field, None)
    for field in ("case_retrieval", "case_selection"):
        retrieval = forged[field]
        retrieval["retrieval_backend"] = "structured_default"
        for recognizer in (
            "evidence_case_ids",
            "evidence_payload_ref",
            "evidence_payload_hash",
            "hifiasm_numeric_gate",
        ):
            retrieval.pop(recognizer, None)
    forged["prompt_payload_hash"] = generate_payload_ref(
        {
            key: value
            for key, value in forged.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    with pytest.raises(ValueError, match="validation-owned attestation"):
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="s1",
            prompt_contract="hifiasm_methods_v1",
            prompt_payload=forged,
            system_prompt="Return JSON.",
        )


def test_decision_request_requires_explicit_prompt_contract():
    with pytest.raises(ValueError, match="prompt_contract"):
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="s1",
            prompt_payload={"method_id": "hifiasm"},
            system_prompt="Return JSON.",
        )


def test_explicit_legacy_contract_remains_compatible_and_stays_out_of_model_json(
    monkeypatch,
):
    prompt_payload = {
        "method_id": "hifiasm",
        "case_selection": {
            "retrieval_backend": "structured_default",
            "hifiasm_numeric_gate": {
                "production_cutover_enabled": False,
                "decision": "structured_default",
                "reason": "production_cutover_disabled",
            },
        },
    }
    captured = {}

    class _Response:
        content = json.dumps({"params": {}, "reasoning": "legacy", "confidence": 0.5})

    class _Model:
        def invoke(self, messages):
            captured["messages"] = messages
            return _Response()

    monkeypatch.setattr("app.stages.assembly.decision.build_chat_model", lambda: _Model())
    result = make_assembly_decision(
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="s1",
            prompt_contract="legacy",
            prompt_payload=prompt_payload,
            system_prompt="Return JSON.",
        )
    )

    assert result.valid is True
    assert json.loads(captured["messages"][1]["content"]) == prompt_payload
    assert "prompt_contract" not in json.loads(captured["messages"][1]["content"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda prompt: prompt.update({"prompt_payload_ref": "data/forged.json"}),
        lambda prompt: prompt.pop("prompt_payload_hash"),
    ],
    ids=["ref-tamper", "hash-delete"],
)
def test_decision_rejects_mutated_final_envelope_fields_before_model(
    monkeypatch,
    mutate,
):
    canonical = _canonical_prompt()
    request = AssemblyDecisionRequest(
        kind="parameter_recommendation",
        session_id="s1",
        prompt_contract="hifiasm_methods_v1",
        prompt_payload=canonical,
        system_prompt="Return JSON.",
    )
    mutate(canonical)
    monkeypatch.setattr(
        "app.stages.assembly.decision.build_chat_model",
        lambda: pytest.fail("mutated final envelope must not reach the model"),
    )

    with pytest.raises(ValueError, match="validation-owned attestation"):
        make_assembly_decision(request)


def test_decision_rechecks_attestation_before_model_serialization(monkeypatch):
    canonical = _canonical_prompt()
    request = AssemblyDecisionRequest(
        kind="parameter_recommendation",
        session_id="s1",
        prompt_contract="hifiasm_methods_v1",
        prompt_payload=canonical,
        system_prompt="Return JSON.",
    )
    quote = canonical["evidence_cases"][0]["source_excerpts"][0]["quote"]
    canonical["evidence_cases"][0]["source_excerpts"][0]["quote"] = "X" + quote[1:]
    canonical["prompt_payload_hash"] = generate_payload_ref(
        {
            key: value
            for key, value in canonical.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    monkeypatch.setattr(
        "app.stages.assembly.decision.build_chat_model",
        lambda: pytest.fail("model must not receive a mutated canonical prompt"),
    )
    with pytest.raises(ValueError, match="differs from its validation-owned attestation"):
        make_assembly_decision(request)


def test_decision_cannot_bypass_remembered_attestation_by_removing_markers(monkeypatch):
    canonical = _canonical_prompt()
    request = AssemblyDecisionRequest(
        kind="parameter_recommendation",
        session_id="s1",
        prompt_contract="hifiasm_methods_v1",
        prompt_payload=canonical,
        system_prompt="Return JSON.",
    )
    canonical.pop("hifiasm_methods_prompt_schema_version")
    canonical["case_retrieval"]["retrieval_backend"] = "structured_default"
    canonical["case_selection"]["retrieval_backend"] = "structured_default"
    canonical["prompt_payload_hash"] = generate_payload_ref(
        {
            key: value
            for key, value in canonical.items()
            if key not in {"prompt_payload_ref", "prompt_payload_hash"}
        }
    )

    monkeypatch.setattr(
        "app.stages.assembly.decision.build_chat_model",
        lambda: pytest.fail("removing mutable markers must not bypass the registry proof"),
    )
    with pytest.raises(ValueError, match="differs from its validation-owned attestation"):
        make_assembly_decision(request)


def test_decision_rechecks_attestation_before_response_validation(monkeypatch):
    canonical = _canonical_prompt()
    response = _response()

    class _Response:
        content = json.dumps(response)

    class _Model:
        def invoke(self, _messages):
            omitted = canonical["methods_evidence"]["evidence_budget"]["omitted_spans"]
            omitted.append(
                {"case_id": "case_a", "span_id": "forged", "reason": "span_limit"}
            )
            canonical["prompt_payload_hash"] = generate_payload_ref(
                {
                    key: value
                    for key, value in canonical.items()
                    if key not in {"prompt_payload_ref", "prompt_payload_hash"}
                }
            )
            return _Response()

    monkeypatch.setattr("app.stages.assembly.decision.build_chat_model", lambda: _Model())
    result = make_assembly_decision(
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="s1",
            prompt_contract="hifiasm_methods_v1",
            prompt_payload=canonical,
            system_prompt="Return JSON.",
        )
    )

    assert result.valid is False
    assert "validation-owned attestation" in (result.error or "")


def test_model_unavailable_fallback_validates_against_the_same_canonical_prompt(monkeypatch):
    raw_prompt = _prompt_payload(cases=[_case(spans=[
        {"span_id": f"s{index}", "document": "methods", "quote": char * 2_000}
        for index, char in enumerate("abcd")
    ])])
    canonical = _seal_canonical(sanitize_hifiasm_methods_prompt_payload(raw_prompt))
    original = deepcopy(canonical)
    captured = {}
    real_normalize = normalize_hifiasm_methods_response

    class _Model:
        def invoke(self, _messages):
            raise RuntimeError("offline")

    def _capture_normalize(response, prompt_payload):
        captured["prompt_payload"] = prompt_payload
        return real_normalize(response, prompt_payload)

    monkeypatch.setattr("app.stages.assembly.decision.build_chat_model", lambda: _Model())
    monkeypatch.setattr(
        "app.stages.assembly.decision.normalize_hifiasm_methods_response",
        _capture_normalize,
    )

    result = make_assembly_decision(
        AssemblyDecisionRequest(
            kind="parameter_recommendation",
            session_id="s1",
            prompt_contract="hifiasm_methods_v1",
            prompt_payload=canonical,
            system_prompt="Return JSON.",
            allow_default_on_error=True,
        )
    )

    assert result.valid is True
    assert result.error == "RuntimeError: offline"
    assert captured["prompt_payload"] == canonical
    assert captured["prompt_payload"]["methods_evidence"]["evidence_budget"] == original[
        "methods_evidence"
    ]["evidence_budget"]
    assert canonical == original


def test_model_unavailable_fallback_rejects_prompt_mutated_during_invoke(monkeypatch):
    canonical = _canonical_prompt()

    class _Model:
        def invoke(self, _messages):
            canonical["methods_evidence"]["evidence_budget"]["omitted_cases"].append(
                {"case_id": "forged", "reason": "serialized_evidence_bytes"}
            )
            canonical["prompt_payload_hash"] = generate_payload_ref(
                {
                    key: value
                    for key, value in canonical.items()
                    if key not in {"prompt_payload_ref", "prompt_payload_hash"}
                }
            )
            raise RuntimeError("offline after mutation")

    monkeypatch.setattr("app.stages.assembly.decision.build_chat_model", lambda: _Model())

    with pytest.raises(ValueError, match="validation-owned attestation"):
        make_assembly_decision(
            AssemblyDecisionRequest(
                kind="parameter_recommendation",
                session_id="s1",
                prompt_contract="hifiasm_methods_v1",
                prompt_payload=canonical,
                system_prompt="Return JSON.",
                allow_default_on_error=True,
            )
        )
