"""Server-entry contracts; fixtures are not representative biological acceptance."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.control.workflow_plan import workflow_steps_from_run_config
from app.diagnostics import route_acceptance as audit
from app.schemas.manifest import InputManifest
from app.schemas.run_config import load_run_config
from app.storage.ledger import Ledger
from app.workflow import recommended_workflow_template
from tests.data_v2 import v2_run_dir

ROOT = Path(__file__).resolve().parents[1]


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
    assert effective_method_closure(config)["base_polish"] == ("base_polish.racon",)
    assert effective_method_closure(config)["gapcloser"] == ("gapcloser.tgs_gapcloser",)
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
        audit, "validate_frozen_inputs", lambda _: SimpleNamespace(run_config=config)
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


def test_non_complete_plan_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = v2_run_dir(tmp_path, "audit")
    config = load_run_config(ROOT / "docs/examples/contig_run_config.template.json")
    monkeypatch.setattr(
        audit, "validate_frozen_inputs", lambda _: SimpleNamespace(run_config=config)
    )
    with pytest.raises(ValueError, match="not the complete"):
        audit.audit_route(run)


def test_cli_invalid_evidence_is_json_and_nonzero(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "sys.argv", ["route_acceptance", "--run-dir", str(tmp_path / "runs" / "absent")]
    )
    assert audit.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_evidence"


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
        audit, "validate_frozen_inputs", lambda _: SimpleNamespace(run_config=config)
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
                "processes": [
                    {
                        "success": True,
                        "collected": True,
                        "candidate_id": candidate,
                        "input_artifacts": refs,
                        "evaluation_settings": {"threads": 1},
                    }
                    for candidate in ("source", "polished")
                ]
            }
        ], []

    monkeypatch.setattr(audit, "execution_coverage", coverage)
    if broken_handoff:
        with pytest.raises(ValueError, match="no execution consumes preceding"):
            audit.audit_route(run)
    else:
        report = audit.audit_route(run)
        assert report["status"] == "complete_execution_coverage"
        assert len(report["occurrences"]) == 11
