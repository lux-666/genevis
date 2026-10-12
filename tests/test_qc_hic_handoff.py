"""QC Hi-C artifacts remain immutable command dependencies in AutoHiC."""

import os
from unittest.mock import patch

from app.execution.evidence import load_frozen_attempt
from app.schemas.artifacts import ParameterAttempt
from app.stages.hic_scaffolding.method_application.autohic import freeze_autohic_attempt
from app.stages.read_qc.builder import build_stage
from tests import test_hic_scaffolding as hic_fixture
from tests import test_read_qc as qc_fixture


def test_fastp_hic_to_approved_autohic_freezes_clean_artifacts(tmp_path):
    helper = hic_fixture.HiCScaffoldingStageTests()
    assembly, reads, error_model, chromosome_model = helper._inputs(str(tmp_path))
    state = helper._configured_state(str(tmp_path), assembly, reads)
    original_steps = state["workflow_steps"]
    state["workflow_steps"] = [
        original_steps[0],
        {
            "step_id": "step-001-read_qc",
            "stage": "read_qc",
            "allow_local_execution": True,
            "selected_method_names": ["read_qc.hic_reads.fastp"],
        },
        original_steps[2],
    ]
    state["workflow_step_index"] = 1
    state["entered_workflow_step_index"] = 1
    state["current_stage"] = "read_qc"
    fastp_bin = qc_fixture.ReadQCSeqKitStageTests()._fake_fastp_bin(str(tmp_path))
    state["stage_outputs"]["read_qc"] = {
        "execution_mode": "local",
        "env": {"PATH": f"{fastp_bin}{os.pathsep}{os.environ['PATH']}"},
        "hic_reads_request": {"preserve_hic_junction": True},
    }
    cleaned = build_stage().invoke(state)
    assert cleaned["stage_outputs"]["read_qc"]["status"] == "completed", cleaned[
        "stage_outputs"
    ]["read_qc"].get("reason")
    clean_paths = cleaned["resource_refs"]["hic_reads"]["paths"]
    assert set(clean_paths).isdisjoint(map(str, reads))
    cleaned["workflow_step_index"] = 2
    cleaned["entered_workflow_step_index"] = 2
    cleaned["current_stage"] = "hic_scaffolding"
    envs = helper._fake_autohic_environment(
        str(tmp_path), error_model, chromosome_model
    )
    approved = helper._approve_parameters(helper._run_to_parameter_gate(cleaned))
    with patch.dict(os.environ, {"GENE_AGENT_ENVS_CONFIG": str(envs)}):
        update = freeze_autohic_attempt(approved)
    frozen = {**approved, **update}
    output = frozen["stage_outputs"]["hic_scaffolding"]
    assert output["status"] == "autohic_frozen", output
    attempt = ParameterAttempt.model_validate(output["parameter_attempts"][0])
    contract = load_frozen_attempt(
        frozen,
        attempt.frozen_attempt_ref,
        expected_stage="hic_scaffolding",
        expected_method_name=hic_fixture.AUTOHIC_METHOD,
    )
    clean_deps = [dep for dep in contract.input_dependencies if dep.path in clean_paths]
    assert len(clean_deps) == len(clean_paths)
    assert all(dep.kind == "artifact" for dep in clean_deps)
