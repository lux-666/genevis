"""Streaming sequence input and TGS-GapCloser segment consistency checks."""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
            raise ValueError("GapCloser requires declared fasta or fastq format")
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


def validate_gap_outputs(source: Path, output: Path, details: Path) -> dict[str, Any]:
    """Check upstream 1-based inclusive S/N/F coordinates against both FASTAs.

    Source intervals may be trimmed only across one anchored internal gap,
    including overlap closure with no F segment. Unfilled N segments consume
    the same source Ns, and source prefixes/suffixes cannot disappear.
    Biological accuracy still requires independent evaluation.
    """
    before, after = fasta_records(source), fasta_records(output)
    if set(before) != set(after):
        raise ValueError("GapCloser output scaffold identities differ from input")
    for scaffold, sequence in before.items():
        if "N" not in sequence.strip("N") and after[scaffold] != sequence:
            raise ValueError("scaffold without internal N-gaps was modified")
    seen: set[str] = set()
    name: str | None = None
    next_position = 1
    source_end = 0
    pending_fill = False
    previous_kind = ""
    copied = copied_non_n = filled = inserted = 0

    def finish() -> None:
        if name is not None and next_position != len(after[name]) + 1:
            raise ValueError("gap details do not cover the complete output scaffold")
        if pending_fill:
            raise ValueError("filled segment lacks a following source anchor")
        if name is not None and source_end != len(before[name]):
            raise ValueError("gap details discard the source scaffold suffix")

    with details.open(encoding="ascii") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                finish()
                fields = line[1:].split()
                if not fields or fields[0] not in after or fields[0] in seen:
                    raise ValueError("invalid or duplicate gap-details scaffold")
                name = fields[0]
                seen.add(name)
                next_position, source_end = 1, 0
                previous_kind = ""
                continue
            fields = line.split()
            if name is None or len(fields) not in {3, 5}:
                raise ValueError("invalid gap-details segment")
            start, end = int(fields[0]), int(fields[1])
            kind = fields[2]
            if start != next_position or not start <= end <= len(after[name]):
                raise ValueError("invalid gap-details output coordinates")
            segment = after[name][start - 1 : end]
            if kind == "S" and len(fields) == 5:
                first, last = int(fields[3]), int(fields[4])
                if not source_end < first <= last <= len(before[name]):
                    raise ValueError("invalid gap-details source coordinates")
                if segment != before[name][first - 1 : last]:
                    raise ValueError("copied source segment differs from output")
                omitted = before[name][source_end : first - 1]
                if pending_fill or omitted:
                    # Positive fills and zero/negative-length overlap closures
                    # must bridge exactly one internal source gap. Trimming may
                    # touch its flanks, but cannot discard a prefix, an entire
                    # intervening contig, or sequence beyond an unfilled N run.
                    if (
                        previous_kind not in {"S", "F"}
                        or not source_end
                        or before[name][source_end - 1] == "N"
                        or segment[0] == "N"
                        or re.fullmatch(r"[^N]*N+[^N]*", omitted) is None
                    ):
                        raise ValueError(
                            "source deletion/fill does not bridge one anchored N-gap"
                        )
                pending_fill = False
                source_end = last
                copied += len(segment)
                copied_non_n += len(segment) - segment.count("N")
            elif kind == "N" and len(fields) == 3:
                if pending_fill or set(segment) != {"N"}:
                    raise ValueError("gap-details N segment contains non-N bases")
                if before[name][source_end : source_end + len(segment)] != segment:
                    raise ValueError("unfilled N segment does not match source gap")
                source_end += len(segment)
            elif kind == "F" and len(fields) == 3:
                if previous_kind != "S":
                    raise ValueError("filled segment lacks a preceding source anchor")
                if "N" in segment:
                    raise ValueError("filled segment still contains N bases")
                pending_fill = True
                filled += 1
                inserted += len(segment)
            else:
                raise ValueError("unsupported gap-details segment type/shape")
            next_position = end + 1
            previous_kind = kind
    finish()
    if seen != set(after):
        raise ValueError("missing scaffold in gap details")
    return {
        "schema_version": 1,
        "input": sequence_summary(before),
        "output": sequence_summary(after),
        "filled_segments": filled,
        "inserted_bases": inserted,
        "copied_source_bases": copied,
        "removed_source_non_n_bases": sum(
            len(seq) - seq.count("N") for seq in before.values()
        )
        - copied_non_n,
        "sequence_changed": before != after,
        "validation": "segment_consistency_only_not_consensus_accuracy",
    }
