"""Pure telomere support and PAF algorithms, independent of graph and storage."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.stages.sequences import reverse_complement


@dataclass(frozen=True)
class PafRecord:
    query: str
    query_length: int
    query_start: int
    query_end: int
    strand: str
    target: str
    target_length: int
    target_start: int
    target_end: int
    mapq: int

    @property
    def overlap_bp(self) -> int:
        return self.query_end - self.query_start


def terminal_signal(
    sequence: str, motif: str, thresholds: dict[str, int]
) -> tuple[int, int]:
    window = min(len(sequence), int(thresholds["terminal_window_bp"]))
    return max_motif_copies(sequence[:window], motif), max_motif_copies(
        sequence[-window:], motif
    )


def max_motif_copies(sequence: str, motif: str) -> int:
    alternatives = [re.escape(motif), re.escape(reverse_complement(motif))]
    match_lengths = [
        len(match.group(0)) // len(motif)
        for match in re.finditer(f"(?:{'|'.join(alternatives)})+", sequence)
    ]
    return max(match_lengths, default=0)


def candidate_rows(
    paf_path: Path,
    assembly_ends: dict[str, dict[str, str]],
    read_signals: dict[str, dict[str, str]],
    read_sequences: dict[str, str],
    thresholds: dict[str, int],
    motif: str,
    allowed_sequences: set[str] | None,
    mode: str,
    *,
    observations: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for paf in iter_paf(paf_path):
        signal = read_signals.get(paf.query)
        sequence = read_sequences.get(paf.query)
        target = assembly_ends.get(paf.target)
        for read_end in ("left", "right"):
            target_end = (
                read_end
                if paf.strand == "+"
                else ("right" if read_end == "left" else "left")
            )
            target_distance = (
                paf.target_start
                if target_end == "left"
                else paf.target_length - paf.target_end
            )
            tail_length = (
                paf.query_start
                if read_end == "left"
                else paf.query_length - paf.query_end
            )
            oriented_sequence = (
                (sequence or "")
                if paf.strand == "+"
                else reverse_complement(sequence or "")
            )
            oriented_query_start = (
                paf.query_start
                if paf.strand == "+"
                else paf.query_length - paf.query_end
            )
            oriented_query_end = (
                paf.query_end
                if paf.strand == "+"
                else paf.query_length - paf.query_start
            )
            tail = (
                oriented_sequence[:oriented_query_start]
                if target_end == "left"
                else oriented_sequence[oriented_query_end:]
            )
            tail_motif_copies = max_motif_copies(tail, motif)
            reasons = [
                reason
                for rejected, reason in (
                    (not signal, "missing_read_signal"),
                    (not sequence, "missing_read_sequence"),
                    (not target, "missing_assembly_end"),
                    (
                        bool(allowed_sequences and paf.target not in allowed_sequences),
                        "target_not_selected",
                    ),
                    (
                        (signal or {}).get(f"{read_end}_has_signal") != "true",
                        "read_end_without_telomere_signal",
                    ),
                    (
                        (target or {}).get(f"{target_end}_has_signal") == "true",
                        "target_end_already_has_telomere",
                    ),
                    (paf.mapq < thresholds["min_mapq"], "mapq_below_threshold"),
                    (
                        paf.overlap_bp < thresholds["min_overlap_bp"],
                        "overlap_below_threshold",
                    ),
                    (
                        target_distance > thresholds["max_target_terminal_distance_bp"],
                        "target_distance_above_threshold",
                    ),
                    (tail_length <= 0, "no_unaligned_tail"),
                    (
                        tail_motif_copies < thresholds["min_motif_copies"],
                        "tail_motif_below_threshold",
                    ),
                )
                if rejected
            ]
            if observations is not None:
                observations.append(
                    {
                        "read_id": paf.query,
                        "target": paf.target,
                        "read_end": read_end,
                        "target_end": target_end,
                        "strand": paf.strand,
                        "mapq": str(paf.mapq),
                        "overlap_bp": str(paf.overlap_bp),
                        "target_distance_bp": str(target_distance),
                        "extension_length": str(tail_length),
                        "tail_motif_copies": str(tail_motif_copies),
                        "decision": "filtered" if reasons else "eligible_for_support",
                        "rejection_reasons": ";".join(reasons),
                    }
                )
            if reasons:
                continue
            assert signal is not None
            grouped[(paf.target, target_end)].append(
                {
                    "target": paf.target,
                    "target_end": target_end,
                    "target_start": str(paf.target_start),
                    "target_stop": str(paf.target_end),
                    "junction_coordinate": str(
                        paf.target_start if target_end == "left" else paf.target_end
                    ),
                    "target_bases_trimmed": str(target_distance),
                    "read_id": paf.query,
                    "source_asset_id": signal.get("source_asset_id", ""),
                    "source_read_id": signal.get("source_read_id", paf.query),
                    "read_end": read_end,
                    "strand": paf.strand,
                    "mapq": str(paf.mapq),
                    "overlap_bp": str(paf.overlap_bp),
                    "target_distance_bp": str(target_distance),
                    "extension_length": str(tail_length),
                    "query_start": str(paf.query_start),
                    "query_end": str(paf.query_end),
                    "oriented_query_start": str(oriented_query_start),
                    "oriented_query_end": str(oriented_query_end),
                    "tail": tail,
                }
            )
    rows: list[dict[str, str]] = []
    for candidates in grouped.values():
        candidates.sort(
            key=lambda row: (int(row["mapq"]), int(row["overlap_bp"])), reverse=True
        )
        representative = candidates[0]
        candidate_id = f"{representative['target']}:{representative['target_end']}"
        supporting = [
            row
            for row in candidates
            if tails_compatible(representative["tail"], row["tail"])
        ]
        support_ids = list(dict.fromkeys(row["read_id"] for row in supporting))
        accepted = len(support_ids) >= thresholds["min_supporting_reads"]
        for index, row in enumerate(candidates):
            decision = "rejected"
            reason = "insufficient_or_conflicting_read_support"
            if accepted and row is representative:
                decision = "auto_patch" if mode == "auto_patch" else "proposed"
                reason = "approved_read_support_threshold"
            elif accepted and row in supporting:
                reason = "supporting_read"
            rows.append(
                {
                    **{key: value for key, value in row.items() if key != "tail"},
                    "candidate_id": candidate_id,
                    "supporting_reads": ",".join(support_ids),
                    "decision": decision,
                    "reason": reason,
                }
            )
    return rows


def tails_compatible(first: str, second: str) -> bool:
    if not first or not second:
        return False
    if abs(len(first) - len(second)) > max(
        100, int(max(len(first), len(second)) * 0.2)
    ):
        return False
    k = 11
    if min(len(first), len(second)) < k:
        return first == second
    first_kmers = {first[i : i + k] for i in range(len(first) - k + 1)}
    second_kmers = {second[i : i + k] for i in range(len(second) - k + 1)}
    return (
        len(first_kmers & second_kmers) / min(len(first_kmers), len(second_kmers))
        >= 0.2
    )


def iter_paf(path: Path) -> Iterator[PafRecord]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) < 12:
                raise ValueError(
                    f"malformed PAF line {line_number}: expected at least 12 "
                    "tab-delimited fields"
                )
            try:
                record = PafRecord(
                    query=fields[0],
                    query_length=int(fields[1]),
                    query_start=int(fields[2]),
                    query_end=int(fields[3]),
                    strand=fields[4],
                    target=fields[5],
                    target_length=int(fields[6]),
                    target_start=int(fields[7]),
                    target_end=int(fields[8]),
                    mapq=int(fields[11]),
                )
            except ValueError as exc:
                raise ValueError(
                    f"malformed PAF line {line_number}: invalid numeric field"
                ) from exc
            if (
                not record.query
                or not record.target
                or record.strand not in {"+", "-"}
                or record.query_length < 0
                or record.query_start < 0
                or record.query_end < record.query_start
                or record.query_end > record.query_length
                or record.target_length < 0
                or record.target_start < 0
                or record.target_end < record.target_start
                or record.target_end > record.target_length
                or not 0 <= record.mapq <= 255
            ):
                raise ValueError(
                    f"malformed PAF line {line_number}: invalid field values"
                )
            yield record
