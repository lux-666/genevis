"""Streaming observations preserve complete counts beyond the display cap."""

import pytest

from app.stages.finishing.observations import measure_assembly
from app.stages.sequences import fasta_records, sequence_summary


def test_gap_counts_and_examples_are_independent(tmp_path):
    path = tmp_path / "assembly.fa"
    path.write_text("".join(f">s{i}\nNNACNNGTNN\n" for i in range(140)))
    observed = measure_assembly(path, stage="gapcloser")
    assert observed["sequence"] == sequence_summary(fasta_records(path))
    assert observed["gaps"]["count"] == 140
    assert observed["gaps"]["total_bases"] == 280
    assert len(observed["gaps"]["examples"]) == 100
    assert observed["gaps"]["examples_truncated"]
    assert observed["gaps"]["examples"][0] == {
        "sequence": "s0",
        "start": 4,
        "end": 6,
        "length": 2,
    }


def test_terminal_count_and_validation_continue_past_cap(tmp_path):
    path = tmp_path / "assembly.fa"
    path.write_text("".join(f">s{i}\nTTAGGGTTAGGGACGT\n" for i in range(110)))
    observed = measure_assembly(path, stage="t2t", motif="TTAGGG")
    assert observed["terminal_observations"]["count"] == 110
    assert len(observed["terminal_observations"]["examples"]) == 100
    with path.open("a") as handle:
        handle.write(">s0\nACGT\n")
    with pytest.raises(ValueError, match="duplicate"):
        measure_assembly(path, stage="t2t", motif="TTAGGG")
