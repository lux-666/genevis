"""Deterministic, observational evaluation of hifiasm initial retrieval."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml

from app.domains.assembly.case_selector import EvidenceBasedCaseSelector
from app.domains.assembly.evidence_case import (
    HIFIASM_EVIDENCE_SCHEMA_VERSION,
    validate_hifiasm_evidence_cases,
)
from app.domains.assembly.numeric_case_retriever import HifiasmNumericCaseRetriever
from app.domains.assembly.retrieval_query import build_hifiasm_retrieval_query
from app.knowledge.packs import load_knowledge_pack

_METHOD_ID = "assembly.candidate.hifiasm"
_TIERS = {"strong": 2.0, "acceptable": 1.0, "irrelevant": 0.0}


class EvaluationCorpusError(ValueError):
    """Raised when an offline evaluation corpus is malformed."""


def run_offline_evaluation(
    corpus_path: Path | str,
    *,
    knowledge_root: Path | str,
    k: int = 5,
) -> dict[str, Any]:
    """Compare initial selectors without producing an activation decision."""

    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer")
    path = Path(corpus_path)
    corpus = _load_corpus(path)
    pack = load_knowledge_pack(
        "parameter_recommender",
        root=Path(knowledge_root),
        required_files=("cases", "strategies"),
    )
    cases = pack["cases"]
    pack_root = Path(pack["pack_root"])
    legacy_cases = [
        case
        for case in cases
        if case.get("schema_version") != HIFIASM_EVIDENCE_SCHEMA_VERSION
        and not (case.get("features") or {}).get("failure_mode")
    ]
    validated_cases = validate_hifiasm_evidence_cases(cases, pack_root=pack_root)
    initial_cases = [case for case in validated_cases if case.get("case_type") == "initial"]
    _validate_query_case_universe(
        corpus["queries"],
        case_ids={str(case.get("case_id")) for case in cases},
    )

    strategy = pack.get("strategies") or {}
    numeric_config = dict(strategy.get("hifiasm_numeric") or {})
    numeric_config.pop("initial_cutover_enabled", None)
    baseline_selector = EvidenceBasedCaseSelector(min_cases=0, max_cases=k, strategy=strategy)
    curated_selector = HifiasmNumericCaseRetriever.from_config(
        numeric_config,
        max_cases=k,
    )

    query_reports: list[dict[str, Any]] = []
    for query in sorted(corpus["queries"], key=lambda item: item["query_id"]):
        held_out_ids = set(query.get("held_out_case_ids") or [])
        normalized_query = build_hifiasm_retrieval_query(
            query["prompt_profile"],
            method_id=_METHOD_ID,
            task="recommend_initial",
            query_id=query["query_id"],
        )
        baseline = baseline_selector.select(
            [case for case in legacy_cases if str(case.get("case_id")) not in held_out_ids],
            method_id=_METHOD_ID,
            task="recommend_initial",
            prompt_profile=_legacy_profile_from_canonical(normalized_query),
        )
        curated = curated_selector.select(
            [case for case in initial_cases if str(case.get("case_id")) not in held_out_ids],
            query=normalized_query,
        )
        query_reports.append(
            {
                "query_id": query["query_id"],
                "task": "recommend_initial",
                "canonical_query": normalized_query,
                "held_out_case_ids": sorted(held_out_ids),
                "baseline": _run_record(
                    backend="structured_default",
                    selected_ids=[str(case.get("case_id")) for case in baseline.selected_cases],
                    trace=baseline.trace,
                    all_cases=cases,
                    query=query,
                    k=k,
                ),
                "curated": {
                    **_run_record(
                        backend="hifiasm_numeric",
                        selected_ids=list(curated.metadata["selected_case_ids"]),
                        trace=curated.trace,
                        all_cases=cases,
                        query=query,
                        k=k,
                    ),
                    "warning": curated.metadata.get("warning"),
                    "missingness_explicit": _trace_has_explicit_missingness(curated.trace),
                },
            }
        )

    replay = {
        "corpus_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "strategy_sha256": _json_hash(strategy),
        "validated_initial_cases_sha256": _json_hash([dict(case) for case in initial_cases]),
        "legacy_initial_cases_sha256": _json_hash(legacy_cases),
        "query_input_sha256": _json_hash(
            [
                {
                    "query_id": item["query_id"],
                    "canonical_query": item["canonical_query"],
                    "held_out_case_ids": item["held_out_case_ids"],
                }
                for item in query_reports
            ]
        ),
    }
    return {
        "schema_version": 1,
        "corpus_id": corpus["corpus_id"],
        "observational_only": True,
        "activation_effect": "none",
        "requested_k": k,
        "effective_k": k,
        "replay": replay,
        "aggregate": {
            "baseline": _aggregate_backend(query_reports, "baseline"),
            "curated": _aggregate_backend(query_reports, "curated"),
        },
        "queries": query_reports,
    }


def _load_corpus(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise EvaluationCorpusError("evaluation corpus must be a mapping")
    allowed = {"schema_version", "corpus_id", "queries"}
    unknown = set(raw) - allowed
    if unknown:
        raise EvaluationCorpusError(f"evaluation corpus contains unknown fields: {sorted(unknown)}")
    if raw.get("schema_version") != 1:
        raise EvaluationCorpusError("evaluation corpus schema_version must be 1")
    if not isinstance(raw.get("corpus_id"), str) or not raw["corpus_id"]:
        raise EvaluationCorpusError("evaluation corpus requires corpus_id")
    queries = raw.get("queries")
    if not isinstance(queries, list) or not queries:
        raise EvaluationCorpusError("evaluation corpus requires a non-empty queries list")
    seen: set[str] = set()
    for index, query in enumerate(queries):
        _validate_query(query, index=index)
        if query["query_id"] in seen:
            raise EvaluationCorpusError(f"duplicate query_id: {query['query_id']}")
        seen.add(query["query_id"])
    return raw


def _validate_query(query: Any, *, index: int) -> None:
    if not isinstance(query, dict):
        raise EvaluationCorpusError(f"queries[{index}] must be a mapping")
    allowed = {
        "query_id",
        "task",
        "prompt_profile",
        "relevant_case_tiers",
        "incompatible_case_ids",
        "held_out_case_ids",
        "notes",
    }
    unknown = set(query) - allowed
    if unknown:
        raise EvaluationCorpusError(f"queries[{index}] contains unknown fields: {sorted(unknown)}")
    if not isinstance(query.get("query_id"), str) or not query["query_id"]:
        raise EvaluationCorpusError(f"queries[{index}].query_id is required")
    if query.get("task") != "recommend_initial":
        raise EvaluationCorpusError("offline evaluation supports recommend_initial only")
    if not isinstance(query.get("prompt_profile"), dict):
        raise EvaluationCorpusError(f"queries[{index}].prompt_profile must be a mapping")
    tiers = query.get("relevant_case_tiers")
    if not isinstance(tiers, dict) or set(tiers) - set(_TIERS):
        raise EvaluationCorpusError(f"queries[{index}].relevant_case_tiers is invalid")
    labels: list[str] = []
    for tier, ids in tiers.items():
        _validate_unique_string_list(ids, field_path=f"queries[{index}].relevant_case_tiers.{tier}")
        labels.extend(ids)
    if len(labels) != len(set(labels)):
        raise EvaluationCorpusError(f"queries[{index}].relevant_case_tiers must be mutually exclusive")
    for field in ("incompatible_case_ids", "held_out_case_ids"):
        _validate_unique_string_list(query.get(field) or [], field_path=f"queries[{index}].{field}")


def _validate_unique_string_list(value: Any, *, field_path: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise EvaluationCorpusError(f"{field_path} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise EvaluationCorpusError(f"{field_path} must contain unique values")


def _validate_query_case_universe(queries: list[dict[str, Any]], *, case_ids: set[str]) -> None:
    for query in queries:
        for field in ("incompatible_case_ids", "held_out_case_ids"):
            unknown = set(query.get(field) or []) - case_ids
            if unknown:
                raise EvaluationCorpusError(
                    f"{query['query_id']}: {field} contains IDs absent from case corpus: {sorted(unknown)}"
                )


def _run_record(
    *,
    backend: str,
    selected_ids: list[str],
    trace: list[dict[str, Any]],
    all_cases: list[dict[str, Any]],
    query: dict[str, Any],
    k: int,
) -> dict[str, Any]:
    selected = selected_ids[:k]
    return {
        "backend": backend,
        "status": "complete",
        "effective_k": k,
        "selected_case_ids": selected,
        "metrics": _retrieval_metrics(
            selected,
            tiers=query["relevant_case_tiers"],
            incompatible=set(query.get("incompatible_case_ids") or []),
            held_out=set(query.get("held_out_case_ids") or []),
            case_by_id={str(case.get("case_id")): case for case in all_cases},
            trace=trace,
            k=k,
        ),
        "trace": trace,
    }


def _retrieval_metrics(
    selected_ids: list[str],
    *,
    tiers: dict[str, list[str]],
    incompatible: set[str],
    held_out: set[str],
    case_by_id: dict[str, dict[str, Any]],
    trace: list[dict[str, Any]],
    k: int,
) -> dict[str, Any]:
    grades = {case_id: grade for tier, grade in _TIERS.items() for case_id in tiers.get(tier, [])}
    relevant = {case_id for case_id, grade in grades.items() if grade > 0}
    hits = set(selected_ids) & relevant
    dcg = sum(
        (2 ** grades.get(case_id, 0.0) - 1) / math.log2(rank + 1)
        for rank, case_id in enumerate(selected_ids, 1)
    )
    ideal = sorted((grade for grade in grades.values() if grade > 0), reverse=True)[:k]
    idcg = sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(ideal, 1))
    first_relevant = next((rank for rank, case_id in enumerate(selected_ids, 1) if case_id in relevant), None)
    coverages = [
        float(item["evidence_coverage"])
        for item in trace
        if item.get("decision") == "candidate" and isinstance(item.get("evidence_coverage"), (int, float))
    ]
    return {
        f"recall_at_{k}": len(hits) / len(relevant) if relevant else None,
        f"ndcg_at_{k}": dcg / idcg if idcg else None,
        "mean_reciprocal_rank": 1 / first_relevant if first_relevant else 0.0,
        "mean_evidence_coverage": sum(coverages) / len(coverages) if coverages else None,
        "incompatible_case_leakage_rate": (
            len(set(selected_ids) & incompatible) / len(selected_ids) if selected_ids else 0.0
        ),
        "unknown_selected_case_count": len([case_id for case_id in selected_ids if case_id not in case_by_id]),
        "held_out_case_selected_count": len(set(selected_ids) & held_out),
    }


def _aggregate_backend(reports: list[dict[str, Any]], backend_key: str) -> dict[str, Any]:
    rows = [report[backend_key]["metrics"] for report in reports]
    names = sorted({name for row in rows for name in row})
    means: dict[str, float | None] = {}
    for name in names:
        values = [float(row[name]) for row in rows if isinstance(row.get(name), (int, float))]
        means[name] = sum(values) / len(values) if values else None
    return {"completed_queries": len(rows), "mean_metrics": means}


def _trace_has_explicit_missingness(trace: list[dict[str, Any]]) -> bool:
    return any(
        feature.get("status") in {"missing_query_value", "missing_case_value"}
        for item in trace
        for feature in (item.get("feature_distances") or {}).values()
    )


def _legacy_profile_from_canonical(query: dict[str, Any]) -> dict[str, Any]:
    features = query.get("numeric_features") or {}

    def value(name: str) -> Any:
        record = features.get(name)
        return record.get("value") if isinstance(record, dict) else None

    genome_mb = value("genome_size_mb")
    read_n50_kb = value("read_n50_kb")
    return {
        "assets": {"read_type": query.get("read_type")},
        "sample": {
            "assembly_goal": query.get("assembly_goal"),
            "ploidy": value("ploidy"),
            "clade": (query.get("categorical_context") or {}).get("clade"),
        },
        "read_qc": {
            "effective_coverage_1kb": value("coverage"),
            "read_n50": read_n50_kb * 1_000 if read_n50_kb is not None else None,
        },
        "genome_survey": {
            "estimated_genome_size": genome_mb * 1_000_000 if genome_mb is not None else None,
            "heterozygosity": value("heterozygosity"),
        },
    }


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run observational hifiasm initial retrieval evaluation")
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--knowledge-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args(argv)
    rendered = json.dumps(
        run_offline_evaluation(args.corpus, knowledge_root=args.knowledge_root, k=args.k),
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
