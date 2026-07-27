from __future__ import annotations

from copy import deepcopy

import pytest

from app.domains.assembly import case_selector as selector_module
from app.domains.assembly import numeric_case_retriever as retriever_module
from app.domains.assembly.case_selector import select_evidence_cases
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalGenomeFacts,
    CanonicalRecommendationFacts,
    QuantityFact,
    QuantityOrigin,
)


def _case(
    case_id: str,
    *,
    genome_size_mb: float,
    read_type: str,
    source_tool: str,
) -> dict:
    return {
        "schema_version": 3,
        "case_id": case_id,
        "case_type": "initial",
        "source": {"kind": "paper", "title": case_id},
        "applicability": {
            "read_type": read_type,
            "source_tool": source_tool,
            "target_methods": [source_tool],
            "assembly_goal": "documentary only",
        },
        "observed_facts": {
            "genome_size_mb": {"value": genome_size_mb},
        },
        "intervention": {"tool": source_tool, "parameters": {}},
        "rationale_claims": [],
        "outcomes": {},
        "source_excerpts": [],
        "retrieval_eligible": True,
        "curation": {"status": "approved"},
    }


def _facts(*, genome_size_bp: int | None = None) -> CanonicalRecommendationFacts:
    return CanonicalRecommendationFacts(
        genome=CanonicalGenomeFacts(
            effective_genome_size=(
                QuantityFact(
                    value=genome_size_bp,
                    unit="bp",
                    origin=QuantityOrigin(
                        kind="resolved",
                        producer="genome_survey",
                    ),
                )
                if genome_size_bp is not None
                else None
            )
        )
    )


def _context() -> AssemblyRecommendationContext:
    return AssemblyRecommendationContext(
        stage="assembly",
        assembly_route="hifi",
        read_type="pacbio_hifi",
        target_level="strict_t2t",
    )


@pytest.fixture(autouse=True)
def _accept_test_cards(monkeypatch):
    monkeypatch.setattr(
        selector_module,
        "validate_hifiasm_evidence_cases",
        lambda cases, pack_root=None: deepcopy(cases),
    )
    monkeypatch.setattr(
        retriever_module,
        "project_hifiasm_retrieval_case",
        deepcopy,
    )
    monkeypatch.setattr(
        retriever_module,
        "_rank_hifiasm_prompt_case",
        lambda case, numeric_match=None: {
            **deepcopy(case),
            "numeric_match": deepcopy(numeric_match),
        },
    )


def test_numeric_similarity_beats_method_and_read_type_metadata() -> None:
    close_mismatch = _case(
        "close_mismatch",
        genome_size_mb=1000,
        read_type="ont",
        source_tool="other_tool",
    )
    far_match = _case(
        "far_match",
        genome_size_mb=8000,
        read_type="hifi",
        source_tool="hifiasm",
    )
    selected, metadata = select_evidence_cases(
        [far_match, close_mismatch],
        method_id="hifiasm",
        task="recommend_initial",
        facts=_facts(genome_size_bp=1_000_000_000),
        context=_context(),
        max_cases=2,
    )

    assert [case["case_id"] for case in selected] == ["close_mismatch", "far_match"]
    assert metadata["selected_count"] == 2
    assert all("hard_filter_reasons" not in case["numeric_match"] for case in selected)


def test_method_without_paper_corpus_returns_empty_optional_selection() -> None:
    selected, metadata = select_evidence_cases(
        [],
        method_id="flye",
        task="recommend_initial",
        facts=_facts(),
        context=_context(),
    )

    assert selected == []
    assert metadata["evidence_available"] is False
    assert "no curated paper-card corpus" in metadata["warning"]


def test_retry_never_reselects_literature() -> None:
    with pytest.raises(ValueError, match="inherits attempt 0"):
        select_evidence_cases(
            [],
            method_id="hifiasm",
            task="retry_after_review_failure",
            facts=_facts(),
            context=_context(),
        )
