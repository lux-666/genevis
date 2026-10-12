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


def test_iupac_reverse_complement():
    assert reverse_complement("AARY") == "RYTT"
    assert reverse_complement("acgtryswkmbdhvn") == "NBDHVKMWSRYACGT"
    assert (
        reverse_complement(reverse_complement("ACGTRYSWKMBDHVN")) == "ACGTRYSWKMBDHVN"
    )
    with pytest.raises(ValueError):
        reverse_complement("AX")
