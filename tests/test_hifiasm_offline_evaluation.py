"""Offline tests for the observational hifiasm initial evaluation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.domains.assembly.hifiasm_evaluation import EvaluationCorpusError, run_offline_evaluation

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "phase11" / "hifiasm_evaluation"
_CORPUS = _FIXTURE_ROOT / "corpus.yaml"
_KNOWLEDGE_ROOT = _FIXTURE_ROOT / "knowledge_root" / "assembly"


def _report(*, k: int = 3) -> dict:
    return run_offline_evaluation(_CORPUS, knowledge_root=_KNOWLEDGE_ROOT, k=k)


def _query(report: dict, query_id: str) -> dict:
    return next(item for item in report["queries"] if item["query_id"] == query_id)


def _candidate_trace(query_report: dict, case_id: str) -> dict:
    return next(
        item
        for item in query_report["curated"]["trace"]
        if item["case_id"] == case_id and item["decision"] == "candidate"
    )


def test_curated_fixture_uses_methods_only_v3_sources():
    pack_root = _KNOWLEDGE_ROOT / "parameter_recommender"
    shard = yaml.safe_load(
        (pack_root / "cases" / "hifiasm" / "initial.yaml").read_text(encoding="utf-8")
    )

    assert shard["cases"]
    for case in shard["cases"]:
        assert case["schema_version"] == 3
        assert "source_documents" not in case
        assert case["applicability"]["read_type"] == "hifi"
        assert case["applicability"]["target_methods"] == ["assembly.candidate.hifiasm"]
        assert case["evidence_spans"]

        methods_path = pack_root / case["methods_path"]
        methods_text = methods_path.read_bytes().decode("utf-8")
        assert methods_text.strip()
        assert not (methods_path.parent / "body.md").exists()

    first = shard["cases"][0]
    methods_text = (pack_root / first["methods_path"]).read_text(encoding="utf-8")
    assert first["evidence_spans"][0]["quote"] not in methods_text


def test_report_is_reproducible_and_has_no_cutover_or_gate_output():
    first = _report()
    second = _report()

    assert first == second
    assert first["observational_only"] is True
    assert first["activation_effect"] == "none"
    assert "cutover_permitted" not in first
    assert "corpus_gate" not in first
    assert "thresholds" not in first
    assert "safety" not in first
    assert all(len(value) == 64 for value in first["replay"].values())


def test_baseline_and_curated_initial_runs_share_query_and_requested_k():
    report = _report(k=3)
    query = _query(report, "initial_small")

    assert query["task"] == "recommend_initial"
    assert query["canonical_query"]["query_id"] == "initial_small"
    assert query["baseline"]["effective_k"] == 3
    assert query["curated"]["effective_k"] == 3
    assert query["baseline"]["selected_case_ids"][0] == "legacy_small"
    assert query["curated"]["selected_case_ids"][0] == "eval_initial_small"
    assert "recall_at_3" in query["baseline"]["metrics"]
    assert "recall_at_3" in query["curated"]["metrics"]


def test_old_coverage_boundary_remains_continuous():
    report = _report()
    below = _candidate_trace(_query(report, "initial_boundary_29_9"), "eval_initial_medium")
    above = _candidate_trace(_query(report, "initial_boundary_30_1"), "eval_initial_medium")

    assert abs(
        below["feature_distances"]["coverage"]["distance"]
        - above["feature_distances"]["coverage"]["distance"]
    ) < 0.001
    assert below["rank_numeric"] == above["rank_numeric"] == 1


def test_missing_values_are_observed_without_blocking_a_report():
    report = _report()
    query = _query(report, "initial_weak_missing")
    candidate = _candidate_trace(query, "eval_initial_large")

    assert candidate["evidence_coverage"] == 0.0
    assert candidate["weak_evidence"] is True
    assert query["curated"]["missingness_explicit"] is True
    assert query["curated"]["status"] == "complete"
    assert report["activation_effect"] == "none"


def test_leave_one_case_out_is_observational_and_replayable():
    query = _query(_report(), "initial_large_leave_one_out")

    assert query["held_out_case_ids"] == ["eval_initial_large", "legacy_large"]
    assert "legacy_large" not in query["baseline"]["selected_case_ids"]
    assert "eval_initial_large" not in query["curated"]["selected_case_ids"]
    assert query["baseline"]["metrics"]["held_out_case_selected_count"] == 0
    assert query["curated"]["metrics"]["held_out_case_selected_count"] == 0


def test_failing_observed_metric_never_becomes_an_activation_decision(tmp_path):
    corpus = yaml.safe_load(_CORPUS.read_text(encoding="utf-8"))
    corpus["queries"][0]["incompatible_case_ids"] = ["eval_initial_small"]
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.safe_dump(corpus, sort_keys=False), encoding="utf-8")

    report = run_offline_evaluation(path, knowledge_root=_KNOWLEDGE_ROOT, k=3)
    metric = _query(report, "initial_small")["curated"]["metrics"]

    assert metric["incompatible_case_leakage_rate"] > 0
    assert report["observational_only"] is True
    assert report["activation_effect"] == "none"


def test_evaluation_k_is_not_capped_by_descriptor_configuration():
    report = _report(k=2)

    assert report["requested_k"] == 2
    assert report["effective_k"] == 2
    assert len(_query(report, "initial_small")["curated"]["selected_case_ids"]) == 2


def test_retry_queries_are_rejected_from_initial_observational_corpus(tmp_path):
    corpus = yaml.safe_load(_CORPUS.read_text(encoding="utf-8"))
    corpus["queries"][0]["task"] = "retry_after_execution_failure"
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.safe_dump(corpus, sort_keys=False), encoding="utf-8")

    with pytest.raises(EvaluationCorpusError, match="recommend_initial only"):
        run_offline_evaluation(path, knowledge_root=_KNOWLEDGE_ROOT)


@pytest.mark.parametrize("obsolete_field", ["thresholds", "scientific_approval", "expansion_requirements"])
def test_obsolete_gate_fields_are_rejected(tmp_path, obsolete_field):
    corpus = yaml.safe_load(_CORPUS.read_text(encoding="utf-8"))
    corpus[obsolete_field] = {}
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.safe_dump(corpus, sort_keys=False), encoding="utf-8")

    with pytest.raises(EvaluationCorpusError, match="unknown fields"):
        run_offline_evaluation(path, knowledge_root=_KNOWLEDGE_ROOT)


def test_query_id_lists_are_typed_unique_and_known(tmp_path):
    corpus = yaml.safe_load(_CORPUS.read_text(encoding="utf-8"))
    corpus["queries"][0]["held_out_case_ids"] = ["ghost"]
    path = tmp_path / "corpus.yaml"
    path.write_text(yaml.safe_dump(corpus, sort_keys=False), encoding="utf-8")

    with pytest.raises(EvaluationCorpusError, match="absent from case corpus"):
        run_offline_evaluation(path, knowledge_root=_KNOWLEDGE_ROOT)
