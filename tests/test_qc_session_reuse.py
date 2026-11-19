"""A rejected Assembly session can supply QC to an independent run."""

import json
import os
from pathlib import Path

import pytest

import app.entrypoints.run as run
from app.control.stage_seeds import stage_seeds_from_run_config
from app.entrypoints.review_interactive import collect_gate_decision
from app.stages.read_qc.reuse import read_completed_qc_source
from app.stages.read_resolution import resolve_hic_pairs, resolve_long_read_members
from app.storage.artifacts import read_artifact_records
from app.storage.frozen_inputs import validate_frozen_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests import test_read_qc as qc_fixture
from tests.busco_fixture import ensure_test_host_envs


@pytest.mark.parametrize("filtered", [False, True])
def test_rerun_assembly_reuses_qc_from_blocked_or_rejected_source(
    tmp_path, monkeypatch, filtered
):
    root = str(tmp_path / "data")
    source_paths = SessionPaths(root, "source")
    reads = tmp_path / "cyclone.fq"
    r1, r2 = tmp_path / "hic_1.fq", tmp_path / "hic_2.fq"
    for path in (reads, r1, r2):
        path.write_text("@r\nACGT\n+\nIIII\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "session_id": "source",
                "sample_id": "sample",
                "genome_size_estimate": 100,
                "assets": [
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(reads)],
                        "platform": "cycloneseq",
                    },
                    {
                        "asset_type": "hic_reads_registered",
                        "platform": "illumina",
                        "paired_reads": [{"r1": str(r1), "r2": str(r2)}],
                    },
                ],
            }
        )
    )
    config = tmp_path / "run.json"
    config.write_text(
        json.dumps(
            {
                "requested_stages": [
                    {"stage": "read_qc", "allow_local_execution": True},
                    {"stage": "assembly", "allow_local_execution": True},
                ],
                "read_qc": {
                    "length_filter": {
                        "approved": filtered,
                        "thresholds": {"min_len": 2},
                    },
                    "hic_reads": {},
                },
            }
        )
    )
    helper = qc_fixture.ReadQCSeqKitStageTests()
    tool_path = os.pathsep.join(
        [
            str(helper._fake_seqkit_bin(str(tmp_path))),
            str(helper._fake_fastp_bin(str(tmp_path))),
            os.environ["PATH"],
        ]
    )
    monkeypatch.setenv("GENE_AGENT_ENVS_CONFIG", str(ensure_test_host_envs(tmp_path)))
    monkeypatch.setattr(run, "_foreground_review_available", lambda: True)
    monkeypatch.setattr(
        run, "_interactive_gate_loop", lambda _g, _c, result, *a, **kw: result
    )
    monkeypatch.setattr(
        "app.execution.run_readiness.preflight_run_readiness", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "app.execution.env_dispatch._has_frozen_input_contract", lambda _path: False
    )

    def seeds(run_config, **kwargs):
        result = stage_seeds_from_run_config(run_config, **kwargs)
        if run_config.read_qc.reuse is None:
            result["read_qc"]["env"] = {"PATH": tool_path}
        return result

    monkeypatch.setattr(run, "stage_seeds_from_run_config", seeds)
    advisor_facts = []

    class Advisor:
        def invoke(self, messages):
            advisor_facts.append(json.loads(messages[1]["content"])["facts"])
            return json.dumps(
                {
                    "recommended_route": "cyclone",
                    "recommended_read_type": "cyclone",
                    "recommended_method": "assembly.candidate.flye",
                    "reasoning": "fixture recommendation",
                    "uncertainty_notes": [],
                }
            )

    monkeypatch.setattr(
        "app.stages.assembly.decision.build_chat_model", lambda *a, **kw: Advisor()
    )
    assert (
        run.main(
            [
                "run",
                "--manifest",
                str(manifest),
                "--run-config",
                str(config),
                "--data-dir",
                root,
            ]
        )
        == 0
    )
    source_state, source_step = read_completed_qc_source(source_paths)
    assert source_state["supervision_state"] == "blocked_human"
    if filtered:
        answers = iter(["stop_human_review", ""])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        _, decision = collect_gate_decision("source", str(source_paths.run_dir))
        decision_path = tmp_path / "stop.json"
        decision_path.write_text(json.dumps(decision))
        assert (
            run.main(
                [
                    "resume",
                    "--session-id",
                    "source",
                    "--data-dir",
                    root,
                    "--decision-file",
                    str(decision_path),
                    "--checkpointer",
                    "sqlite",
                ]
            )
            == 1
        )
        source_state, _ = read_completed_qc_source(source_paths)
        assert source_state["supervision_state"] == "failed_terminal"

    source_ledger = Ledger(source_paths.run_dir).read_event_rows("source")
    assert (
        run.main(["rerun-assembly", "--from-session", "source", "--data-dir", root])
        == 0
    )
    (child_dir,) = [
        p for p in Path(root, "runs").iterdir() if p != source_paths.run_dir
    ]
    child_paths = SessionPaths.from_run_dir(child_dir)
    child, child_step = read_completed_qc_source(child_paths)
    qc = child["stage_outputs"]["read_qc"]
    assert child["supervision_state"] == "blocked_human"
    assert qc["reuse"] == {"session_id": "source", "workflow_step_id": source_step}
    assert qc["summary"] == source_state["stage_outputs"]["read_qc"]["summary"]
    assert Path(qc["summary_artifact_ref"]).is_relative_to(child_dir)
    assert advisor_facts[1] == advisor_facts[0]
    assert validate_frozen_inputs(child_paths).marker.parent_session_id == "source"

    [child_read] = resolve_long_read_members(child)
    [source_read] = resolve_long_read_members(source_state)
    assert Path(child_read.path).read_bytes() == Path(source_read.path).read_bytes()
    if filtered:
        assert Path(child_read.path).is_relative_to(child_dir)
        assert child_read.read_artifact_ref != source_read.read_artifact_ref
    else:
        assert child_read.path == str(reads)
    [pair] = resolve_hic_pairs(child)
    assert pair.r2 is not None
    assert Path(pair.r1.path).is_relative_to(child_dir)
    assert Path(pair.r2.path).is_relative_to(child_dir)
    child_events = Ledger(child_dir).read_event_rows(child_paths.session_id)
    assert any(
        row["event_type"] == "read_qc_reused" and row["workflow_step_id"] == child_step
        for row in child_events
    )
    assert not any(
        row["stage"] == "read_qc" and row["event_type"].startswith("attempt_")
        for row in child_events
    )
    assert Ledger(source_paths.run_dir).read_event_rows("source") == source_ledger

    if filtered:
        # Preserve length and format, but change the registered content.
        reads.write_text("@r\nTCGT\n+\nIIII\n")
        assert (
            run.main(["rerun-assembly", "--from-session", "source", "--data-dir", root])
            == 1
        )
        (rejected_dir,) = [
            p
            for p in Path(root, "runs").iterdir()
            if p not in {source_paths.run_dir, child_dir}
        ]
        rejected = SessionPaths.from_run_dir(rejected_dir)
        failure = next(
            row
            for row in Ledger(rejected_dir).read_event_rows(rejected.session_id)
            if row["event_type"] == "stage_failed" and row["stage"] == "read_qc"
        )
        assert "different registered read inputs" in failure["summary"]["error"]
        assert not any(
            row["stage"] == "read_qc"
            for row in read_artifact_records(rejected_dir, rejected.session_id)
        )
