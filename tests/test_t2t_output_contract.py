"""Software contracts only: artifact binding, frozen code and error propagation."""

import csv
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.control.errors import ResumeValidationError
from app.integrity import sha256_file
from app.stages.finishing.advisor import reconstruction_summary, table_summary
from app.stages.t2t.nodes import apply_t2t_patches
from app.stages.t2t.reconstruction import (
    VALIDATION_FIELDS,
    validate_reconstruction_outputs,
)
from app.stages.t2t.review import _prepare_t2t_selected_output
from app.stages.t2t.script import reconstruction_script


def output_files(tmp_path: Path, *, empty: bool = False):
    assembly = tmp_path / "candidate.fa"
    assembly.write_text(">record\nACGTACGT\n")
    metadata = tmp_path / "reconstruction.json"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": "t2t.reconstruction.v1",
                "candidates": []
                if empty
                else [
                    {
                        "candidate_id": "record:right",
                        "decision": "proposed",
                        "consensus_sequence": "ACGT",
                    }
                ],
                "observations": [],
                "plans": []
                if empty
                else [
                    {
                        "candidate_id": "record:right",
                        "target": "record",
                        "end": "right",
                        "junction": 4,
                        "tail": "ACGT",
                    }
                ],
            }
        )
    )
    validation = tmp_path / "validation.tsv"
    with validation.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VALIDATION_FIELDS, delimiter="\t")
        writer.writeheader()
        if not empty:
            writer.writerow(
                {
                    "candidate_id": "record:right",
                    "candidate_sha256": sha256_file(assembly),
                    "quality_status": "evidence_insufficient",
                }
            )
    return metadata, assembly, validation


@pytest.mark.parametrize("empty", [False, True])
def test_output_contract_accepts_bound_output_and_no_candidates(tmp_path, empty):
    files = output_files(tmp_path, empty=empty)
    result = validate_reconstruction_outputs(*files)
    assert bool(result["plans"]) is not empty


def test_reconstruction_summary_is_bounded_and_versioned(tmp_path):
    metadata, _assembly, _validation = output_files(tmp_path)
    summary = reconstruction_summary(metadata)
    assert summary["schema_version"] == "t2t.reconstruction.v1"
    assert summary["candidate_rows"] == 1
    assert summary["observation_rows"] == 0
    assert summary["plan_count"] == 1
    assert "consensus_sequence" not in summary


def test_reconstruction_summary_rejects_unknown_version(tmp_path):
    metadata, _assembly, _validation = output_files(tmp_path)
    metadata.write_text(metadata.read_text().replace("t2t.reconstruction.v1", "v0"))
    with pytest.raises(ValueError, match="unsupported reconstruction schema_version"):
        reconstruction_summary(metadata)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("root", "must be an object"),
        ("missing_table", "invalid reconstruction"),
        ("wrong_value", "must be strings"),
        ("missing_plan", "plans differ"),
        ("duplicate_plan", "plans differ"),
        ("changed_plan", "differs from candidate sequence"),
        ("changed_hash", "validation differs"),
        ("missing_evidence", "validation differs"),
        ("wrong_header", "invalid read-back validation header"),
        ("duplicate_record", "duplicate reconstructed scaffold"),
        ("schema", "unsupported reconstruction schema_version"),
    ],
)
def test_output_contract_rejects_inconsistent_artifacts(tmp_path, mutation, match):
    metadata, assembly, validation = output_files(tmp_path)
    data = json.loads(metadata.read_text())
    if mutation == "root":
        data = []
    elif mutation == "missing_table":
        del data["observations"]
    elif mutation == "wrong_value":
        data["candidates"][0]["decision"] = 1
    elif mutation == "missing_plan":
        data["plans"] = []
    elif mutation == "duplicate_plan":
        data["plans"] *= 2
    elif mutation == "changed_plan":
        data["plans"][0]["tail"] = "AAAA"
    elif mutation == "changed_hash":
        validation.write_text(
            validation.read_text().replace(sha256_file(assembly), "0" * 64)
        )
    elif mutation == "missing_evidence":
        validation.write_text(validation.read_text().splitlines()[0] + "\n")
    elif mutation == "wrong_header":
        validation.write_text("wrong\n")
    elif mutation == "duplicate_record":
        assembly.write_text(assembly.read_text() * 2)
    elif mutation == "schema":
        data["schema_version"] = "t2t.reconstruction.v0"
    metadata.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=match):
        validate_reconstruction_outputs(metadata, assembly, validation)


def test_frozen_worker_is_self_contained_and_shell_quotes_paths(tmp_path):
    config = {
        "work_dir": str(tmp_path / "work ' space"),
        "assembly": str(tmp_path / "$(touch unexpected) ' input.fa"),
        "reads": {"map-pb": str(tmp_path / "reads file.fa")},
    }
    script = reconstruction_script(config)
    assert "from app." not in script
    assert "def reconstruct(" in script
    assert "def readback_evidence(" in script
    assert script.count("minimap2 -c -x map-pb --secondary=yes") == 2
    assert "minimap2 --version" in script
    # Parse only: never launch a biological tool in this test.
    checked = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True
    )
    assert checked.returncode == 0, checked.stderr


def test_materialization_error_is_failure_not_source_retention():
    with (
        patch(
            "app.stages.t2t.nodes._output", return_value={"status": "candidates_built"}
        ),
        patch(
            "app.stages.t2t.nodes.load_t2t_frozen_execution_inputs",
            return_value=SimpleNamespace(mode="auto_patch"),
        ),
        patch(
            "app.stages.t2t.nodes._materialize_t2t_patches",
            return_value={"error": "missing_selected_output"},
        ),
        patch("app.stages.t2t.nodes._fail", return_value={"failed": True}) as fail,
    ):
        assert apply_t2t_patches({}) == {"failed": True}
    assert "missing_selected_output" in fail.call_args.args[1]


@pytest.mark.parametrize("partial", [True, False])
def test_manual_adoption_binds_complete_validated_output(tmp_path, partial):
    original = tmp_path / "validated.fa"
    original.write_text("validated bytes")
    changed = tmp_path / "materialized.fa"
    changed.write_text("different bytes")
    out = {
        "telomere_extension_candidates_ref": "candidates.tsv",
        "reconstructed_assembly_ref": str(original),
    }
    rows = [
        {
            "candidate_id": key,
            "decision": "proposed",
            "reconstruction_method": "poa_consensus",
        }
        for key in ("one", "two")
    ]
    inputs = SimpleNamespace(mode="manual_review", input_assembly_ref="source.fa")
    with (
        patch("app.stages.t2t.review.absolute_data_dir", return_value=tmp_path),
        patch("app.stages.t2t.review._assembly_input_dependency"),
        patch("app.stages.t2t.review.artifact_ref_for_path"),
        patch("app.stages.t2t.review.artifact_input_dependency"),
        patch("app.stages.t2t.review.verify_input_dependencies"),
        patch("app.stages.t2t.review._read_tsv", return_value=rows),
        patch(
            "app.stages.t2t.review._materialize_t2t_patches",
            return_value={"patched_assembly_ref": str(changed)},
        ) as materialize,
    ):
        with pytest.raises(
            ResumeValidationError,
            match="complete candidate"
            if partial
            else "differs from read-back candidate",
        ):
            _prepare_t2t_selected_output(
                {"session_id": "software-test"},
                out,
                inputs,
                decision="accept_selected_candidates",
                accepted_candidate_ids=["one"] if partial else ["one", "two"],
            )
        assert materialize.call_count == (0 if partial else 1)


def test_advisor_preserves_json_read_ids_and_fractional_metrics(tmp_path):
    table = tmp_path / "evidence.tsv"
    with table.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "supporting_reads",
                "supporting_read_ids",
                "extension_mean_depth",
                "quality_status",
                "quality_reasons",
            ],
            delimiter="\t",
        )
        writer.writeheader()
        writer.writerow(
            {
                "supporting_reads": "a,b,c",
                "supporting_read_ids": '["a,b", "c"]',
                "extension_mean_depth": "1.25",
                "quality_status": "evidence_insufficient",
                "quality_reasons": "reason_a;reason_b",
            }
        )
    result = table_summary(table)
    assert result["numeric"]["supporting_read_count"]["mean"] == 2
    assert result["numeric"]["extension_mean_depth"]["mean"] == 1.25
    assert result["counts"]["quality_reasons"] == {"reason_a": 1, "reason_b": 1}


def test_advisor_does_not_copy_large_payloads_into_examples(tmp_path):
    table = tmp_path / "payload.tsv"
    table.write_text(
        "candidate_id\tconsensus_sequence\tbaseline_sequence\tcigar\textension_length\n"
        "record:right\tACGT\tACGT\t4M\t4\n"
    )
    result = table_summary(table)
    assert result["examples"] == [
        {"candidate_id": "record:right", "extension_length": "4"}
    ]
    assert result["numeric"]["extension_length"]["mean"] == 4
