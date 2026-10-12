"""Repeated paired QC must consume and freeze the preceding clean artifacts."""

import copy
import json
from pathlib import Path

import pytest

from app.stages.read_qc.builder import build_stage
from app.stages.read_qc.nodes import resolve_raw_long_reads
from app.storage.artifacts import read_artifact_records
from tests import test_read_qc as fixture


@pytest.mark.parametrize("family", ["short_reads", "hic_reads"])
def test_second_paired_qc_occurrence_uses_first_clean_artifacts(tmp_path, family):
    helper = fixture.ReadQCSeqKitStageTests()
    state = helper._short_pair_state(
        str(tmp_path),
        asset_type=(
            "raw_short_dna_reads" if family == "short_reads" else "hic_reads_registered"
        ),
    )
    method = f"read_qc.{family}.fastp"
    state["workflow_steps"][1]["selected_method_names"] = [method]
    state["workflow_steps"].append(
        {
            "step_id": "step-002-read_qc",
            "stage": "read_qc",
            "allow_local_execution": True,
            "selected_method_names": [method],
        }
    )
    seed = {
        "execution_mode": "local",
        "env": state["stage_outputs"]["read_qc"]["env"],
        f"{family}_request": {"threads": 2, "preserve_hic_junction": True},
    }
    state["stage_outputs"]["read_qc"] = copy.deepcopy(seed)
    first = build_stage().invoke(state)
    assert first["stage_outputs"]["read_qc"]["status"] == "completed"
    first_paths = first["resource_refs"][family]["paths"]
    first["workflow_step_index"] = 2
    first["entered_workflow_step_index"] = 2
    first["current_stage"] = "read_qc"
    first["stage_outputs"]["read_qc"] = copy.deepcopy(seed)
    second = build_stage().invoke(first)
    output = second["stage_outputs"]["read_qc"]
    assert output["status"] == "completed", output.get("reason")
    assert set(first_paths).isdisjoint(second["resource_refs"][family]["paths"])
    attempts = [
        json.loads((Path(second["data_dir"]) / row["path_ref"]).read_text())["attempt"]
        for row in read_artifact_records(second["data_dir"], second["session_id"])
        if row["artifact_type"] == "read_qc.frozen_attempt"
        and "step-002-read_qc" in row["path_ref"]
    ]
    assert len(attempts) == 1
    dependencies = attempts[0]["input_dependencies"]
    assert {dep["path"] for dep in dependencies} == set(first_paths)
    assert all(dep["kind"] == "artifact" for dep in dependencies)


def test_valid_short_reads_cannot_hide_corrupt_long_resource(tmp_path):
    state = fixture.ReadQCSeqKitStageTests()._short_pair_state(str(tmp_path))
    state["resource_refs"]["long_reads"] = {
        "resource_type": "long_reads",
        "paths": [str(tmp_path / "long.fastq")],
        "source_stage": "inventory",
        "workflow_step_id": "step-000-inventory",
        "metadata": {"asset_registry_ref": state["asset_registry_ref"]},
    }
    result = resolve_raw_long_reads(state)["stage_outputs"]["read_qc"]
    assert result["status"] == "failed"
    assert "must carry both" in result["reason"]
