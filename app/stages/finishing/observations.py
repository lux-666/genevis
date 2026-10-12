"""Bounded-memory assembly observations with complete counts and capped examples."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.stages.sequences import read_sequences
from app.stages.t2t.algorithms import max_motif_copies


def measure_assembly(
    path: Path, *, stage: str, motif: str | None = None
) -> dict[str, Any]:
    if stage == "t2t" and not motif:
        raise ValueError("terminal observations require a resolved motif")
    summary = {"scaffolds": 0, "total_bases": 0, "n_bases": 0, "internal_gaps": 0}
    examples: list[dict[str, Any]] = []
    gap_bases = 0
    seen: set[str] = set()
    for name, sequence in read_sequences(path, "fasta"):
        if name in seen:
            raise ValueError(f"duplicate scaffold identifier: {name}")
        seen.add(name)
        summary["scaffolds"] += 1
        summary["total_bases"] += len(sequence)
        summary["n_bases"] += sequence.count("N")
        for match in re.finditer("N+", sequence):
            if match.start() == 0 or match.end() == len(sequence):
                continue
            summary["internal_gaps"] += 1
            gap_bases += match.end() - match.start()
            if stage == "gapcloser" and len(examples) < 100:
                examples.append(
                    {
                        "sequence": name,
                        "start": match.start(),
                        "end": match.end(),
                        "length": match.end() - match.start(),
                    }
                )
        if stage == "t2t" and len(examples) < 100:
            assert motif is not None
            examples.append(
                {
                    "sequence": name,
                    "length": len(sequence),
                    "left_max_motif_copies": max_motif_copies(sequence[:10000], motif),
                    "right_max_motif_copies": max_motif_copies(
                        sequence[-10000:], motif
                    ),
                }
            )
    result: dict[str, Any] = {"sequence": summary}
    if stage == "gapcloser":
        result["gaps"] = {
            "count": summary["internal_gaps"],
            "total_bases": gap_bases,
            "examples": examples,
            "examples_truncated": summary["internal_gaps"] > 100,
        }
    elif stage == "t2t":
        result["terminal_observations"] = {
            "motif": motif,
            "measurement_window_bp": 10000,
            "count": summary["scaffolds"],
            "examples": examples,
            "examples_truncated": summary["scaffolds"] > 100,
        }
    return result
