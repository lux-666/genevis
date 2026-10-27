"""Pure telomere support and PAF algorithms, independent of graph and storage."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from app.stages.sequences import reverse_complement

CLUSTER_MAX_READS = 256
CLUSTER_JUNCTION_DISTANCE_BP = 50
CLUSTER_SAMPLE_BP = 1000
CLUSTER_MIN_IDENTITY = 0.65


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
    matches: int = 0
    block_length: int = 0
    cigar: str = ""

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
    # Only terminal recruits are kept in memory; competing placements still come
    # from the full assembly, including internal/repetitive locations.
    placements: dict[str, list[PafRecord]] = defaultdict(list)
    for paf in iter_paf(paf_path):
        if paf.query in read_sequences:
            placements[paf.query].append(paf)
    for paf in iter_paf(paf_path):
        signal = read_signals.get(paf.query)
        sequence = read_sequences.get(paf.query)
        target = assembly_ends.get(paf.target)
        if sequence is not None and len(sequence) != paf.query_length:
            raise ValueError(f"PAF query length differs from input: {paf.query}")
        if target and "length" in target and int(target["length"]) != paf.target_length:
            raise ValueError(f"PAF target length differs from input: {paf.target}")
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
                        (target or {}).get(f"{target_end}_has_signal") == "true",
                        "target_end_already_has_telomere",
                    ),
                    (paf.mapq < thresholds["min_mapq"], "mapq_below_threshold"),
                    (paf.mapq == 255, "mapq_unavailable"),
                    (
                        has_competing_placement(paf, placements[paf.query]),
                        "competing_placement",
                    ),
                    (
                        has_split_placement(
                            paf, placements[paf.query], thresholds["min_overlap_bp"]
                        ),
                        "split_or_chimeric_placement",
                    ),
                    (
                        paf.overlap_bp < thresholds["min_overlap_bp"],
                        "overlap_below_threshold",
                    ),
                    (
                        target_distance > thresholds["max_target_terminal_distance_bp"],
                        "target_distance_above_threshold",
                    ),
                    (tail_length <= 0, "no_unaligned_tail"),
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
                    "tail_motif_copies": str(tail_motif_copies),
                    "platform": signal.get("platform", "unknown"),
                    "molecule_id": molecule_id(paf.query, signal),
                    "cigar": paf.cigar,
                }
            )
    rows: list[dict[str, str]] = []
    for candidates in grouped.values():
        candidates.sort(
            key=lambda row: (-int(row["mapq"]), -int(row["overlap_bp"]), row["read_id"])
        )
        # Complete-link clusters avoid a repeat-derived bridge merging distinct
        # branches. Multiple PAF records of the same read never add support.
        clusters: list[list[dict[str, str]]] = []
        seen: set[str] = set()
        overflow: list[dict[str, str]] = []
        for row in candidates:
            if row["read_id"] in seen:
                continue
            seen.add(row["read_id"])
            if len(seen) > CLUSTER_MAX_READS:
                overflow.append(row)
                continue
            for cluster in clusters:
                if all(
                    abs(
                        int(row["junction_coordinate"])
                        - int(other["junction_coordinate"])
                    )
                    <= CLUSTER_JUNCTION_DISTANCE_BP
                    and tails_compatible(row["tail"], other["tail"])
                    for other in cluster
                ):
                    cluster.append(row)
                    break
            else:
                clusters.append([row])
        clusters.sort(
            key=lambda group: (
                -len({r["molecule_id"] for r in group}),
                -len(group),
                -sum(int(r["overlap_bp"]) for r in group),
                group[0]["read_id"],
            )
        )
        for cluster_index, supporting in enumerate(clusters):
            representative = supporting[0]
            base_id = f"{representative['target']}:{representative['target_end']}"
            candidate_id = (
                base_id if cluster_index == 0 else f"{base_id}:alt{cluster_index}"
            )
            support_ids = [r["read_id"] for r in supporting]
            molecule_count = len({r["molecule_id"] for r in supporting})
            accepted = molecule_count >= thresholds["min_supporting_reads"]
            for row in supporting:
                decision = "rejected"
                reason = "insufficient_or_conflicting_read_support"
                if accepted and row is representative and cluster_index == 0:
                    decision = "auto_patch" if mode == "auto_patch" else "proposed"
                    reason = "approved_read_support_threshold"
                elif accepted:
                    reason = (
                        "alternative_cluster" if cluster_index else "supporting_read"
                    )
                rows.append(
                    {
                        **{key: value for key, value in row.items() if key != "tail"},
                        "candidate_id": candidate_id,
                        "supporting_reads": ",".join(support_ids),
                        "supporting_read_ids": json.dumps(support_ids),
                        "supporting_molecules": str(molecule_count),
                        "cluster_rank": str(cluster_index + 1),
                        "decision": decision,
                        "reason": reason,
                    }
                )
        for row in overflow:
            rows.append(
                {
                    **{key: value for key, value in row.items() if key != "tail"},
                    "candidate_id": f"{row['target']}:{row['target_end']}:budget",
                    "supporting_reads": "",
                    "supporting_read_ids": "[]",
                    "supporting_molecules": "0",
                    "cluster_rank": "",
                    "decision": "rejected",
                    "reason": "clustering_read_budget_exceeded",
                }
            )
    return rows


def molecule_id(read_id: str, signal: dict[str, str]) -> str:
    """Count PacBio subreads from one ZMW once; other inputs remain read IDs."""
    source_id = signal.get("source_read_id", read_id)
    if signal.get("platform") in {"clr", "pacbio_hifi"}:
        match = re.fullmatch(r"([^/]+)/(\d+)/(?:\d+_\d+|ccs)", source_id)
        if match:
            return f"{signal.get('source_asset_id', '')}::{match[1]}/{match[2]}"
    return read_id


def has_competing_placement(record: PafRecord, placements: list[PafRecord]) -> bool:
    for other in placements:
        if other is record:
            continue
        shared = max(
            0,
            min(record.query_end, other.query_end)
            - max(record.query_start, other.query_start),
        )
        distinct = (
            record.target != other.target
            or record.strand != other.strand
            or abs(record.target_start - other.target_start) > 100
        )
        score = record.matches or record.overlap_bp
        other_score = other.matches or other.overlap_bp
        if (
            distinct
            and shared >= 0.5 * record.overlap_bp
            and other_score >= 0.9 * score
        ):
            return True
    return False


def has_split_placement(
    record: PafRecord, placements: list[PafRecord], minimum: int
) -> bool:
    return any(
        other is not record
        and other.overlap_bp >= minimum
        and other.mapq >= 20
        and other.mapq != 255
        and (other.target != record.target or other.strand != record.strand)
        and max(
            0,
            min(record.query_end, other.query_end)
            - max(record.query_start, other.query_start),
        )
        < 0.2 * min(record.overlap_bp, other.overlap_bp)
        for other in placements
    )


def tails_compatible(first: str, second: str) -> bool:
    if not first or not second:
        return False
    if abs(len(first) - len(second)) > max(
        100, int(max(len(first), len(second)) * 0.2)
    ):
        return False
    if min(len(first), len(second)) < 11:
        return first == second
    # Bounded pairwise POA preserves order and handles CLR indels. K-mer set
    # overlap saturates on long tails and would merge unrelated repeat branches.
    import spoa  # type: ignore[import-not-found]

    for first_sample, second_sample in (
        (first[:CLUSTER_SAMPLE_BP], second[:CLUSTER_SAMPLE_BP]),
        (first[-CLUSTER_SAMPLE_BP:], second[-CLUSTER_SAMPLE_BP:]),
    ):
        _consensus, msa = spoa.poa(
            [first_sample, second_sample], algorithm=1, genmsa=True
        )
        matches = sum(a == b and a in "ACGT" for a, b in zip(msa[0], msa[1]))
        if matches / max(len(msa[0]), 1) < CLUSTER_MIN_IDENTITY:
            return False
    return True


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
                    matches=int(fields[9]),
                    block_length=int(fields[10]),
                    cigar=next(
                        (f[5:] for f in fields[12:] if f.startswith("cg:Z:")), ""
                    ),
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
                or record.matches < 0
                or record.block_length < record.matches
            ):
                raise ValueError(
                    f"malformed PAF line {line_number}: invalid field values"
                )
            yield record
