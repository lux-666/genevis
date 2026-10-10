"""Frozen parent/SQLite/CLI checks with fixture tools, not quality experiments."""

import argparse
import json
import shlex
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from app.control.workflow_plan import workflow_steps_from_run_config
from app.entrypoints.run import _run_resume, _stage_seeds_from_run_config
from app.graph.builder import build_graph
from app.integrity import sha256_file
from app.schemas.state import initial_state
from app.stages.assembly_dedup.advisor import history, load_json
from app.stages.assembly_dedup.nodes import _registered_assembly_read_members
from app.storage.frozen_inputs import freeze_session_inputs
from app.storage.ledger import Ledger
from app.storage.paths import SessionPaths
from tests import test_assembly as fixtures
from tests.test_dedup_recommendation_loop import Model, _approve, _state, proposal


def freeze_parent(tmp_path, assets, run_config, methods):
    conda = tmp_path / "conda"
    conda.write_text(
        "#!/bin/sh\nwhile [ $# -gt 0 ]; do\n"
        'case "$1" in run|--live-stream|--no-capture-output) shift;; '
        "-p|-n) shift 2;; *) break;; esac\ndone\n"
        + f'export PATH={shlex.quote(str(tmp_path / "bin"))}:"$PATH"\nexec "$@"\n'
    )
    conda.chmod(0o755)
    envs = tmp_path / "envs.yaml"
    envs.write_text(
        json.dumps(
            {
                "default": {"conda_bin": str(conda), "fallback_prefix": str(tmp_path)},
                "methods": {
                    m: {"conda_prefix": str(tmp_path), "required_tools": t}
                    for m, t in methods.items()
                },
            }
        )
    )
    paths = SessionPaths.from_run_dir(tmp_path / "parent" / "runs" / "recovery")
    frozen = freeze_session_inputs(
        paths,
        manifest_draft={
            "session_id": paths.session_id,
            "genome_size_estimate": 1200,
            "assets": assets,
        },
        run_config_draft=run_config,
        config_path=envs,
        probe_environments=False,
    )
    Ledger(paths.run_dir).create_session(
        paths.session_id, paths.to_ref(paths.input_manifest_path)
    )
    shared = (
        frozen.run_config.shared_parameters.model_dump(mode="json", exclude_none=True)
        if frozen.run_config.shared_parameters
        else {}
    )
    initial = initial_state(
        session_id=paths.session_id,
        input_manifest_ref=paths.to_ref(paths.input_manifest_path),
        data_dir=str(paths.run_dir),
        workflow_steps=workflow_steps_from_run_config(frozen.run_config),
        stage_seeds=_stage_seeds_from_run_config(
            frozen.run_config, shared_parameters=shared
        ),
    )
    initial["shared_parameters"] = shared
    return paths, frozen, initial


def checkpoint(paths, frozen, initial=None):
    graph = build_graph(
        backend="sqlite", data_dir=str(paths.run_dir), verified_inputs=frozen
    )
    config = {"configurable": {"thread_id": paths.session_id}}
    try:
        return (
            graph.invoke(initial, config) if initial else graph.get_state(config).values
        )
    finally:
        graph.checkpointer.conn.close()


def resume_cli(tmp_path, paths, blocked, decision, **fields):
    gate = blocked["active_interrupt"]
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(
        json.dumps(
            {
                "session_id": paths.session_id,
                "stage": blocked["current_stage"],
                "gate_id": gate["gate_id"],
                "review_request_id": gate["review_request_id"],
                "review_request_hash": gate["request_hash"],
                "decision": decision,
                **fields,
            }
        )
    )
    assert (
        _run_resume(
            argparse.Namespace(
                session_id=paths.session_id,
                decision_file=str(decision_file),
                data_dir=str(paths.run_dir),
                checkpointer="sqlite",
            )
        )
        == 0
    )


ASSEMBLY_MODES = [
    ("flye", "cycloneseq", "cyclone", "nano-raw"),
    ("flye", "ont", "ont_raw", "nano-raw"),
    ("flye", "ont", "ont_hq", "nano-hq"),
    ("flye", "clr", "pacbio_clr", "pacbio-raw"),
    ("flye", "pacbio_hifi", "pacbio_hifi", "pacbio-hifi"),
    ("nextdenovo", "cycloneseq", "cyclone", "ont"),
    ("nextdenovo", "ont", "ont_raw", "ont"),
    ("nextdenovo", "ont", "ont_hq", "ont"),
    ("nextdenovo", "clr", "pacbio_clr", "clr"),
    ("nextdenovo", "pacbio_hifi", "pacbio_hifi", "hifi"),
    ("hifiasm", "pacbio_hifi", "pacbio_hifi", "hifi"),
    ("hifiasm_ont", "ont", "ont_raw", "ont"),
    ("hifiasm_ont", "ont", "ont_hq", "ont"),
    ("hifiasm_ont", "cycloneseq", "cyclone", "ont"),
]


@pytest.mark.parametrize("platform", [None, "unknown", "pacbio_hifi", "clr", "ont"])
def test_registered_assembly_advisor_constrains_depth_to_available_reads(
    tmp_path, platform
):
    from app.control.lifecycle import ResumeValidationError
    from app.stages.assembly_dedup.advisor import execution_capabilities
    from app.stages.assembly_dedup.nodes import freeze_purge_dups_attempt

    _, source = _state(tmp_path)
    assets = [
        {
            "asset_type": "assembly_fasta_registered",
            "paths": [str(source)],
            "format": "fasta",
            "compression": "none",
        }
    ]
    if platform:
        reads = tmp_path / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n")
        assets.append(
            {
                "asset_type": "raw_long_reads",
                "paths": [str(reads)],
                "format": "fastq",
                "compression": "none",
                "platform": platform,
            }
        )
    paths, frozen, initial = freeze_parent(
        tmp_path,
        assets,
        {
            "requested_stages": [
                {"stage": "assembly_dedup", "allow_local_execution": True}
            ],
            "assembly_dedup": {
                "automatic_purge_approved": True,
                "recommendation_mode": "advisor",
            },
            "review_policy": {"max_blocked_seconds": -1},
        },
        {
            "assembly_dedup.purge_dups": [
                "minimap2",
                "split_fa",
                "purge_dups",
                "get_seqs",
            ],
            "assembly_dedup.eval.quast": ["quast.py"],
        },
    )
    item = proposal()
    item["parameters"]["coverage_mode"] = "read_depth"
    model = Model([AIMessage(content=json.dumps(item))])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = checkpoint(paths, frozen, initial)
    caps = json.loads(model.messages[0][1]["content"])["execution_capabilities"]
    decisions = load_json(blocked, blocked["active_interrupt"]["request_ref"])[
        "payload"
    ]["decisions_allowed"]
    assert caps == execution_capabilities(blocked)
    assert caps["long_reads"]["available"] == bool(platform)
    assert [m["platform"] for m in caps["long_reads"]["members"]] == (
        [platform] if platform else []
    )
    if platform in {None, "unknown"}:
        assert caps["available_coverage_modes"] == ["self_alignment"]
        assert caps["read_depth_unavailable_reason"]
        assert "follow_recommendation" not in decisions
        with pytest.raises((ValueError, ResumeValidationError)):
            _approve(blocked)
    else:
        assert "read_depth" in caps["available_coverage_modes"]
        assert caps["long_reads"]["mapping_presets"] == [
            {
                "pacbio_hifi": "map-hifi",
                "clr": "map-pb",
                "ont": "map-ont",
            }[platform]
        ]
        assert "follow_recommendation" in decisions
        approved = _approve(blocked)
        # Recheck the resolved inputs after advice and after approval.
        with patch(
            "app.stages.assembly_dedup.nodes.resolve_long_read_members", return_value=[]
        ):
            with pytest.raises(ValueError, match="unavailable coverage_mode"):
                _approve(blocked)
            with patch(
                "app.stages.assembly_dedup.nodes.freeze_method_attempts"
            ) as freeze:
                rejected = freeze_purge_dups_attempt(approved)
                freeze.assert_not_called()
        assert (
            "unavailable coverage_mode"
            in rejected["stage_outputs"]["assembly_dedup"]["reason"]
        )


def test_registered_assembly_depth_requires_unambiguous_registered_reads():
    with patch(
        "app.stages.assembly_dedup.nodes.resolve_long_read_members", return_value=[]
    ):
        with pytest.raises(ValueError, match="requires registered long reads"):
            _registered_assembly_read_members({})


@pytest.mark.parametrize("coverage_mode", ["read_depth", "self_alignment"])
def test_unknown_registered_platform_is_handled_by_parent(tmp_path, coverage_mode):
    _, source = _state(tmp_path)
    reads = tmp_path / "unknown.fastq"
    reads.write_text("@r1\nACGT\n+\n!!!!\n")
    paths, frozen, initial = freeze_parent(
        tmp_path,
        [
            {
                "asset_type": "assembly_fasta_registered",
                "paths": [str(source)],
                "format": "fasta",
                "compression": "none",
            },
            {
                "asset_type": "raw_long_reads",
                "paths": [str(reads)],
                "format": "fastq",
                "compression": "none",
                "platform": "unknown",
            },
        ],
        {
            "requested_stages": [
                {"stage": "assembly_dedup", "allow_local_execution": True}
            ],
            "assembly_dedup": {
                "automatic_purge_approved": True,
                "recommendation_mode": "deterministic_baseline",
                "coverage_mode": coverage_mode,
            },
        },
        {
            "assembly_dedup.purge_dups": [
                "minimap2",
                "split_fa",
                "purge_dups",
                "get_seqs",
            ]
        },
    )
    if coverage_mode == "read_depth":
        with patch("app.stages.assembly_dedup.nodes.freeze_method_attempts") as freeze:
            final = checkpoint(paths, frozen, initial)
            freeze.assert_not_called()
    else:
        final = checkpoint(paths, frozen, initial)
        assert final["supervision_state"] == "completed"
        return
    assert final["supervision_state"] == "failed_terminal"
    cursor = final["stage_outputs"]["assembly_dedup"]
    assert cursor["status"] == "failed"
    assert cursor["purge_dups_status"] == "not_started"
    assert "unsupported long-read platform" in cursor["reason"]
    assert "unknown" in cursor["reason"]


@pytest.mark.parametrize("method,platform,read_type,native_mode", ASSEMBLY_MODES)
def test_assembly_modes_parent_cli_recovery(
    tmp_path, method, platform, read_type, native_mode, hifi_override=False
):
    helper = fixtures.AssemblyStageTests()
    tool = {"flye": "flye", "nextdenovo": "nextDenovo"}.get(method, "hifiasm")
    factory = {
        "flye": helper._fake_flye_bin,
        "nextdenovo": helper._fake_nextdenovo_bin,
    }.get(method, helper._fake_hifiasm_bin)
    marker = tmp_path / "calls"
    executable = factory(str(tmp_path)) / tool
    captured_fofn = tmp_path / "executed.input.fofn"
    capture = (
        "fofn=$(awk -F= '/^input_fofn[[:space:]]*=/ "
        '{gsub(/^[ \\t]+|[ \\t]+$/, "", $2); print $2}\' "$1")\n'
        f'cat "$fofn" > {shlex.quote(str(captured_fofn))}\n'
        if method == "nextdenovo"
        else ""
    )
    executable.write_text(
        executable.read_text().replace(
            "#!/bin/sh\n",
            f"#!/bin/sh\necho called >> {shlex.quote(str(marker))}\n" + capture,
            1,
        )
    )
    separator = "_" if method == "nextdenovo" else " "
    reads = [tmp_path / f"reads{separator}one.fq", tmp_path / f"reads{separator}two.fq"]
    for path in reads:
        path.write_text("@r1\nACGT\n+\n!!!!\n")
    method_id = f"assembly.candidate.{method}"
    paths, frozen, initial = freeze_parent(
        tmp_path,
        [
            {
                "asset_type": "raw_long_reads",
                "paths": [str(p) for p in reads],
                "platform": platform,
                "format": "fastq",
                "compression": "none",
            },
        ],
        {
            "requested_stages": [{"stage": "assembly", "allow_local_execution": True}],
            "shared_parameters": {"read_type": read_type},
            "assembly": {
                "threads": 1,
                **({"hifi_reads_ref": str(reads[1])} if hifi_override else {}),
            },
            "review_policy": {"max_blocked_seconds": -1},
        },
        {method_id: [tool]},
    )
    route = {"pacbio_hifi": "hifi", "ont_raw": "ont", "ont_hq": "ont"}.get(
        read_type, "cyclone"
    )

    class MethodModel:
        calls = 0

        def invoke(self, messages):
            self.calls += 1
            assert method_id in json.loads(messages[1]["content"])["allowed_methods"]
            return json.dumps(
                {
                    "recommended_route": route,
                    "recommended_read_type": read_type,
                    "recommended_method": method_id,
                    "reasoning": "fixture selection",
                    "uncertainty_notes": [],
                }
            )

    model = MethodModel()
    with (
        patch("app.stages.assembly.decision.build_chat_model", return_value=model),
        patch(
            "app.llm.parameters.assembly.recommender.build_chat_model",
            return_value=fixtures._ParameterModel(),
        ),
    ):
        blocked = checkpoint(paths, frozen, initial)
        assert blocked["active_interrupt"]["gate_id"] == "route_selection"
        assert not marker.exists()
        resume_cli(
            tmp_path,
            paths,
            blocked,
            f"select_{route}",
            method_name=method_id,
            metadata={"read_type": read_type},
        )
        blocked = checkpoint(paths, frozen)
        gate = blocked["active_interrupt"]
        assert gate["gate_id"] == "parameter_approval"
        assert blocked["stage_outputs"]["assembly"]["assembly_route"] == "auto"
        assert not marker.exists()
        resume_cli(
            tmp_path,
            paths,
            blocked,
            "approve_parameters",
            metadata={
                key: gate[key]
                for key in (
                    "attempt_id",
                    "task",
                    "previous_attempt_id",
                    "recommendation_hash",
                )
            },
        )
    final = checkpoint(paths, frozen)
    assert final["supervision_state"] == "completed"
    assert model.calls == 1 and marker.read_text().splitlines() == ["called"]
    resource = final["resource_refs"]["assembly"]
    bundle = json.loads(
        paths.resolve_ref(
            resource["metadata"]["candidate_bundle_ref"]["path_ref"]
        ).read_text()
    )
    assert bundle["method_name"] == method_id
    expected_reads = reads[1:] if hifi_override else reads
    assert len(bundle["input_member_ids"]["primary"]) == len(expected_reads)
    assert Path(resource["paths"][0]).read_text().startswith(">")
    command = json.loads(
        paths.workflow_step_artifact_path(
            "step-001-assembly", "assembly", "attempts", "000", "command.json"
        ).read_text()
    )
    if method == "nextdenovo":
        cfg = paths.workflow_step_artifact_path(
            "step-001-assembly",
            "assembly",
            "attempts",
            "000",
            "outputs",
            "nextdenovo",
            "run.cfg",
        ).read_text()
        assert f"read_type = {native_mode}\n" in cfg
        assert "nextgraph_options = -a 1\n" in cfg
        # Execution inputs remain verifiable after collect and terminal work cleanup.
        assert Path(command["argv"][1]).read_text() == cfg
        assert Path(
            command["rendered_parameters"]["input_fofn"]
        ).read_text().splitlines() == [str(path) for path in expected_reads]
        assert bundle["input_member_ids"]["primary"]
        assert len(bundle["input_member_ids"]["primary"]) == len(expected_reads)
        # The fixture reads the run.cfg-bound input.fofn at execution time;
        # terminal CLI compaction then removes the transient work directory.
        assert captured_fofn.read_text().splitlines() == [
            str(path) for path in expected_reads
        ]
    else:
        argv = command["argv"]
        assert all(str(p) in argv for p in expected_reads)
        if hifi_override:
            assert str(reads[0]) not in argv
        if method == "flye":
            assert f"--{native_mode}" in argv
        else:
            assert ("--ont" in argv) == (method == "hifiasm_ont")


def test_explicit_hifi_override_survives_parent_reentry(tmp_path):
    test_assembly_modes_parent_cli_recovery(
        tmp_path,
        "hifiasm",
        "pacbio_hifi",
        "pacbio_hifi",
        "hifi",
        hifi_override=True,
    )


@pytest.mark.parametrize("coverage_mode", ["self_alignment", "read_depth"])
@pytest.mark.parametrize("selection", ["accept", "keep_source"])
def test_dedup_parent_multiround_cli_recovery(tmp_path, coverage_mode, selection):
    _, source = _state(tmp_path)
    bins = tmp_path / "bin"
    for tool, script in {
        "pbcstat": "printf 'stats\\n' > PB.stat\nprintf 'cov\\n' > PB.base.cov\n",
        "calcuts": "printf '1 2 3 4 5 6\\n'\n",
    }.items():
        path = bins / tool
        path.write_text("#!/bin/sh\nset -eu\n" + script)
        path.chmod(0o755)
    calls = tmp_path / "tool_calls"
    for path in bins.iterdir():
        path.write_text(
            path.read_text().replace(
                "#!/bin/sh\n",
                f"#!/bin/sh\necho {path.name} >> {shlex.quote(str(calls))}\n",
                1,
            )
        )
    reads = tmp_path / "reads.fq"
    reads.write_text("@r1\nACGT\n+\n!!!!\n")
    raw_hash = sha256_file(source)
    paths, frozen, initial = freeze_parent(
        tmp_path,
        [
            {
                "asset_type": "assembly_fasta_registered",
                "paths": [str(source)],
                "format": "fasta",
                "compression": "none",
            },
            {
                "asset_type": "raw_long_reads",
                "paths": [str(reads)],
                "format": "fastq",
                "compression": "none",
                "platform": "pacbio_hifi",
            },
        ],
        {
            "requested_stages": [
                {"stage": "assembly_dedup", "allow_local_execution": True}
            ],
            "assembly_dedup": {
                "automatic_purge_approved": True,
                "recommendation_mode": "advisor",
                "max_attempts": 2,
                "threads": 1,
            },
            "review_policy": {"max_blocked_seconds": -1},
        },
        {
            "assembly_dedup.purge_dups": [
                "minimap2",
                "split_fa",
                "purge_dups",
                "get_seqs",
                "pbcstat",
                "calcuts",
            ],
            "assembly_dedup.eval.quast": ["quast.py"],
        },
    )
    proposals = [
        proposal(),
        proposal(preset="asm10"),
        proposal(selection, selected=0 if selection == "accept" else None),
    ]
    for item in proposals[:2]:
        item["parameters"]["coverage_mode"] = coverage_mode
    model = Model([AIMessage(content=json.dumps(item)) for item in proposals])
    with patch("app.llm.recommender.build_chat_model", return_value=model):
        blocked = checkpoint(paths, frozen, initial)
        for completed_attempts in range(2):
            assert blocked["active_interrupt"]["gate_id"] == "dedup_recommendation"
            assert len(history(blocked)) == completed_attempts
            resume_cli(tmp_path, paths, blocked, "follow_recommendation")
            blocked = checkpoint(paths, frozen)
        before = calls.read_bytes()
        records = history(blocked)
        preserved = {
            ref["path_ref"]: sha256_file(paths.resolve_ref(ref["path_ref"]))
            for r in records
            for ref in (r["candidate_ref"], r["primary_ref"])
        }
        for record in records:
            execution = load_json(blocked, record["execution_ref"])
            attempt = load_json(blocked, execution["frozen_attempt_ref"])["attempt"]
            assert any(
                d.get("artifact_ref")
                == blocked["stage_outputs"]["assembly_dedup"][
                    "input_primary_output_ref"
                ]
                for d in attempt["input_dependencies"]
            )
            if coverage_mode == "read_depth":
                assert str(reads) in json.dumps(attempt["input_dependencies"])
                assert attempt["metadata"]["read_mapping_presets"]
        resume_cli(tmp_path, paths, blocked, "follow_recommendation")
    final = checkpoint(paths, frozen)
    assert final["supervision_state"] == "completed"
    assert calls.read_bytes() == before and sha256_file(source) == raw_hash
    assert len(model.messages) == 3
    for ref, digest in preserved.items():
        assert sha256_file(paths.resolve_ref(ref)) == digest
    called = calls.read_text().splitlines()
    assert called.count("purge_dups") == 2 and called.count("quast.py") == 3
    assert called.count("pbcstat") == (2 if coverage_mode == "read_depth" else 0)
    selected_ref = final["resource_refs"]["assembly"]["metadata"][
        "candidate_bundle_ref"
    ]
    if selection == "accept":
        assert selected_ref == records[0]["candidate_ref"]
    else:
        assert "/registered_assembly/" in selected_ref["path_ref"]
    events = Ledger(paths.run_dir).read_events(paths.session_id)
    assert (
        len(
            [
                e
                for e in events
                if e["event_type"] == "method_disposition_recorded"
                and e["stage"] == "assembly_dedup"
            ]
        )
        == 2
    )
