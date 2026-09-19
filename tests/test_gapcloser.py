"""TGS-GapCloser HiFi slice: biological validation and lifecycle boundaries."""

import gzip
import json
import os
import random
import shutil
import sys
from pathlib import Path

import pytest

from app.stages.gapcloser.validation import read_sequences, validate_gap_outputs


def _state(
    tmp_path: Path,
    platform: str = "pacbio_hifi",
    gaps: bool = True,
    second_platform: str | None = None,
    gzip_assembly: bool = False,
    truncated_reads: bool = False,
):
    from app.integrity import sha256_file
    from app.stages.gapcloser.nodes import METHOD
    from app.stages.resource_resolution import update_resource_ref
    from tests.test_assembly_dedup import AssemblyDedupStageTests

    fixture = AssemblyDedupStageTests()
    state = fixture._state(str(tmp_path))
    state["workflow_steps"][2].update(
        {
            "step_id": "step-002-gapcloser",
            "stage": "gapcloser",
            "selected_method_names": [METHOD],
        }
    )
    state["workflow_steps"][3]["review_target"]["producer_step_id"] = (
        "step-002-gapcloser"
    )
    state["current_stage"] = "gapcloser"
    reads = tmp_path / "reads with space.fq"
    sequence = "AAAATTCCCC"
    reads.write_text(f"@r1\n{sequence}\n+\n{'I' * len(sequence)}\n")
    if truncated_reads:
        reads.write_bytes(gzip.compress(reads.read_bytes())[:-5])
    read_files = [("hifi", reads, platform)]
    if second_platform is not None:
        second = tmp_path / "second.fq.gz"
        second.write_bytes(gzip.compress(reads.read_bytes()))
        read_files.append(("second", second, second_platform))
    fixture._publish_asset_registry(
        state,
        assets=[
            {
                "asset_id": member_id,
                "asset_type": "raw_long_reads",
                "path": str(path),
                "format": "fastq",
                "platform": member_platform,
                "checksum_status": "ready",
                "checksum": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for member_id, path, member_platform in read_files
        ],
    )
    draft = tmp_path / "source.fa"
    draft.write_text(">chr1\n" + ("AAAANNNNCCCC" if gaps else "AAAACCCC") + "\n")
    if gzip_assembly:
        draft.write_bytes(gzip.compress(draft.read_bytes()))
    bundle, primary = fixture._publish_candidate(
        state,
        candidate_id="source",
        method_name="assembly.candidate.hifiasm",
        draft=draft,
        member_id="hifi",
        platform=platform,
    )
    run = Path(state["data_dir"])
    state["current_stage"] = "assembly"
    state["workflow_step_index"] = 1
    state["resource_refs"] = update_resource_ref(
        state,
        "assembly",
        [str(run / primary["path_ref"])],
        "assembly",
        {"candidate_bundle_ref": bundle, "primary_output_ref": primary},
    )
    state["current_stage"] = "inventory"
    state["workflow_step_index"] = 0
    state["resource_refs"] = update_resource_ref(
        state,
        "long_reads",
        [str(path) for _, path, _ in read_files],
        "inventory",
        {
            "asset_ids": [member_id for member_id, _, _ in read_files],
            "asset_registry_ref": state["asset_registry_ref"],
        },
    )
    state["current_stage"] = "gapcloser"
    state["workflow_step_index"] = 2
    return state


def _fake_tool(tmp_path: Path) -> str:
    binary = tmp_path / "bin"
    binary.mkdir()
    tool = binary / "tgsgapcloser"
    tool.write_text(
        "#!/bin/sh\n"
        "test -s hifi.fasta || exit 3\n"
        "printf '>chr1\\nAAAATTCCCC\\n' > result.scaff_seqs\n"
        "printf '>chr1\\n1\\t4\\tS\\t1\\t4\\n5\\t6\\tF\\n"
        "7\\t10\\tS\\t9\\t12\\n' > result.gap_fill_detail\n"
    )
    tool.chmod(0o755)
    return str(binary) + os.pathsep + os.environ["PATH"]


@pytest.mark.parametrize(
    "decision", ["accept_candidate", "keep_prior_assembly", "stop_human_review"]
)
def test_execution_manual_adoption_and_delivery(tmp_path: Path, decision: str) -> None:
    from app.stages.gapcloser.builder import build_stage

    state = _state(tmp_path)
    prior = state["resource_refs"]["assembly"]
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": _fake_tool(tmp_path)}}}
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "blocked_human", result["stage_outputs"]
    assert result["resource_refs"]["assembly"] == prior
    _adopt_and_settle(result, decision)


def _adopt_and_settle(result: dict, decision: str) -> dict:
    from app.control.occurrence_settlement import settle_workflow_step_outcome
    from app.review.application import (
        load_verified_review_application,
        persist_review_application,
        persist_review_intent,
    )
    from app.review.base import ResumeReviewRequest
    from app.stages.gapcloser.review import apply_application, prepare_application
    from app.storage.ledger import Ledger

    prior = result["resource_refs"]["assembly"]
    interrupt = result["active_interrupt"]
    request = ResumeReviewRequest(
        session_id="s1",
        stage="gapcloser",
        gate_id="gapcloser_adoption",
        review_request_id=interrupt["review_request_id"],
        review_request_hash=interrupt["request_hash"],
        decision=decision,
    )
    intent = persist_review_intent(result, request, source="review")
    prepared = prepare_application(result, intent.intent)
    ref = persist_review_application(result, intent=intent, prepared=prepared)
    application = load_verified_review_application(result["data_dir"], ref)
    update = apply_application(result, application)
    applied = {**result, **update}
    settle_workflow_step_outcome(applied, data_dir=result["data_dir"])
    if decision == "stop_human_review":
        assert update["workflow_step_outcome"]["outcome"] == "failed"
        assert "resource_refs" not in update
    else:
        assert update["current_stage"] == "assembly_review"
        new_ref = update["resource_refs"]["assembly"]
        assert (new_ref["paths"] != prior["paths"]) == (decision == "accept_candidate")
        assert update["workflow_step_outcome"]["outcome"] == "completed"
        deliveries = [
            event
            for event in Ledger(result["data_dir"]).read_events("s1")
            if event["event_type"] == "resource_delivered"
        ]
        assert len(deliveries) == 1
        assert deliveries[0]["delivery_mode"] == (
            "materialized" if decision == "accept_candidate" else "reused"
        )
        from app.stages.assembly_review.nodes import resolve_candidates

        resolved = resolve_candidates(applied)
        candidates = resolved["stage_outputs"]["assembly_review"]["candidates"]
        assert (
            candidates[0]["primary_output_ref"]
            == new_ref["metadata"]["primary_output_ref"]
        )
    return applied


@pytest.mark.parametrize("platform", ["ont", "cycloneseq", "clr", "unknown"])
def test_noisy_or_unknown_reads_rejected(tmp_path: Path, platform: str) -> None:
    from app.stages.gapcloser.nodes import enter_gapcloser

    result = enter_gapcloser(_state(tmp_path, platform=platform))
    assert result["stage_outputs"]["gapcloser"]["status"] == "failed"


def test_no_gap_reuses_without_runner(tmp_path: Path) -> None:
    from app.stages.gapcloser.builder import build_stage

    state = _state(tmp_path, gaps=False)
    result = build_stage().invoke(state)
    assert result["current_stage"] == "assembly_review"
    assert (
        result["resource_refs"]["assembly"]["paths"]
        == state["resource_refs"]["assembly"]["paths"]
    )
    assert "execution_attempts" not in result["stage_outputs"]["gapcloser"]


def test_execution_requires_occurrence_consent(tmp_path: Path) -> None:
    from app.stages.gapcloser.builder import build_stage

    state = _state(tmp_path)
    state["workflow_steps"][2]["allow_local_execution"] = False
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "failed_terminal"
    assert "execution_attempts" not in result["stage_outputs"]["gapcloser"]


def test_gzip_assembly_alias_and_truncated_reads(tmp_path: Path) -> None:
    from app.stages.gapcloser.nodes import enter_gapcloser, freeze_gapcloser

    state = _state(tmp_path, gzip_assembly=True)
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": _fake_tool(tmp_path)}}}
    state.update(enter_gapcloser(state))
    state.update(freeze_gapcloser(state))
    scripts = list((Path(state["data_dir"]) / "work").rglob("run.sh"))
    assert len(scripts) == 1
    assert "--scaff assembly.fasta.gz" in scripts[0].read_text()

    broken = tmp_path / "broken"
    broken.mkdir()
    state = _state(broken, truncated_reads=True)
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": os.environ["PATH"]}}}
    state.update(enter_gapcloser(state))
    result = freeze_gapcloser(state)
    assert result["stage_outputs"]["gapcloser"]["status"] == "failed"
    assert "execution_attempts" not in result["stage_outputs"]["gapcloser"]


@pytest.mark.parametrize("second_platform", ["pacbio_hifi", "ont"])
def test_complete_read_set_normalization_or_rejection(
    tmp_path: Path, second_platform: str
) -> None:
    from app.stages.gapcloser.builder import build_stage

    state = _state(tmp_path, second_platform=second_platform)
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": _fake_tool(tmp_path)}}}
    result = build_stage().invoke(state)
    cursor = result["stage_outputs"]["gapcloser"]
    if second_platform != "pacbio_hifi":
        assert result["supervision_state"] == "failed_terminal"
        assert "execution_attempts" not in cursor
        return
    assert result["supervision_state"] == "blocked_human"
    root = Path(state["data_dir"])
    record = json.loads((root / cursor["input_record_ref"]["path_ref"]).read_text())
    assert [member["member_id"] for member in record["reads"]] == ["hifi", "second"]
    normalized = dict(
        read_sequences(root / record["normalized_reads_ref"]["path_ref"], "fasta")
    )
    assert normalized == {
        "member_0_read_0": "AAAATTCCCC",
        "member_1_read_0": "AAAATTCCCC",
    }


@pytest.mark.parametrize("corruption", ["hash", "output", "occurrence"])
def test_review_corruption_rejected_before_decision(
    tmp_path: Path, corruption: str
) -> None:
    from app.control.lifecycle import ResumeValidationError
    from app.review.application import persist_review_intent
    from app.review.base import ResumeReviewRequest
    from app.stages.gapcloser.builder import build_stage
    from app.storage.ledger import Ledger

    state = _state(tmp_path)
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": _fake_tool(tmp_path)}}}
    result = build_stage().invoke(state)
    interrupt = result["active_interrupt"]
    request = ResumeReviewRequest(
        session_id="s1",
        stage="gapcloser",
        gate_id="gapcloser_adoption",
        review_request_id=interrupt["review_request_id"],
        review_request_hash="0" * 64
        if corruption == "hash"
        else interrupt["request_hash"],
        decision="accept_candidate",
    )
    if corruption == "output":
        output = result["stage_outputs"]["gapcloser"]["output_ref"]
        (Path(state["data_dir"]) / output["path_ref"]).write_text(">chr1\nAAAA\n")
    elif corruption == "occurrence":
        interrupt["workflow_step_id"] = "step-999-gapcloser"
    with pytest.raises((ValueError, ResumeValidationError)):
        persist_review_intent(result, request, source="review")
    assert not any(
        event["event_type"] == "review_decision"
        for event in Ledger(state["data_dir"]).read_events("s1")
    )


def test_example_frozen_parent_run_no_gap(tmp_path: Path) -> None:
    result, _ = _run_frozen_example(tmp_path, "AAAACCCC", "AAAACCCC")
    assert result["supervision_state"] == "completed", result
    assert result["resource_refs"]["assembly"]["source_stage"] == "gapcloser"
    assert "execution_attempts" not in result["stage_outputs"]["gapcloser"]


def _run_frozen_example(tmp_path: Path, source: str, read: str) -> tuple[dict, Path]:
    import yaml

    from app.control.workflow_plan import workflow_steps_from_run_config
    from app.entrypoints.run import _stage_seeds_from_run_config
    from app.graph.builder import build_graph
    from app.schemas.state import initial_state
    from app.storage.frozen_inputs import freeze_session_inputs
    from app.storage.ledger import Ledger
    from app.storage.paths import SessionPaths

    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load(
        (root / "docs/examples/gapcloser_manifest.template.yaml").read_text()
    )
    draft = tmp_path / "assembly.fa"
    draft.write_text(f">chr1\n{source}\n")
    reads = tmp_path / "reads.fq.gz"
    reads.write_bytes(gzip.compress(f"@r1\n{read}\n+\n{'I' * len(read)}\n".encode()))
    manifest["session_id"] = "example-gapcloser"
    manifest["assets"][0]["paths"] = [str(draft)]
    manifest["assets"][1]["paths"] = [str(reads)]
    envs = tmp_path / "envs.yaml"
    envs.write_text(
        json.dumps(
            {
                "default": {"fallback_prefix": sys.prefix},
                "methods": {
                    "gapcloser.tgs_gapcloser": {
                        "conda_prefix": sys.prefix,
                        "required_tools": ["tgsgapcloser", "minimap2", "getopt"],
                    }
                },
            }
        )
    )
    paths = SessionPaths.from_run_dir(tmp_path / "runs" / manifest["session_id"])
    frozen = freeze_session_inputs(
        paths,
        manifest_draft=manifest,
        run_config_draft=root / "docs/examples/gapcloser_run_config.template.json",
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
    return result, paths.run_dir


def test_collect_replay_after_publication_does_not_run_again(tmp_path: Path) -> None:
    from app.stages.gapcloser.builder import build_stage
    from app.stages.gapcloser.nodes import collect_gapcloser

    state = _state(tmp_path)
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": _fake_tool(tmp_path)}}}
    result = build_stage().invoke(state)
    cursor = dict(result["stage_outputs"]["gapcloser"])
    expected = {
        key: cursor.pop(key) for key in ("output_ref", "details_ref", "validation_ref")
    }
    cursor["status"] = "gap_ran"
    replay = {**result, "stage_outputs": {"gapcloser": cursor}}
    collected = collect_gapcloser(replay)["stage_outputs"]["gapcloser"]
    assert all(collected[key] == value for key, value in expected.items())


def test_missing_details_fails_without_adoption(tmp_path: Path) -> None:
    from app.stages.gapcloser.builder import build_stage

    state = _state(tmp_path)
    path = _fake_tool(tmp_path)
    tool = tmp_path / "bin" / "tgsgapcloser"
    tool.write_text("#!/bin/sh\nprintf '>chr1\\nAAAATTCCCC\\n' > result.scaff_seqs\n")
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": path}}}
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "failed_terminal"
    assert result["resource_refs"] == state["resource_refs"]
    assert result.get("active_interrupt") is None


def test_real_tgs_gapcloser_hifi_smoke(tmp_path: Path) -> None:
    """Opt-in real binaries on a deterministic synthetic gap, not a real sample."""
    if os.environ.get("GENE_AGENT_TGS_REAL_SMOKE") != "1":
        pytest.skip(
            "set GENE_AGENT_TGS_REAL_SMOKE=1 with TGS-GapCloser/minimap2 on PATH"
        )
    assert shutil.which("tgsgapcloser") and shutil.which("minimap2")
    import argparse

    from app.control.lifecycle import inspect_session
    from app.entrypoints.run import _run_resume

    rng = random.Random(1847)
    truth = "".join(rng.choices("ACGT", k=10000))
    source = truth[:4500] + "N" * 1000 + truth[5500:]
    result, run_dir = _run_frozen_example(tmp_path, source, truth)
    assert result["supervision_state"] == "blocked_human", result["stage_outputs"]
    cursor = result["stage_outputs"]["gapcloser"]
    report = json.loads((run_dir / cursor["validation_ref"]["path_ref"]).read_text())
    assert report["input"]["internal_gaps"] == 1
    assert report["output"]["internal_gaps"] == 0
    assert report["filled_segments"] == 1
    fasta = run_dir / cursor["output_ref"]["path_ref"]
    assert dict(read_sequences(fasta, "fasta"))["chr1"] == truth
    interrupt = result["active_interrupt"]
    decision = tmp_path / "decision.json"
    decision.write_text(
        json.dumps(
            {
                "session_id": result["session_id"],
                "stage": "gapcloser",
                "gate_id": "gapcloser_adoption",
                "decision": "accept_candidate",
                "review_request_id": interrupt["review_request_id"],
                "review_request_hash": interrupt["request_hash"],
            }
        )
    )
    assert (
        _run_resume(
            argparse.Namespace(
                session_id=result["session_id"],
                decision_file=str(decision),
                data_dir=str(tmp_path),
                checkpointer="sqlite",
            )
        )
        == 0
    )
    summary = inspect_session(result["session_id"], run_dir)
    assert summary["supervision_state"] == "completed"
    assert summary["final_assembly"]["primary_output_path"] == str(fasta)


def test_frozen_closure_and_review_bind_to_gapcloser() -> None:
    from app.control.workflow_plan import workflow_steps_from_run_config
    from app.entrypoints.run import _stage_seeds_from_run_config
    from app.execution.run_readiness import effective_method_closure, run_requires_llm
    from app.schemas.run_config import RunConfig

    config = RunConfig.model_validate(
        {
            "requested_stages": [{"stage": "gapcloser", "allow_local_execution": True}],
            "gapcloser": {"threads": 3},
        }
    )
    assert effective_method_closure(config) == {
        "gapcloser": ("gapcloser.tgs_gapcloser",)
    }
    assert not run_requires_llm(config)
    assert (
        _stage_seeds_from_run_config(config, shared_parameters={})["gapcloser"][
            "threads"
        ]
        == 3
    )
    review = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "gapcloser", "allow_local_execution": True},
                {"stage": "assembly_review", "allow_local_execution": True},
            ]
        }
    )
    assert (
        workflow_steps_from_run_config(review)[-1]["review_target"]["producer_step_id"]
        == "step-001-gapcloser"
    )


def test_gap_details_validate_copied_and_filled_sequence(tmp_path: Path) -> None:
    source = tmp_path / "source.fa"
    output = tmp_path / "result.fa"
    details = tmp_path / "details"
    source.write_text(">chr1\nAAAANNNNCCCC\n")
    output.write_text(">chr1\nAAAATTCCCC\n")
    details.write_text(">chr1\n1\t4\tS\t1\t4\n5\t6\tF\n7\t10\tS\t9\t12\n")
    report = validate_gap_outputs(source, output, details)
    assert report["input"]["internal_gaps"] == 1
    assert report["output"]["internal_gaps"] == 0
    assert report["filled_segments"] == 1
    assert report["inserted_bases"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        ">chr1\n1\t4\tS\t1\t4\n6\t6\tF\n7\t10\tS\t9\t12\n",
        ">chr1\n1\t4\tS\t2\t5\n5\t6\tF\n7\t10\tS\t9\t12\n",
        ">chr2\n1\t10\tF\n",
        ">chr1\n1\t10\tN\n",
        ">chr1\n1\t10\tF\n",
    ],
)
def test_bad_details_fail_closed(tmp_path: Path, bad: str) -> None:
    source = tmp_path / "source.fa"
    output = tmp_path / "result.fa"
    details = tmp_path / "details"
    source.write_text(">chr1\nAAAANNNNCCCC\n")
    output.write_text(">chr1\nAAAATTCCCC\n")
    details.write_text(bad)
    with pytest.raises(ValueError):
        validate_gap_outputs(source, output, details)


def test_fastq_multiline_and_truncation(tmp_path: Path) -> None:
    path = tmp_path / "reads.fq"
    path.write_text("@r1\nAC\nGT\n+\nII\nII\n")
    assert list(read_sequences(path, "fastq")) == [("r1", "ACGT")]
    path.write_text("@r1\nACGT\n+\nIII\n")
    with pytest.raises(ValueError):
        list(read_sequences(path, "fastq"))


@pytest.mark.parametrize(
    "source,result,segments,removed",
    [
        ("AAAANNNNCCCC", "AAAANNNNCCCC", "1 4 S 1 4\n5 8 N\n9 12 S 9 12", 0),
        ("AAAANNNNCCCC", "AAAACCCC", "1 4 S 1 4\n5 8 S 9 12", 0),
        ("AAAANNNNCCCC", "AAACCCC", "1 3 S 1 3\n4 7 S 9 12", 1),
        ("AAAANNNNCCCC", "AAATTCCC", "1 3 S 1 3\n4 5 F\n6 8 S 10 12", 2),
        ("NNAAAANNNNCCCCNN", "NNAAAATTCCCCNN", "1 6 S 1 6\n7 8 F\n9 14 S 11 16", 0),
        (
            "NNAAAANNNNCCCCNN",
            "NNAAAANNNNCCCCNN",
            "1 2 N\n3 6 S 3 6\n7 10 N\n11 14 S 11 14\n15 16 N",
            0,
        ),
        (
            "AAAANNNNCCCCNNGGGG",
            "AAAATTCCCCNNGGGG",
            "1 4 S 1 4\n5 6 F\n7 10 S 9 12\n11 12 N\n13 16 S 15 18",
            0,
        ),
        ("NNNN", "NNNN", "1 4 N", 0),
        ("GGGGTTTT", "GGGGTTTT", "1 8 S 1 8", 0),
    ],
)
def test_gap_validation_preserves_source_outside_anchored_gaps(
    tmp_path, source, result, segments, removed
):
    original, output, details = [
        tmp_path / name for name in ("source", "out", "detail")
    ]
    original.write_text(f">chr1\n{source}\n")
    output.write_text(f">chr1\n{result}\n")
    details.write_text(f">chr1\n{segments}\n")
    report = validate_gap_outputs(original, output, details)
    assert report["removed_source_non_n_bases"] == removed


@pytest.mark.parametrize(
    "source,result,segments",
    [
        ("AAAANNNNCCCC", "NNNNNNNNNNNN", "1 12 N"),
        ("GGGGTTTT", "TTTT", "1 4 S 5 8"),
        ("AAAANNNNCCCC", "AAATTCCCC", "1 3 S 2 4\n4 5 F\n6 9 S 9 12"),
        ("AAAANNNNCCCC", "AAAATTCCC", "1 4 S 1 4\n5 6 F\n7 9 S 9 11"),
        ("AAAANNNNCCCC", "AAAANNNNNCCC", "1 4 S 1 4\n5 9 N\n10 12 S 10 12"),
        ("AAAANNNNCCCC", "AAAANNNCCCC", "1 4 S 1 4\n5 7 N\n8 11 S 9 12"),
        ("AAAANNNNCCCC", "AAANNNNCCCC", "1 2 S 1 2\n3 3 S 4 4\n4 7 N\n8 11 S 9 12"),
        (
            "AAAANNNNCCCC",
            "AATTANNNNCCCC",
            "1 2 S 1 2\n3 4 F\n5 5 S 4 4\n6 9 N\n10 13 S 9 12",
        ),
        ("AAAANNCCCCNNGGGG", "AAAATTGGGG", "1 4 S 1 4\n5 6 F\n7 10 S 13 16"),
        ("AAAANNCCCCNNGGGG", "AAAAGGGG", "1 4 S 1 4\n5 8 S 13 16"),
    ],
)
def test_gap_validation_rejects_unexplained_source_loss(
    tmp_path, source, result, segments
):
    original, output, details = [
        tmp_path / name for name in ("source", "out", "detail")
    ]
    original.write_text(f">chr1\n{source}\n")
    output.write_text(f">chr1\n{result}\n")
    details.write_text(f">chr1\n{segments}\n")
    with pytest.raises(ValueError):
        validate_gap_outputs(original, output, details)


def test_gap_closing_cannot_truncate_another_ungapped_scaffold(tmp_path):
    original, output, details = [
        tmp_path / name for name in ("source", "out", "detail")
    ]
    original.write_text(">chr1\nAAAANNNNCCCC\n>chr2\nGGGGTTTT\n")
    output.write_text(">chr1\nAAAATTCCCC\n>chr2\nTTTT\n")
    details.write_text(">chr1\n1 4 S 1 4\n5 6 F\n7 10 S 9 12\n>chr2\n1 4 S 5 8\n")
    with pytest.raises(ValueError, match="without internal N-gaps"):
        validate_gap_outputs(original, output, details)


def test_corrupt_all_n_result_fails_before_adoption_gate(tmp_path):
    from app.stages.gapcloser.builder import build_stage
    from app.storage.ledger import Ledger

    state = _state(tmp_path)
    path = _fake_tool(tmp_path)
    (tmp_path / "bin/tgsgapcloser").write_text(
        "#!/bin/sh\nprintf '>chr1\\nNNNNNNNNNNNN\\n' > result.scaff_seqs\n"
        "printf '>chr1\\n1 12 N\\n' > result.gap_fill_detail\n"
    )
    state["stage_outputs"] = {"gapcloser": {"env": {"PATH": path}}}
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "failed_terminal"
    assert result["resource_refs"] == state["resource_refs"]
    assert result.get("active_interrupt") is None
    assert not any(
        row["event_type"] in {"review_requested", "attempt_succeeded"}
        for row in Ledger(state["data_dir"]).read_events(state["session_id"])
    )
