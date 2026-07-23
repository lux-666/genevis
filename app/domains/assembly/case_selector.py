"""Literature-case selection for assembly parameter prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domains.assembly.evidence_case import validate_hifiasm_evidence_cases
from app.domains.assembly.numeric_case_retriever import HifiasmNumericCaseRetriever
from app.domains.assembly.retrieval_query import build_hifiasm_retrieval_query
from app.schemas.parameter_recommendation import (
    AssemblyRecommendationContext,
    CanonicalRecommendationFacts,
)

_LITERATURE_METHODS = {"hifiasm", "hifiasm_ont"}
_RETRY_TASKS = {
    "retry_after_execution_failure",
    "retry_after_review_failure",
}
_DEFAULT_TOP_K = 5


def select_evidence_cases(
    cases: list[dict[str, Any]],
    *,
    method_id: str,
    task: str,
    facts: CanonicalRecommendationFacts,
    context: AssemblyRecommendationContext,
    min_cases: int = 0,
    max_cases: int | None = None,
    strategy: dict[str, Any] | None = None,
    source_root: Path | str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank paper cards by numeric sample similarity without compatibility gates.

    Method, read type, route, assembly goal, and clade are intentionally neither
    filters nor score terms. They remain inside each card so the LLM can judge
    transferability while recommending the full parameter set.
    """

    del min_cases
    if task in _RETRY_TASKS:
        raise ValueError("retry inherits attempt 0 literature cards without reselection")
    if task != "recommend_initial":
        raise ValueError(f"unsupported assembly parameter task: {task!r}")

    method_name = _method_name(method_id)
    if method_name not in _LITERATURE_METHODS:
        return [], {
            "retrieval_backend": "numeric_literature",
            "selected_case_ids": [],
            "candidate_case_ids": [],
            "selected_count": 0,
            "eligible_case_count": 0,
            "total_cases": len(cases),
            "evidence_available": False,
            "warning": f"no curated paper-card corpus for {method_name}",
        }

    numeric_config = _numeric_config(strategy)
    retriever = HifiasmNumericCaseRetriever.from_config(
        numeric_config,
        max_cases=max_cases or _DEFAULT_TOP_K,
    )
    validated_cases = validate_hifiasm_evidence_cases(
        cases,
        pack_root=Path(source_root) if source_root is not None else None,
    )
    query = build_hifiasm_retrieval_query(
        facts,
        context,
        method_id=method_id,
        task=task,
    )
    result = retriever.select(validated_cases, query=query)
    metadata = result.metadata
    return result.selected_cases, {
        "retrieval_backend": "numeric_literature",
        "selected_case_ids": list(metadata.get("selected_case_ids") or []),
        "candidate_case_ids": list(metadata.get("candidate_case_ids") or []),
        "selected_count": int(metadata.get("selected_count") or 0),
        "eligible_case_count": metadata.get("eligible_case_count"),
        "total_cases": metadata.get("total_cases"),
        "evidence_available": bool(metadata.get("evidence_available")),
        "warning": metadata.get("warning"),
    }


def _numeric_config(strategy: dict[str, Any] | None) -> dict[str, Any] | None:
    if strategy is None:
        return None
    if not isinstance(strategy, dict):
        raise ValueError("strategy must be a mapping")
    raw = strategy.get("hifiasm_numeric")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("hifiasm_numeric strategy must be a mapping")
    return dict(raw)


def _method_name(method_id: Any) -> str:
    return str(method_id or "").rsplit(".", 1)[-1].lower().replace("-", "_")
