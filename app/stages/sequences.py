"""Validated sequence readers and compact assembly summaries."""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator
from pathlib import Path

_BASES = frozenset("ACGTRYSWKMBDHVN")


def _sequence(parts: list[str]) -> str:
    sequence = "".join(parts).upper()
    if not sequence or not set(sequence) <= _BASES:
        raise ValueError("empty or non-IUPAC DNA sequence")
    return sequence


def read_sequences(path: Path, sequence_format: str) -> Iterator[tuple[str, str]]:
    """Validate plain/gzip FASTA or multiline FASTQ without retaining all reads."""
    with path.open("rb") as raw:
        compressed = raw.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    count = 0
    with opener(path, "rt", encoding="ascii") as handle:
        if sequence_format == "fasta":
            name = ""
            parts: list[str] = []
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                if line.startswith(">"):
                    if name:
                        count += 1
                        yield name, _sequence(parts)
                    fields = line[1:].split()
                    if not fields:
                        raise ValueError("empty FASTA identifier")
                    name, parts = fields[0], []
                elif not name:
                    raise ValueError("FASTA sequence before header")
                else:
                    parts.append(line)
            if name:
                count += 1
                yield name, _sequence(parts)
        elif sequence_format == "fastq":
            while header := handle.readline():
                fields = header.rstrip()[1:].split()
                if not header.startswith("@") or not fields:
                    raise ValueError("invalid FASTQ header")
                parts = []
                for line in handle:
                    if line.startswith("+"):
                        break
                    parts.append(line.rstrip("\r\n"))
                else:
                    raise ValueError("truncated FASTQ sequence")
                sequence = _sequence(parts)
                quality_length = 0
                while quality_length < len(sequence):
                    quality = handle.readline()
                    if not quality:
                        raise ValueError("truncated FASTQ quality")
                    quality = quality.rstrip("\r\n")
                    if any(not 33 <= ord(char) <= 126 for char in quality):
                        raise ValueError("invalid FASTQ quality")
                    quality_length += len(quality)
                if quality_length != len(sequence):
                    raise ValueError("FASTQ sequence/quality length mismatch")
                count += 1
                yield fields[0], sequence
        else:
            raise ValueError("Sequence input requires declared fasta or fastq format")
    if not count:
        raise ValueError("empty sequence input")


def fasta_records(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for name, sequence in read_sequences(path, "fasta"):
        if name in records:
            raise ValueError(f"duplicate scaffold identifier: {name}")
        records[name] = sequence
    return records


def sequence_summary(records: dict[str, str]) -> dict[str, int]:
    return {
        "scaffolds": len(records),
        "total_bases": sum(map(len, records.values())),
        "n_bases": sum(seq.count("N") for seq in records.values()),
        "internal_gaps": sum(
            len(re.findall(r"N+", seq.strip("N"))) for seq in records.values()
        ),
    }
