"""Read-back evidence must keep each terminal candidate's conflicts separate."""

import csv
import json

import pytest

from app.stages.t2t.reconstruction import readback_evidence


@pytest.mark.parametrize("conflicting_end", ["left", "right"])
def test_competing_read_only_affects_the_overlapping_terminal_candidate(
    tmp_path, conflicting_end
):
    (tmp_path / "reconstructed_assembly.fa").write_text(">chr1\n" + "A" * 1000 + "\n")
    plans = [
        {
            "candidate_id": end,
            "target": "chr1",
            "end": end,
            "tail": "A" * 100,
            "junction": junction,
            "construction_reads": ["construction_a", "construction_b"],
        }
        for end, junction in (("left", 100), ("right", 900))
    ]
    (tmp_path / "reconstruction.json").write_text(
        json.dumps({"schema_version": "t2t.reconstruction.v1", "plans": plans})
    )
    (tmp_path / "reads.tsv").write_text("read_id\nleft_read\nright_read\n")
    (tmp_path / "readback.paf").write_text(
        "left_read\t200\t0\t200\t+\tchr1\t1000\t0\t200\t200\t200\t60\tcg:Z:200M\n"
        "right_read\t200\t0\t200\t+\tchr1\t1000\t800\t1000\t200\t200\t60\tcg:Z:200M\n"
        f"{conflicting_end}_read\t200\t0\t200\t+\tchr1\t1000\t400\t600\t200\t200\t0\tcg:Z:200M\n"
    )
    readback_evidence(
        {
            "work_dir": str(tmp_path),
            "reads_scan": str(tmp_path / "reads.tsv"),
            "thresholds": {"min_mapq": 20, "min_supporting_reads": 1},
            "motif": "TTTAGGG",
        }
    )
    with (tmp_path / "readback_validation.tsv").open(newline="") as handle:
        evidence = {
            row["candidate_id"]: row for row in csv.DictReader(handle, delimiter="\t")
        }
    unaffected_end = "right" if conflicting_end == "left" else "left"
    unaffected = evidence[unaffected_end]
    assert unaffected["competing_reads"] == "0"
    assert unaffected["extension_coverage_fraction"] == "1.000000"
    assert unaffected["junction_spanning_molecules"] == "1"
    assert unaffected["quality_status"] == "supported_in_sample"
    assert unaffected["quality_reasons"] == ""
    conflicting = evidence[conflicting_end]
    assert conflicting["competing_reads"] == "1"
    assert conflicting["quality_status"] == "evidence_insufficient"
    assert "competing_readback_placements" in conflicting["quality_reasons"]
