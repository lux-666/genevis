"""One DNA grammar and orientation boundary for all stages."""

import gzip

import pytest

from app.stages.sequences import fasta_records, read_sequences, reverse_complement


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize(
    "content", [">r1\nAARY\nSWKM\n", "@r1\nAARY\nSWKM\n+\n!!!!\n!!!!\n"]
)
def test_multiline_and_gzip_by_content(tmp_path, compressed, content):
    path = tmp_path / "reads.data"
    path.write_bytes(
        gzip.compress(content.encode()) if compressed else content.encode()
    )
    assert list(read_sequences(path)) == [("r1", "AARYSWKM")]


@pytest.mark.parametrize(
    "content",
    [
        "@r\nACGT\n+\n!!\n",
        "@r\nACGT\n+\n!!!!!\n",
        "@r\nACGX\n+\n!!!!\n",
        ">\nACGT\n",
        "@\nACGT\n+\n!!!!\n",
        ">r\n",
        "@r\nACGT\n",
        "",
    ],
)
def test_invalid_sequences_are_rejected(tmp_path, content):
    path = tmp_path / "reads"
    path.write_text(content)
    with pytest.raises(ValueError):
        list(read_sequences(path))


def test_duplicate_scaffold_rejected(tmp_path):
    path = tmp_path / "assembly"
    path.write_text(">same\nACGT\n>same\nACGT\n")
    with pytest.raises(ValueError, match="duplicate"):
        fasta_records(path)

    from app.stages.t2t.nodes import _scan_fasta_ends

    with pytest.raises(ValueError, match="duplicate"):
        list(
            _scan_fasta_ends(
                path, "ACGT", {"terminal_window_bp": 4, "min_motif_copies": 1}
            )
        )


def test_iupac_reverse_complement():
    assert reverse_complement("AARY") == "RYTT"
    assert reverse_complement("acgtryswkmbdhvn") == "NBDHVKMWSRYACGT"
    assert (
        reverse_complement(reverse_complement("ACGTRYSWKMBDHVN")) == "ACGTRYSWKMBDHVN"
    )
    with pytest.raises(ValueError):
        reverse_complement("AX")


def test_t2t_selected_reads_still_validate_unselected_duplicates(tmp_path):
    from app.stages.t2t.nodes import _load_telomeric_read_sequences

    reads = tmp_path / "telomeric.fa"
    reads.write_text(">selected\nACGT\n>unselected\nTTTT\n")
    assert _load_telomeric_read_sequences(
        {"map-hifi": str(reads)}, selected_ids={"selected"}
    ) == {"selected": "ACGT"}
    reads.write_text(reads.read_text() + ">unselected\nAAAA\n")
    with pytest.raises(ValueError, match="duplicate"):
        _load_telomeric_read_sequences(
            {"map-hifi": str(reads)}, selected_ids={"selected"}
        )
