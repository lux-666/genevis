"""Streaming sequence input and TGS-GapCloser segment consistency checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.stages.sequence_index import SequenceIndex
from app.stages.sequences import sequence_summary


def validate_gap_outputs(source: Path, output: Path, details: Path) -> dict[str, Any]:
    """Check upstream 1-based inclusive S/N/F coordinates against both FASTAs.

    Source intervals may be trimmed only across one anchored internal gap,
    including overlap closure with no F segment. Unfilled N segments consume
    the same source Ns, and source prefixes/suffixes cannot disappear.
    Biological accuracy still requires independent evaluation.
    """
    with SequenceIndex(source) as before, SequenceIndex(output) as after:
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
                raise ValueError(
                    "gap details do not cover the complete output scaffold"
                )
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
                                "source deletion/fill does not bridge "
                                "one anchored N-gap"
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
                        raise ValueError(
                            "filled segment lacks a preceding source anchor"
                        )
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
            "sequence_changed": any(before[name] != after[name] for name in before),
            "validation": "segment_consistency_only_not_consensus_accuracy",
        }
