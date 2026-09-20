"""One correction attempt, paired evidence, durable adoption and final Review."""

import json
import os
from pathlib import Path

import pytest

from app.schemas.artifacts import ArtifactRef


def state_for_polish(tmp_path: Path, platform: str = "pacbio_hifi") -> dict:
    from tests.test_gapcloser import _state

    state = _state(tmp_path, platform=platform, gaps=False)
    state["workflow_steps"][2].update(
        {
            "step_id": "step-002-base_polish",
            "stage": "base_polish",
            "selected_method_names": ["base_polish.racon"],
        }
    )
    state["workflow_steps"][3]["review_target"]["producer_step_id"] = (
        "step-002-base_polish"
    )
    state["current_stage"] = "base_polish"
    return state


def fake_tools(tmp_path: Path, *, missing_after: bool = False) -> str:
    binary = tmp_path / "bin"
    binary.mkdir()
    mm = binary / "minimap2"
    mm.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then echo 2.28; exit; fi\n'
        'case "$*" in\n'
        "*candidate.fasta*) "
        + (
            "exit 0"
            if missing_after
            else "printf 'member_0_read_0\\t10\\t0\\t10\\t+\\tchr1\\t10\\t0\\t10"
            "\\t10\\t10\\t60\\ttp:A:P\\tcg:Z:10=\\n'"
        )
        + ";;\n"
        "*) printf 'member_0_read_0\\t10\\t0\\t10\\t+\\tchr1\\t8\\t0\\t8"
        "\\t8\\t10\\t60\\ttp:A:P\\tcg:Z:4=2I4=\\n';;\nesac\n"
    )
    racon = binary / "racon"
    racon.write_text(
        '#!/bin/sh\nif [ "$1" = --version ]; then echo 1.5.0; exit; fi\n'
        "printf '>chr1 LN:i:10\\nAAAATTCCCC\\n'\n"
    )
    for path in (mm, racon):
        path.chmod(0o755)
    return str(binary) + os.pathsep + os.environ["PATH"]


@pytest.mark.parametrize(
    "decision", ["accept_candidate", "keep_prior_assembly", "stop_human_review"]
)
def test_polish_compare_adopt_and_finish_review(tmp_path: Path, decision: str) -> None:
    from app.control.occurrence_settlement import settle_workflow_step_outcome
    from app.review.application import (
        load_verified_review_application,
        persist_review_application,
        persist_review_intent,
    )
    from app.review.base import ResumeReviewRequest
    from app.stages.assembly_review.nodes import resolve_candidates
    from app.stages.base_polish.builder import build_stage
    from app.stages.base_polish.review import apply_application, prepare_application

    state = state_for_polish(tmp_path)
    prior = state["resource_refs"]["assembly"]
    state["stage_outputs"] = {"base_polish": {"env": {"PATH": fake_tools(tmp_path)}}}
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "blocked_human", result["stage_outputs"]
    assert result["resource_refs"]["assembly"] == prior
    cursor = result["stage_outputs"]["base_polish"]
    from app.diagnostics.route_acceptance import execution_coverage
    from app.storage.artifacts import read_artifact_records
    from app.storage.ledger import Ledger

    methods, gaps = execution_coverage(
        result,
        Ledger(state["data_dir"]).read_event_rows("s1"),
        read_artifact_records(state["data_dir"], "s1"),
    )
    assert not gaps
    assert methods[0]["execution_verified"]
    report = json.loads(
        (Path(state["data_dir"]) / cursor["comparison_ref"]["path_ref"]).read_text()
    )
    assert report["before"]["alignments"]["base_identity"] == 0.8
    assert report["after"]["alignments"]["base_identity"] == 1.0
    assert report["independent_qv"] is None
    gate = result["active_interrupt"]
    request = ResumeReviewRequest(
        session_id="s1",
        stage="base_polish",
        gate_id="base_polish_adoption",
        review_request_id=gate["review_request_id"],
        review_request_hash=gate["request_hash"],
        decision=decision,
    )
    intent = persist_review_intent(result, request, source="review")
    prepared = prepare_application(result, intent.intent)
    ref = persist_review_application(result, intent=intent, prepared=prepared)
    application = load_verified_review_application(result["data_dir"], ref)
    result.update(apply_application(result, application))
    settle_workflow_step_outcome(result, data_dir=result["data_dir"])
    if decision == "stop_human_review":
        assert result["supervision_state"] == "failed_terminal"
        assert result["resource_refs"]["assembly"] == prior
        return
    assert (result["resource_refs"]["assembly"]["paths"] == prior["paths"]) == (
        decision == "keep_prior_assembly"
    )
    reviewed = resolve_candidates(result)["stage_outputs"]["assembly_review"]
    assert len(reviewed["candidates"]) == 2
    assert {
        c["primary_output_ref"]["artifact_type"] for c in reviewed["candidates"]
    } == {"assembly.draft_assembly", "base_polish.assembly"}
    from app.diagnostics.route_acceptance import final_comparison, verify_delivery
    from app.stages.assembly_review.builder import build_stage as build_review
    from app.storage.paths import SessionPaths
    from tests.test_assembly_review import AssemblyReviewStageTests

    fixture = AssemblyReviewStageTests()
    fixture._fake_quast_bin(str(tmp_path))
    fixture._fake_busco_bin(str(tmp_path))
    lineage = tmp_path / "embryophyta_odb10"
    lineage.mkdir()
    result["workflow_steps"][3]["allow_local_execution"] = True
    result["stage_outputs"]["assembly_review"] = {
        "execution_mode": "local",
        "related_species_reference_dir": str(fixture._test_reference_dir(result)),
        "env": {"PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"]},
        "quast_parameters": {"threads": 1},
        "busco_parameters": {
            "threads": 1,
            "lineage_path": str(lineage),
            "lineage_name": lineage.name,
            "lineage_version": "odb10",
            "lineage_source": "test_fixture",
        },
    }
    evaluated = build_review().invoke(result)
    assert evaluated["supervision_state"] == "blocked_human", evaluated
    signals = evaluated["stage_outputs"]["assembly_review"]["signals"]
    assert len(signals) == 4
    assert all(s["status"] == "available" for s in signals), signals
    paths = SessionPaths.from_run_dir(state["data_dir"])
    comparison = final_comparison(
        paths,
        read_artifact_records(paths.run_dir, "s1"),
        "step-003-assembly_review",
        ArtifactRef.model_validate(evaluated["active_interrupt"]["request_ref"]),
    )
    assert len(comparison["candidates"]) == 2
    rows = Ledger(paths.run_dir).read_event_rows("s1")
    delivery = next(r for r in rows if r["event_type"] == "resource_delivered")
    assert verify_delivery(paths, delivery)["delivery_mode"] == (
        "materialized" if decision == "accept_candidate" else "reused"
    )
    result["resource_refs"]["assembly"]["metadata"][
        "comparison_candidate_bundle_ref"
    ] = result["resource_refs"]["assembly"]["metadata"]["candidate_bundle_ref"]
    assert (
        resolve_candidates(result)["stage_outputs"]["assembly_review"]["status"]
        == "failed"
    )
    result["resource_refs"]["assembly"]["metadata"].pop(
        "comparison_candidate_bundle_ref"
    )
    assert (
        resolve_candidates(result)["stage_outputs"]["assembly_review"]["status"]
        == "failed"
    )


def test_missing_alignment_evidence_cannot_reach_adoption(tmp_path: Path) -> None:
    from app.stages.base_polish.builder import build_stage

    state = state_for_polish(tmp_path)
    state["stage_outputs"] = {
        "base_polish": {"env": {"PATH": fake_tools(tmp_path, missing_after=True)}}
    }
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "failed_terminal"
    assert result.get("active_interrupt") is None
    assert result["resource_refs"] == state["resource_refs"]


@pytest.mark.parametrize(
    "bad",
    ["identity", "cigar", "secondary", "length", "empty", "matches", "block"],
)
def test_invalid_paf_is_not_quality_evidence(tmp_path: Path, bad: str) -> None:
    from app.stages.base_polish.validation import alignment_summary

    fields = [
        "r1",
        "8",
        "0",
        "8",
        "+",
        "chr1",
        "8",
        "0",
        "8",
        "8",
        "8",
        "60",
        "cg:Z:8=",
    ]
    if bad == "identity":
        fields[0] = "unrelated_read"
    elif bad == "cigar":
        fields[-1] = "cg:Z:7="
    elif bad == "secondary":
        fields.append("tp:A:S")
    elif bad == "length":
        fields[6] = "9"
    elif bad == "matches":
        fields[-1] = "cg:Z:8X"
    elif bad == "block":
        fields[10] = "9"
    path = tmp_path / "alignment.paf"
    path.write_text("" if bad == "empty" else "\t".join(fields) + "\n")
    with pytest.raises(ValueError):
        alignment_summary(path, {"chr1": "AAAACCCC"}, {"r1": 8})


def test_paf_ambiguous_bases_are_not_counted_as_matches(tmp_path: Path) -> None:
    from app.stages.base_polish.validation import alignment_summary

    # Minimap2 emits '=' for N/N while excluding those columns from mlen/blen.
    path = tmp_path / "alignment.paf"
    path.write_text("r1\t10\t0\t10\t+\tchr1\t10\t0\t10\t8\t8\t60\tnn:i:2\tcg:Z:10=\n")
    report = alignment_summary(path, {"chr1": "AAAANNCCCC"}, {"r1": 10})
    assert report["matching_alignment_bases"] == 8
    assert report["alignment_block_bases"] == 8


def test_inconsistent_paf_counts_cannot_reach_adoption(tmp_path: Path) -> None:
    from app.stages.base_polish.builder import build_stage

    state = state_for_polish(tmp_path)
    state["stage_outputs"] = {"base_polish": {"env": {"PATH": fake_tools(tmp_path)}}}
    minimap = tmp_path / "bin" / "minimap2"
    minimap.write_text(minimap.read_text().replace("cg:Z:10=", "cg:Z:10X"))
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "failed_terminal"
    assert result.get("active_interrupt") is None
    assert result["resource_refs"] == state["resource_refs"]


def test_unchanged_assembly_is_valid_but_not_an_improvement_claim(
    tmp_path: Path,
) -> None:
    from app.stages.base_polish.validation import compare_outputs

    source, reads, paf = (
        tmp_path / name for name in ("source.fa", "reads.fa", "read.paf")
    )
    source.write_text(">chr1\nAAAACCCC\n")
    reads.write_text(">r1\nAAAACCCC\n")
    paf.write_text("r1\t8\t0\t8\t+\tchr1\t8\t0\t8\t8\t8\t60\tcg:Z:8=\n")
    report = compare_outputs(source, source, reads, paf, paf)
    assert report["changed_scaffolds"] == 0
    assert set(report["delta"].values()) == {0}
    assert report["independent_qv"] is None
    assert "improved" not in report


def test_final_quast_error_metrics_keep_missing_distinct_from_zero(
    tmp_path: Path,
) -> None:
    from app.stages.assembly_review.method_application.quast import (
        parse_quast_report_tsv,
    )

    report = tmp_path / "report.tsv"
    report.write_text(
        "Assembly\tassembly\n# misassemblies\t0\n"
        "# mismatches per 100 kbp\t2.5\n# indels per 100 kbp\t-\n"
        "Genome fraction (%)\t97.6\nDuplication ratio\t1.02\nNGA50\t1,000\n"
    )
    metrics = parse_quast_report_tsv(report)
    assert metrics["misassemblies"] == 0
    assert metrics["mismatches_per_100kbp"] == 2.5
    assert "indels_per_100kbp" not in metrics
    assert metrics["indels_per_100kbp_raw"] == "-"
    assert metrics["nga50"] == 1000


@pytest.mark.parametrize("platform", ["ont", "unknown", "clr", "cycloneseq"])
def test_noisy_platform_not_reclassified(tmp_path: Path, platform: str) -> None:
    from app.stages.base_polish.builder import build_stage

    result = build_stage().invoke(state_for_polish(tmp_path, platform))
    assert result["supervision_state"] == "failed_terminal"
    assert "execution_attempts" not in result["stage_outputs"]["base_polish"]


def test_collect_replay_recovers_all_products_without_running(tmp_path: Path) -> None:
    from app.stages.base_polish.builder import build_stage
    from app.stages.base_polish.nodes import collect_base_polish

    state = state_for_polish(tmp_path)
    state["stage_outputs"] = {"base_polish": {"env": {"PATH": fake_tools(tmp_path)}}}
    result = build_stage().invoke(state)
    cursor = dict(result["stage_outputs"]["base_polish"])
    expected = {
        key: cursor.pop(key)
        for key in ("output_ref", "comparison_ref", "polished_candidate_ref")
    }
    cursor["status"] = "polish_ran"
    replay = {**result, "stage_outputs": {"base_polish": cursor}}
    collected = collect_base_polish(replay)["stage_outputs"]["base_polish"]
    assert all(collected[key] == ref for key, ref in expected.items())


@pytest.mark.parametrize(
    "key", ["comparison_ref", "output_ref", "polished_candidate_ref"]
)
def test_corrupt_polish_evidence_rejected_before_decision(
    tmp_path: Path, key: str
) -> None:
    from app.review.application import persist_review_intent
    from app.review.base import ResumeReviewRequest
    from app.stages.base_polish.builder import build_stage
    from app.storage.ledger import Ledger

    state = state_for_polish(tmp_path)
    state["stage_outputs"] = {"base_polish": {"env": {"PATH": fake_tools(tmp_path)}}}
    result = build_stage().invoke(state)
    gate = result["active_interrupt"]
    request = ResumeReviewRequest(
        session_id="s1",
        stage="base_polish",
        gate_id="base_polish_adoption",
        review_request_id=gate["review_request_id"],
        review_request_hash=gate["request_hash"],
        decision="accept_candidate",
    )
    path = (
        Path(state["data_dir"])
        / result["stage_outputs"]["base_polish"][key]["path_ref"]
    )
    path.write_text("corrupt")
    with pytest.raises(ValueError):
        persist_review_intent(result, request, source="review")
    assert not any(
        r["event_type"] == "review_decision"
        for r in Ledger(state["data_dir"]).read_event_rows("s1")
    )


@pytest.mark.parametrize("decision", ["accept_candidate", "keep_prior_assembly"])
def test_sqlite_close_and_cli_resume(tmp_path: Path, decision: str) -> None:
    import argparse
    import shlex

    from app.control.lifecycle import inspect_session
    from app.control.workflow_plan import workflow_steps_from_run_config
    from app.entrypoints.run import _run_resume, _stage_seeds_from_run_config
    from app.graph.builder import build_graph
    from app.schemas.state import initial_state
    from app.storage.frozen_inputs import freeze_session_inputs
    from app.storage.ledger import Ledger
    from app.storage.paths import SessionPaths

    fake_tools(tmp_path)
    conda = tmp_path / "conda"
    conda.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        'case "$1" in run|--live-stream|--no-capture-output) shift;;\n'
        "-p|-n) shift 2;; *) break;; esac\ndone\n"
        f'export PATH={shlex.quote(str(tmp_path / "bin"))}:"$PATH"\n'
        'exec "$@"\n'
    )
    conda.chmod(0o755)
    source, reads = tmp_path / "source.fa", tmp_path / "reads.fa"
    source.write_text(">chr1\nAAAACCCC\n")
    reads.write_text(">r1\nAAAATTCCCC\n")
    envs = tmp_path / "envs.yaml"
    envs.write_text(
        json.dumps(
            {
                "default": {"conda_bin": str(conda), "fallback_prefix": str(tmp_path)},
                "methods": {
                    "base_polish.racon": {
                        "conda_prefix": str(tmp_path),
                        "required_tools": ["minimap2", "racon"],
                    }
                },
            }
        )
    )
    paths = SessionPaths.from_run_dir(tmp_path / "runs" / "polish-resume")
    frozen = freeze_session_inputs(
        paths,
        manifest_draft={
            "session_id": paths.session_id,
            "assets": [
                {
                    "asset_type": "assembly_fasta_registered",
                    "paths": [str(source)],
                    "format": "fasta",
                    "compression": "none",
                },
                {
                    "asset_type": "raw_long_reads",
                    "paths": [str(reads)],
                    "platform": "pacbio_hifi",
                    "format": "fasta",
                    "compression": "none",
                },
            ],
        },
        run_config_draft={
            "requested_stages": [
                {"stage": "base_polish", "allow_local_execution": True}
            ],
            "base_polish": {"threads": 1},
            "review_policy": {"max_blocked_seconds": -1},
        },
        config_path=envs,
        probe_environments=False,
    )
    Ledger(paths.run_dir).create_session(
        paths.session_id, paths.to_ref(paths.input_manifest_path)
    )
    state = initial_state(
        session_id=paths.session_id,
        input_manifest_ref=paths.to_ref(paths.input_manifest_path),
        data_dir=str(paths.run_dir),
        workflow_steps=workflow_steps_from_run_config(frozen.run_config),
        stage_seeds=_stage_seeds_from_run_config(
            frozen.run_config, shared_parameters={}
        ),
    )
    graph = build_graph(
        backend="sqlite", data_dir=str(paths.run_dir), verified_inputs=frozen
    )
    try:
        result = graph.invoke(state, {"configurable": {"thread_id": paths.session_id}})
    finally:
        graph.checkpointer.conn.close()
    assert result["supervision_state"] == "blocked_human", result
    gate = result["active_interrupt"]
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(
            {
                "session_id": paths.session_id,
                "stage": "base_polish",
                "gate_id": "base_polish_adoption",
                "decision": decision,
                "review_request_id": gate["review_request_id"],
                "review_request_hash": gate["request_hash"],
            }
        )
    )
    assert (
        _run_resume(
            argparse.Namespace(
                session_id=paths.session_id,
                decision_file=str(decision_file),
                data_dir=str(tmp_path),
                checkpointer="sqlite",
            )
        )
        == 0
    )
    summary = inspect_session(paths.session_id, paths.run_dir)
    assert summary["supervision_state"] == "completed"
    final = Path(summary["final_assembly"]["primary_output_path"])
    expected = "AAAATTCCCC" if decision == "accept_candidate" else "AAAACCCC"
    assert expected in final.read_text()
    assert (
        sum(
            r["event_type"] == "attempt_started"
            for r in Ledger(paths.run_dir).read_event_rows(paths.session_id)
        )
        == 1
    )
