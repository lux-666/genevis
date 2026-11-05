"""Disk sequence views retain order and reject duplicate identities."""

import pytest

from app.stages.sequence_index import SequenceIndex
from app.stages.sequences import fasta_records, sequence_summary


def test_index_agrees_with_parser_without_retaining_genome(tmp_path):
    path = tmp_path / "seq.fa"
    path.write_text(">b\nACNNGT\n>a\nAAAA\n")
    with SequenceIndex(path) as index:
        queries = []
        index._db.set_trace_callback(queries.append)
        lengths = index.lengths()
        assert dict(lengths) == {"b": 6, "a": 4}
        assert lengths.get("missing") is None
        assert index._cached is None
        assert not any("SELECT sequence " in sql for sql in queries)
        assert not any("length(sequence)" in sql for sql in queries)
        assert list(index) == ["b", "a"]
        assert len(index) == 2
        assert index["a"] == "AAAA"
        assert index["b"] == "ACNNGT"
        assert sequence_summary(index) == sequence_summary(fasta_records(path))
        with pytest.raises(KeyError):
            index["missing"]
        scratch = index._scratch.name
    from pathlib import Path

    assert not Path(scratch).exists()


def test_duplicate_rejected(tmp_path):
    path = tmp_path / "seq.fa"
    path.write_text(">a\nACGT\n>a\nAAAA\n")
    with pytest.raises(ValueError, match="duplicate"):
        SequenceIndex(path)
