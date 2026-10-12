"""Server-entry contracts; fixtures are not representative biological acceptance."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.control.workflow_plan import workflow_steps_from_run_config
from app.diagnostics import route_acceptance as audit
from app.schemas.manifest import InputManifest
from app.schemas.run_config import RunConfig, load_run_config
from app.storage.ledger import Ledger
from app.workflow import ASSEMBLY_HANDOFF_PRODUCER_STAGES, recommended_workflow_template
from tests.data_v2 import v2_run_dir

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "route,template,stages",
    [
        ("contig", "contig", recommended_workflow_template("assembly_review")),
        ("review", "assembly_review", ["assembly_review"]),
        ("hic", "hic", ["hic_scaffolding"]),
        ("gapcloser", "gapcloser", ["gapcloser"]),
        ("dedup", "dedup", ["assembly_dedup"]),
        ("t2t", "t2t", ["t2t"]),
        ("base-polish", "base_polish", ["base_polish", "assembly_review"]),
        (
            "finishing",
            "finishing",
            ["gapcloser", "t2t", "base_polish", "assembly_review"],
        ),
    ],
)
def test_slice_profiles_use_exact_frozen_plan(
    tmp_path, monkeypatch, route, template, stages
):
    run = v2_run_dir(tmp_path, "audit")
    config = load_run_config(
        ROOT / f"docs/examples/{template}_run_config.template.json"
    )
    assert config.requested_stage_names() == stages
    monkeypatch.setattr(
        audit,
        "validate_frozen_inputs",
        lambda _: SimpleNamespace(run_config=config, file_sha256=lambda key: "0" * 64),
    )
    monkeypatch.setattr(
        audit, "inspect_session", lambda *args: {"supervision_state": "blocked_human"}
    )
    report = audit.audit_route(run)
    assert report["workflow_steps"] == workflow_steps_from_run_config(config)
    assert report["status"] == "incomplete_execution_coverage"
    assert [r["stage"] for r in report["occurrences"]] == ["inventory", *stages]


def test_slice_manifests_register_existing_source():
    for name, types in (
        ("hic", ["assembly_fasta_registered", "hic_reads_registered"]),
        ("hifi_finishing", ["assembly_fasta_registered", "raw_long_reads"]),
    ):
        manifest = InputManifest.model_validate(
            yaml.safe_load(
                (ROOT / f"docs/examples/{name}_manifest.template.yaml").read_text()
            )
        )
        assert [a.asset_type for a in manifest.assets] == types
        if name == "hic":
            assert manifest.assets[1].paired_reads
        else:
            assert manifest.assets[1].platform == "pacbio_hifi"
            assert manifest.user_metadata["target_species"]


def test_hic_resume_comparison_fails_closed():
    from copy import deepcopy

    before = {
        "schema_version": 3,
        "workflow_steps": [{"stage": "inventory"}, {"stage": "hic_scaffolding"}],
        "session_id": "hic-a",
        "frozen_input_hashes": {
            key: "0" * 64
            for key in ("input_manifest", "run_config", "dependency_manifest")
        },
        "session_state": "blocked_human",
        "active_gate": {"stage": "hic_scaffolding", "gate_id": "hic_scaffolding"},
        "review_request_ref": {"artifact_id": "adoption"},
        "occurrences": [
            {
                "stage": "hic_scaffolding",
                "attempt_starts": ["a"],
                "methods": [
                    {
                        "method_name": "hic_scaffolding.autohic",
                        "processes": [
                            {
                                "attempt_id": "a",
                                "success": True,
                                "collected": True,
                                "evidence_ref": {"sha256": "1" * 64},
                                "collected_refs": [{"sha256": "2" * 64}],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    after = deepcopy(before)
    after.update(session_state="completed", active_gate=None)
    assert audit.compare_hic_execution(after, before)["status"] == "unchanged"
    for fault in (
        "session",
        "inputs",
        "gate",
        "request",
        "attempt",
        "product",
        "empty",
        "unfinished",
    ):
        left, right = deepcopy(before), deepcopy(after)
        if fault == "session":
            right["session_id"] = "another"
        elif fault == "inputs":
            right["frozen_input_hashes"]["run_config"] = "3" * 64
        elif fault == "gate":
            left["active_gate"]["gate_id"] = "parameter_approval"
        elif fault == "request":
            right["review_request_ref"] = {"artifact_id": "retry"}
        elif fault == "attempt":
            right["occurrences"][0]["attempt_starts"].append("b")
        elif fault == "product":
            right["occurrences"][0]["methods"][0]["processes"][0]["collected_refs"] = []
        elif fault == "empty":
            left["occurrences"][0]["methods"] = right["occurrences"][0]["methods"] = []
        else:
            right["session_state"] = "blocked_human"
        with pytest.raises(ValueError):
            audit.compare_hic_execution(right, left)


@pytest.mark.parametrize("decision", ["accept_candidate", "keep_prior_assembly"])
def test_hic_comparison_uses_actual_collected_execution_across_adoption(
    tmp_path, decision
):
    from unittest.mock import patch

    from app.review.base import ResumeReviewRequest
    from app.schemas.artifacts import ArtifactRef
    from app.stages.hic_scaffolding.builder import build_stage
    from app.storage.artifacts import read_artifact_records
    from tests.test_hic_scaffolding import HiCScaffoldingStageTests

    fixture = HiCScaffoldingStageTests()
    ran, _ = fixture._ran_autohic_state(str(tmp_path))
    with patch(
        "app.execution.method_runner.LocalRunner.run",
        side_effect=AssertionError("must not reexecute"),
    ):
        blocked = build_stage().invoke(ran)
        gate = blocked["active_interrupt"]

        def capture(state, session_state):
            step_id = gate["workflow_step_id"]
            rows = [
                r
                for r in Ledger(state["data_dir"]).read_event_rows("s1")
                if r["workflow_step_id"] == step_id
            ]
            records = [
                r
                for r in read_artifact_records(state["data_dir"], "s1")
                if r["workflow_step_id"] == step_id
            ]
            occurrence_state = {
                **state,
                "current_stage": "hic_scaffolding",
                "workflow_step_index": blocked["workflow_step_index"],
            }
            methods, gaps = audit.execution_coverage(occurrence_state, rows, records)
            assert not gaps
            return {
                "schema_version": 3,
                "workflow_steps": [
                    {"stage": "inventory"},
                    {"stage": "hic_scaffolding"},
                ],
                "session_id": "s1",
                "frozen_input_hashes": {"fixture": "0" * 64},
                "session_state": session_state,
                "active_gate": gate if session_state == "blocked_human" else None,
                "review_request_ref": gate["request_ref"],
                "occurrences": [
                    {
                        "stage": "hic_scaffolding",
                        "methods": methods,
                        "attempt_starts": [
                            r for r in rows if r["event_type"] == "attempt_started"
                        ],
                    }
                ],
            }

        before = capture(blocked, "blocked_human")
        adopted = fixture._apply_hic_scaffolding_review(
            blocked,
            ResumeReviewRequest(
                session_id="s1",
                stage="hic_scaffolding",
                gate_id="hic_scaffolding",
                decision=decision,
                review_request_id=gate["review_request_id"],
                review_request_hash=gate["request_hash"],
                rationale_artifact_ref=ArtifactRef.model_validate(gate["request_ref"]),
            ),
        )
        # This fixture applies the real persisted adoption effect, not the
        # outer CLI terminal finalizer; compare its verified execution trace.
        after = capture({**blocked, **adopted}, "completed")
        assert audit.compare_hic_execution(after, before)["status"] == "unchanged"


def test_complete_examples_and_tool_closure() -> None:
    from app.execution.run_readiness import effective_method_closure

    config = load_run_config(
        ROOT / "docs/examples/hifi_complete_run_config.template.json"
    )
    manifest = InputManifest.model_validate(
        yaml.safe_load(
            (ROOT / "docs/examples/hifi_complete_manifest.template.yaml").read_text()
        )
    )
    assert config.requested_stage_names() == recommended_workflow_template("t2t")
    assert all(r.allow_local_execution for r in config.requested_stages)
    assert manifest.assets[0].platform == "pacbio_hifi"
    assert manifest.assets[1].paired_reads
    assert config.review_policy.max_blocked_seconds == -1
    assert effective_method_closure(config)["base_polish"] == (
        "base_polish.eval.quast",
        "base_polish.racon",
    )
    assert effective_method_closure(config)["gapcloser"] == (
        "gapcloser.eval.quast",
        "gapcloser.tgs_gapcloser",
    )
    assert (
        workflow_steps_from_run_config(config)[-1]["review_target"]["producer_step_id"]
        == "step-009-base_polish"
    )


def _state(run: Path) -> dict:
    return {
        "data_dir": str(run),
        "session_id": run.name,
        "current_stage": "base_polish",
        "workflow_step_index": 0,
        "workflow_steps": [
            {
                "step_id": "step-001-base_polish",
                "stage": "base_polish",
                "allow_local_execution": True,
                "selected_method_names": ["base_polish.racon"],
            }
        ],
    }


def test_completed_without_process_is_not_acceptance(tmp_path: Path) -> None:
    run = v2_run_dir(tmp_path, "audit")
    ledger = Ledger(run)
    ledger.record_method_disposition(
        "audit", "base_polish", "step-001-base_polish", "base_polish.racon", "completed"
    )
    with pytest.raises(ValueError, match="without verified process"):
        audit.execution_coverage(_state(run), ledger.read_event_rows("audit"), [])


@pytest.fixture
def polished_execution(tmp_path: Path):
    from app.stages.base_polish.builder import build_stage
    from app.storage.artifacts import read_artifact_records
    from tests.test_base_polish import fake_tools, state_for_polish

    state = state_for_polish(tmp_path)
    state["stage_outputs"] = {"base_polish": {"env": {"PATH": fake_tools(tmp_path)}}}
    result = build_stage().invoke(state)
    assert result["supervision_state"] == "blocked_human"
    rows = Ledger(state["data_dir"]).read_event_rows(state["session_id"])
    records = read_artifact_records(state["data_dir"], state["session_id"])
    return result, rows, records


@pytest.mark.parametrize(
    "fault", ["corrupt_product", "empty_collection", "wrong_method"]
)
def test_collection_evidence_is_verified(polished_execution, fault) -> None:
    state, rows, records = polished_execution
    collection = next(row for row in rows if row["event_type"] == "attempt_succeeded")
    if fault == "corrupt_product":
        product = next(
            ref
            for ref in collection["refs"]
            if ref["artifact_type"] == "base_polish.before_paf"
        )
        (Path(state["data_dir"]) / product["path_ref"]).write_text("corrupt PAF")
    elif fault == "empty_collection":
        collection["refs"] = []
    else:
        collection["summary"]["method_name"] = "unrelated.method"
    with pytest.raises(ValueError):
        audit.execution_coverage(state, rows, records)


def test_failed_method_remains_a_gap_after_successful_attempt(
    polished_execution,
) -> None:
    state, rows, records = polished_execution
    methods, gaps = audit.execution_coverage(state, rows, records)
    assert not gaps
    assert methods[0]["processes"][0]["collected_refs"]
    for row in rows:
        if row["event_type"] == "method_disposition_recorded":
            row["summary"].update(disposition="failed", reason="later attempt failed")
    methods, gaps = audit.execution_coverage(state, rows, records)
    assert methods[0]["execution_verified"]
    assert any("failed: later attempt failed" in gap for gap in gaps)


def test_skip_is_reported_as_unexecuted_not_code_defect(tmp_path: Path) -> None:
    run = v2_run_dir(tmp_path, "audit")
    ledger = Ledger(run)
    state = _state(run)
    state["current_stage"] = "gapcloser"
    state["workflow_steps"][0]["selected_method_names"] = ["gapcloser.tgs_gapcloser"]
    ledger.record_method_disposition(
        "audit",
        "gapcloser",
        "step-001-gapcloser",
        "gapcloser.tgs_gapcloser",
        "skipped",
        reason="no_internal_gaps",
    )
    methods, gaps = audit.execution_coverage(state, ledger.read_event_rows("audit"), [])
    assert not methods[0]["execution_verified"]
    assert "no_internal_gaps" in gaps[0]


def test_unfinished_route_reports_every_occurrence_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = v2_run_dir(tmp_path, "audit")
    config = load_run_config(
        ROOT / "docs/examples/hifi_complete_run_config.template.json"
    )
    monkeypatch.setattr(
        audit,
        "validate_frozen_inputs",
        lambda _: SimpleNamespace(run_config=config, file_sha256=lambda key: "0" * 64),
    )
    monkeypatch.setattr(
        audit, "inspect_session", lambda *args: {"supervision_state": "blocked_human"}
    )
    before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
    report = audit.audit_route(run)
    assert report["status"] == "incomplete_execution_coverage"
    assert len(report["occurrences"]) == 11
    assert report["quality_comparison"] is None
    assert report["final_assembly"] is None
    after = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize(
    "stages",
    [
        [],
        ["read_qc"],
        ["genome_survey"],
        ["assembly"],
        ["read_qc", "genome_survey"],
        ["read_qc", "read_qc", "assembly"],
    ],
)
@pytest.mark.parametrize("completed", [False, True])
def test_audit_uses_literal_config_without_requiring_assembly_for_read_stages(
    tmp_path, monkeypatch, stages, completed
):
    run = v2_run_dir(tmp_path, "audit")
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": stage, "allow_local_execution": True} for stage in stages
            ]
        }
    )
    steps = workflow_steps_from_run_config(config)
    rows = []
    for step in steps:
        if completed:
            rows.append(
                {
                    "event_type": "stage_completed",
                    "stage": step["stage"],
                    "workflow_step_id": step["step_id"],
                }
            )
            if step["stage"] == "assembly":
                rows.append(
                    {
                        "event_type": "resource_delivered",
                        "stage": "assembly",
                        "workflow_step_id": step["step_id"],
                    }
                )
    monkeypatch.setattr(
        audit,
        "validate_frozen_inputs",
        lambda _: SimpleNamespace(run_config=config, file_sha256=lambda key: "0" * 64),
    )
    monkeypatch.setattr(
        audit,
        "inspect_session",
        lambda *args: {
            "supervision_state": "completed" if completed else "blocked_human",
            "final_assembly": {"path": "final.fa"} if "assembly" in stages else None,
        },
    )
    monkeypatch.setattr(Ledger, "read_event_rows", lambda *args: rows)
    monkeypatch.setattr(audit, "read_artifact_records", lambda *args: [])
    monkeypatch.setattr(audit, "execution_coverage", lambda *args: ([], []))
    monkeypatch.setattr(
        audit,
        "verify_delivery",
        lambda *args: {"primary_output_ref": {"sha256": "0" * 64}},
    )
    report = audit.audit_route(run)
    assert report["workflow_steps"] == steps
    assert [row["stage"] for row in report["occurrences"]] == ["inventory", *stages]
    assert len({row["step_id"] for row in report["occurrences"]}) == len(steps)
    assert report["status"] == (
        "complete_execution_coverage" if completed else "incomplete_execution_coverage"
    )
    assert report["quality_evaluation_status"] == "not_evaluated"


def test_cli_invalid_evidence_is_json_and_nonzero(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "sys.argv", ["route_acceptance", "--run-dir", str(tmp_path / "runs" / "absent")]
    )
    assert audit.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_evidence"


def test_frozen_gapcloser_slice_checks_registered_source_and_skip(tmp_path):
    from tests.test_gapcloser import _run_frozen_example

    result, run = _run_frozen_example(tmp_path, "AAAACCCC", "AAAACCCC")
    assert result["supervision_state"] == "completed"
    # The graph-only fixture closes SQLite before the CLI terminal finalizer.
    Ledger(run).record_session_terminal(result["session_id"], outcome="completed")

    # SQLite inspection may create WAL/SHM sidecars; durable evidence is read-only.
    def evidence_bytes():
        return {
            p: p.read_bytes()
            for p in run.rglob("*")
            if p.is_file() and "checkpoints" not in p.relative_to(run).parts
        }

    before = evidence_bytes()
    report = audit.audit_route(run)
    assert report["status"] == "incomplete_execution_coverage"
    assert report["quality_evaluation_status"] == "not_evaluated"
    assert report["final_assembly"]
    source = report["occurrences"][0]["deliveries"][0]
    assert source["delivery_mode"] == "materialized"
    assert (
        report["occurrences"][1]["deliveries"][0]["primary_output_ref"]
        == source["primary_output_ref"]
    )
    assert any("no_internal_N_gaps" in gap for gap in report["coverage_gaps"])
    assert evidence_bytes() == before
    Path(source["path"]).write_text(">changed\nACGT\n")
    with pytest.raises(ValueError):
        audit.audit_route(run)


def test_frozen_qc_graph_audits_without_an_assembly(tmp_path, monkeypatch, capsys):
    from app.graph.builder import build_graph
    from app.schemas.state import initial_state
    from tests.test_graph_routing import _freeze_verified_inputs

    run = v2_run_dir(tmp_path, "qc-audit")
    reads = tmp_path / "reads.fa"
    reads.write_text(">r1\nACGT\n")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "session_id": "qc-audit",
                "assets": [
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(reads)],
                        "platform": "ont",
                        "format": "fasta",
                    }
                ],
            }
        )
    )
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "read_qc", "allow_local_execution": False},
                {"stage": "read_qc", "allow_local_execution": False},
            ]
        }
    )
    paths, frozen = _freeze_verified_inputs(run, manifest, config)
    Ledger(run).create_session(
        paths.session_id, paths.to_ref(paths.input_manifest_path)
    )
    state = initial_state(
        session_id=paths.session_id,
        input_manifest_ref=paths.to_ref(paths.input_manifest_path),
        data_dir=str(run),
        workflow_steps=workflow_steps_from_run_config(config),
    )
    graph = build_graph(backend="sqlite", data_dir=str(run), verified_inputs=frozen)
    try:
        result = graph.invoke(state, {"configurable": {"thread_id": paths.session_id}})
    finally:
        graph.checkpointer.conn.close()
    assert result["supervision_state"] == "completed"
    Ledger(run).record_session_terminal(paths.session_id, outcome="completed")
    report = audit.audit_route(run)
    assert report["session_state"] == "completed"
    assert report["final_assembly"] is None
    # Tools were not authorized: successful workflow settlement is not coverage.
    assert report["status"] == "incomplete_execution_coverage"
    assert [row["outcome"] for row in report["occurrences"]] == ["stage_completed"] * 3
    assert any("step-001-read_qc" in gap for gap in report["coverage_gaps"])
    assert any("step-002-read_qc" in gap for gap in report["coverage_gaps"])
    monkeypatch.setattr("sys.argv", ["route_acceptance", "--run-dir", str(run)])
    capsys.readouterr()
    assert audit.main() == 2
    assert json.loads(capsys.readouterr().out) == report


@pytest.fixture
def completed_slice(tmp_path, monkeypatch):
    """Stub only evidence readers to test scoped orchestration and fault policy."""
    from app.schemas.artifacts import ArtifactRef

    def setup(route, *, stages=None):
        run = v2_run_dir(tmp_path, "audit")
        stem = {"review": "assembly_review", "base-polish": "base_polish"}.get(
            route, route
        )
        config = load_run_config(
            ROOT / f"docs/examples/{stem}_run_config.template.json"
        )
        if stages is not None:
            config = RunConfig.model_validate(
                {
                    **config.model_dump(mode="json"),
                    "requested_stages": [
                        {"stage": stage, "allow_local_execution": True}
                        for stage in stages
                    ],
                }
            )
        steps = workflow_steps_from_run_config(config)
        rows, deliveries, methods = [], {}, {}
        previous_primary = None
        for index, step in enumerate(steps):
            if step["stage"] in ASSEMBLY_HANDOFF_PRODUCER_STAGES:
                rows.append(
                    {
                        "event_type": "resource_delivered",
                        "stage": step["stage"],
                        "workflow_step_id": step["step_id"],
                    }
                )
                deliveries[step["step_id"]] = {
                    "delivery_mode": "materialized",
                    "candidate_id": "candidate",
                    "primary_output_ref": {"producer": index},
                }
            rows.append(
                {
                    "event_type": "stage_completed",
                    "stage": step["stage"],
                    "workflow_step_id": step["step_id"],
                }
            )
            names = step["selected_method_names"] if index else []
            methods[step["step_id"]] = [
                {
                    "method_name": name,
                    "processes": [
                        {
                            "success": True,
                            "collected": True,
                            "candidate_id": "candidate",
                            "input_artifacts": [previous_primary]
                            if previous_primary
                            else [],
                            "evaluation_settings": {"threads": 1},
                        }
                    ],
                }
                for name in names
            ]
            if step["step_id"] in deliveries:
                previous_primary = deliveries[step["step_id"]]["primary_output_ref"]
        request_ref = ArtifactRef(
            artifact_id="fixture",
            path_ref="artifacts/request.json",
            sha256="0" * 64,
            size_bytes=1,
            artifact_type="assembly_review.request",
        ).model_dump(mode="json")
        snapshot = {
            "supervision_state": "completed",
            "final_assembly": {"path": "final.fa"},
            "latest_review_request_artifact": request_ref,
        }
        quality = {"candidates": [{"candidate_id": "candidate"}]}
        monkeypatch.setattr(
            audit,
            "validate_frozen_inputs",
            lambda _: SimpleNamespace(
                run_config=config, file_sha256=lambda key: "0" * 64
            ),
        )
        monkeypatch.setattr(audit, "inspect_session", lambda *args: snapshot)
        monkeypatch.setattr(audit, "read_artifact_records", lambda *args: [])
        monkeypatch.setattr(Ledger, "read_event_rows", lambda *args: rows)
        monkeypatch.setattr(
            audit, "verify_delivery", lambda _, row: deliveries[row["workflow_step_id"]]
        )
        monkeypatch.setattr(
            audit,
            "execution_coverage",
            lambda state, *_: (
                methods[steps[state["workflow_step_index"]]["step_id"]],
                [],
            ),
        )
        monkeypatch.setattr(audit, "final_evaluation", lambda *args: quality)
        monkeypatch.setattr(audit, "final_comparison", lambda *args: quality)
        return run, steps, rows, deliveries, methods, snapshot, quality

    return setup


@pytest.mark.parametrize(
    "route", ["hic", "gapcloser", "t2t", "review", "base-polish", "finishing"]
)
def test_completed_scoped_audit_does_not_require_other_stages(completed_slice, route):
    run, *_ = completed_slice(route)
    report = audit.audit_route(run)
    assert report["status"] == "complete_execution_coverage"
    assert (report["quality_evaluation_status"] == "verified") == (
        route in {"review", "base-polish", "finishing"}
    )


@pytest.mark.parametrize(
    "stages,paired",
    [
        (["read_qc", "base_polish", "assembly_review"], True),
        (["base_polish", "read_qc", "assembly_review"], True),
        (["base_polish", "assembly_review", "base_polish", "assembly_review"], True),
        (["base_polish", "assembly_review", "assembly_review"], False),
        (["base_polish", "assembly_dedup", "assembly_review"], False),
    ],
)
def test_final_evaluation_follows_review_producer_binding(
    completed_slice, monkeypatch, stages, paired
):
    run, steps, _, _, _, _, quality = completed_slice("base-polish", stages=stages)
    calls = []
    records = [
        {"workflow_step_id": step["step_id"], "artifact_type": "base_polish.comparison"}
        for step in steps
        if step["stage"] == "base_polish"
    ]
    monkeypatch.setattr(audit, "read_artifact_records", lambda *args: records)

    def evaluate(*args):
        calls.append("evaluation")
        return quality

    def compare(*args):
        assert args[1] == [
            r
            for r in records
            if r["workflow_step_id"] == steps[-1]["review_target"]["producer_step_id"]
        ]
        calls.append("comparison")
        return quality

    monkeypatch.setattr(audit, "final_evaluation", evaluate)
    monkeypatch.setattr(audit, "final_comparison", compare)
    report = audit.audit_route(run)
    assert report["workflow_steps"] == steps
    assert calls == ["comparison" if paired else "evaluation"]


def test_review_after_qc_rejects_input_from_the_adjacent_nonproducer(completed_slice):
    run, steps, _, _, methods, _, _ = completed_slice(
        "base-polish", stages=["base_polish", "read_qc", "assembly_review"]
    )
    for method in methods[steps[-1]["step_id"]]:
        method["processes"][0]["input_artifacts"] = [{"producer": 2}]
    with pytest.raises(ValueError, match="no execution consumes preceding assembly"):
        audit.audit_route(run)


def test_repeated_qc_cannot_borrow_another_occurrences_completion(
    tmp_path, monkeypatch
):
    run = v2_run_dir(tmp_path, "audit")
    config = RunConfig.model_validate(
        {
            "requested_stages": [
                {"stage": "read_qc", "allow_local_execution": True},
                {"stage": "read_qc", "allow_local_execution": True},
            ]
        }
    )
    steps = workflow_steps_from_run_config(config)
    ledger = Ledger(run)
    ledger.create_session("audit", "control/manifest.yaml")
    for step in steps[:-1]:
        ledger.record_stage_completed(
            "audit", step["stage"], workflow_step_id=step["step_id"]
        )
    monkeypatch.setattr(
        audit,
        "validate_frozen_inputs",
        lambda _: SimpleNamespace(run_config=config, file_sha256=lambda key: "0" * 64),
    )
    monkeypatch.setattr(
        audit, "inspect_session", lambda *args: {"supervision_state": "blocked_human"}
    )
    report = audit.audit_route(run)
    assert report["occurrences"][1]["outcome"] == "stage_completed"
    assert report["occurrences"][2]["outcome"] == "not_terminal"
    assert any(
        "step-002-read_qc: not_terminal" == gap for gap in report["coverage_gaps"]
    )
    assert any(
        "step-002-read_qc: missing method disposition:" in gap
        for gap in report["coverage_gaps"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_source",
        "wrong_input",
        "duplicate_delivery",
        "reused_changed_source",
        "missing_final",
        "missing_evaluator",
        "wrong_candidate",
        "different_settings",
    ],
)
def test_completed_slice_rejects_broken_evidence(completed_slice, fault):
    run, steps, rows, deliveries, methods, snapshot, quality = completed_slice("review")
    last = steps[-1]["step_id"]
    if fault == "missing_source":
        rows[:] = [
            r
            for r in rows
            if not (
                r["stage"] == "inventory" and r["event_type"] == "resource_delivered"
            )
        ]
    elif fault == "wrong_input":
        for method in methods[last]:
            method["processes"][0]["input_artifacts"] = []
    elif fault == "duplicate_delivery":
        rows.append(
            next(
                r
                for r in rows
                if r["event_type"] == "resource_delivered"
                and r["workflow_step_id"] == last
            )
        )
    elif fault == "reused_changed_source":
        # Review may select an alternative; the reuse identity restriction is
        # checked against a non-Review producer instead.
        run, steps, rows, deliveries, methods, snapshot, quality = completed_slice(
            "hic"
        )
        deliveries[steps[-1]["step_id"]]["delivery_mode"] = "reused"
    elif fault == "missing_final":
        snapshot["final_assembly"] = None
    elif fault == "missing_evaluator":
        methods[last].pop()
    elif fault == "wrong_candidate":
        methods[last][0]["processes"][0]["candidate_id"] = "unrelated"
    else:
        quality["candidates"].append({"candidate_id": "second"})
        for method in methods[last]:
            method["processes"].append(
                {
                    **method["processes"][0],
                    "candidate_id": "second",
                    "evaluation_settings": {"threads": 2},
                }
            )
    with pytest.raises(ValueError):
        audit.audit_route(run)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "binding",
        "no_candidates",
        "duplicate",
        "missing_signal",
        "missing_metrics",
        "missing_refs",
    ],
)
def test_final_evaluation_validates_single_candidate_packet(
    tmp_path, monkeypatch, fault
):
    from app.schemas.artifacts import ArtifactRef
    from app.storage.paths import SessionPaths

    paths = SessionPaths.from_run_dir(v2_run_dir(tmp_path, "audit"))
    ref = ArtifactRef(
        artifact_id="fixture",
        path_ref="artifacts/packet.json",
        sha256="0" * 64,
        size_bytes=1,
        artifact_type="assembly_review.review_packet",
    )
    request = {
        "workflow_step_id": "step-001-assembly_review",
        "gate_id": "assembly_review",
        "payload": {"report_refs": {"review_packet": ref.path_ref}},
    }
    candidate = {
        "candidate_id": "source",
        "signals": {"available": ["quast", "busco"]},
        "metrics": {"quast": {"n50": 12}, "busco": {"complete": 10}},
        "metric_refs_by_signal": {"quast": ["quast.json"], "busco": ["busco.json"]},
    }
    packet = {"candidates": [candidate]}
    if fault == "binding":
        request["workflow_step_id"] = "another"
    elif fault == "no_candidates":
        packet["candidates"] = []
    elif fault == "duplicate":
        packet["candidates"].append(candidate)
    elif fault == "missing_signal":
        candidate["signals"]["available"].pop()
    elif fault == "missing_metrics":
        candidate["metrics"]["busco"] = {}
    elif fault == "missing_refs":
        candidate["metric_refs_by_signal"]["busco"] = []
    monkeypatch.setattr(audit, "load_review_request", lambda *args, **kwargs: request)
    monkeypatch.setattr(audit, "artifact_ref_for_path", lambda *args, **kwargs: ref)
    monkeypatch.setattr(audit, "resolve_artifact_ref", lambda *args: paths.run_dir)
    monkeypatch.setattr(audit, "_json", lambda *args: packet)
    if fault:
        with pytest.raises(ValueError):
            audit.final_evaluation(paths, "step-001-assembly_review", ref)
    else:
        quality = audit.final_evaluation(paths, "step-001-assembly_review", ref)
        assert len(quality["candidates"]) == 1
        assert quality["independent_qv"] is None


@pytest.mark.parametrize("broken_handoff", [False, True])
def test_complete_audit_requires_input_chain_and_final_pair(
    tmp_path, monkeypatch, broken_handoff
) -> None:
    from app.schemas.artifacts import ArtifactRef
    from app.workflow import ASSEMBLY_HANDOFF_PRODUCER_STAGES

    run = v2_run_dir(tmp_path, "audit")
    config = load_run_config(
        ROOT / "docs/examples/hifi_complete_run_config.template.json"
    )
    steps = workflow_steps_from_run_config(config)
    request_ref = ArtifactRef(
        artifact_id="fixture",
        path_ref="artifacts/request.json",
        sha256="0" * 64,
        size_bytes=1,
        artifact_type="assembly_review.request",
    ).model_dump(mode="json")
    rows, deliveries = [], {}
    for step in steps:
        if step["stage"] in ASSEMBLY_HANDOFF_PRODUCER_STAGES - {"inventory"}:
            rows.append(
                {
                    "event_type": "resource_delivered",
                    "stage": step["stage"],
                    "workflow_step_id": step["step_id"],
                }
            )
            deliveries[step["step_id"]] = {
                "delivery_mode": "materialized",
                "candidate_id": "polished",
                "primary_output_ref": {"producer": step["step_id"]},
            }
        rows.append(
            {
                "event_type": "stage_completed",
                "stage": step["stage"],
                "workflow_step_id": step["step_id"],
            }
        )
    monkeypatch.setattr(
        audit,
        "validate_frozen_inputs",
        lambda _: SimpleNamespace(run_config=config, file_sha256=lambda key: "0" * 64),
    )
    monkeypatch.setattr(
        audit,
        "inspect_session",
        lambda *args: {
            "supervision_state": "completed",
            "final_assembly": {"path": "final.fa"},
            "latest_review_request_artifact": request_ref,
        },
    )
    monkeypatch.setattr(audit, "read_artifact_records", lambda *args: [])
    monkeypatch.setattr(Ledger, "read_event_rows", lambda *args: rows)
    monkeypatch.setattr(
        audit, "verify_delivery", lambda _, row: deliveries[row["workflow_step_id"]]
    )
    monkeypatch.setattr(
        audit,
        "final_comparison",
        lambda *args: {
            "candidates": [{"candidate_id": "source"}, {"candidate_id": "polished"}],
        },
    )

    def coverage(state, *_):
        index = state["workflow_step_index"]
        if index <= 3:
            return [], []
        prior = deliveries[steps[index - 1]["step_id"]]["primary_output_ref"]
        refs = [] if broken_handoff and index == 9 else [prior]
        return [
            {
                "method_name": name,
                "processes": [
                    {
                        "success": True,
                        "collected": True,
                        "candidate_id": candidate,
                        "input_artifacts": refs,
                        "evaluation_settings": {"threads": 1},
                    }
                    for candidate in ("source", "polished")
                ],
            }
            for name in ("assembly_review.eval.quast", "assembly_review.eval.busco")
        ], []

    monkeypatch.setattr(audit, "execution_coverage", coverage)
    if broken_handoff:
        with pytest.raises(ValueError, match="no execution consumes preceding"):
            audit.audit_route(run)
    else:
        report = audit.audit_route(run)
        assert report["status"] == "complete_execution_coverage"
        assert len(report["occurrences"]) == 11
