"""Bounded POA reconstruction and full-assembly read-back evidence.

This module is snapshotted into the frozen execution script. It deliberately
uses only the sequence/PAF helpers and pyspoa, not graph or storage services.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path
from typing import Any, TypedDict

from app.stages.sequences import read_sequences, reverse_complement
from app.stages.t2t.algorithms import (
    CLUSTER_JUNCTION_DISTANCE_BP,
    CLUSTER_MAX_READS,
    CLUSTER_MIN_IDENTITY,
    CLUSTER_SAMPLE_BP,
    PafRecord,
    candidate_rows,
    has_competing_placement,
    iter_paf,
    max_motif_copies,
    molecule_id,
)


# Versioned compute budget, separate from approval timeouts/LLM thresholds.
class ReconstructionProfile(TypedDict):
    version: str
    max_reads_per_cluster: int
    max_tail_bp: int
    max_poa_cells: int
    junction_flank_bp: int


PROFILE: ReconstructionProfile = {
    "version": "terminal-poa-v1",
    "max_reads_per_cluster": 64,
    "max_tail_bp": 50000,
    "max_poa_cells": 25000000,
    "junction_flank_bp": 100,
}
RECONSTRUCTION_SCHEMA_VERSION = "t2t.reconstruction.v1"

VALIDATION_FIELDS = [
    "candidate_id",
    "target",
    "target_end",
    "method",
    "evidence_scope",
    "construction_reads",
    "readback_reads",
    "junction_spanning_reads",
    "junction_spanning_molecules",
    "competing_reads",
    "extension_length",
    "extension_covered_bp",
    "extension_coverage_fraction",
    "extension_mean_depth",
    "extension_min_depth",
    "alignment_identity",
    "telomere_motif_copies",
    "quality_status",
    "quality_reasons",
    "candidate_sha256",
]


def validate_reconstruction_outputs(
    metadata_path: Path, assembly_path: Path, validation_path: Path
) -> dict[str, Any]:
    """Reject malformed or cross-candidate output before recording success."""
    data = json.loads(metadata_path.read_text())
    if not isinstance(data, dict):
        raise ValueError("reconstruction metadata must be an object")
    if data.get("schema_version") != RECONSTRUCTION_SCHEMA_VERSION:
        raise ValueError("unsupported reconstruction schema_version")
    for key in ("candidates", "observations", "plans"):
        if not isinstance(data.get(key), list) or not all(
            isinstance(row, dict) for row in data[key]
        ):
            raise ValueError(f"invalid reconstruction {key}")
    for row in data["candidates"] + data["observations"]:
        if not all(isinstance(value, str) for value in row.values()):
            raise ValueError("reconstruction table values must be strings")
    assembly: dict[str, str] = {}
    for name, sequence in read_sequences(assembly_path):
        if name in assembly:
            raise ValueError(f"duplicate reconstructed scaffold: {name}")
        assembly[name] = sequence
    if not assembly:
        raise ValueError("empty reconstructed assembly")
    selected = {
        row.get("candidate_id"): row
        for row in data["candidates"]
        if row.get("decision") in {"auto_patch", "proposed"}
    }
    plan_ids = [plan.get("candidate_id") for plan in data["plans"]]
    if len(set(plan_ids)) != len(plan_ids) or set(plan_ids) != set(selected):
        raise ValueError("reconstruction plans differ from selected candidates")
    for plan in data["plans"]:
        try:
            sequence = assembly[plan["target"]]
            junction = plan["junction"]
            tail = plan["tail"]
            if (
                plan["end"] not in {"left", "right"}
                or not isinstance(junction, int)
                or not 0 <= junction <= len(sequence)
                or not tail
                or tail != selected[plan["candidate_id"]]["consensus_sequence"]
                or (
                    sequence[:junction]
                    if plan["end"] == "left"
                    else sequence[junction:]
                )
                != tail
            ):
                raise ValueError("reconstruction plan differs from candidate sequence")
        except (KeyError, TypeError) as exc:
            raise ValueError("malformed reconstruction plan") from exc
    with assembly_path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    with validation_path.open(newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != VALIDATION_FIELDS:
            raise ValueError("invalid read-back validation header")
        evidence = list(reader)
    if (
        len(evidence) != len(plan_ids)
        or {row["candidate_id"] for row in evidence} != set(plan_ids)
        or any(row["candidate_sha256"] != digest for row in evidence)
    ):
        raise ValueError("read-back validation differs from reconstructed candidate")
    return data


def _table(path: Path) -> Iterator[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle, delimiter="\t")


def _write_table(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def _write_fasta(path: Path, records: dict[str, str]) -> None:
    with path.open("w", encoding="ascii") as handle:
        for name, sequence in records.items():
            if not sequence:
                raise ValueError(f"empty reconstructed sequence: {name}")
            handle.write(f">{name}\n")
            for offset in range(0, len(sequence), 80):
                handle.write(sequence[offset : offset + 80] + "\n")


def aligned_query_cut(record: PafRecord, cut: int) -> int | None:
    """Project a target cut onto target-forward read coordinates, with indels."""
    query = (
        record.query_start
        if record.strand == "+"
        else record.query_length - record.query_end
    )
    if not record.target_start <= cut <= record.target_end:
        return None
    if cut == record.target_start:
        return query
    if cut == record.target_end:
        return (
            record.query_end
            if record.strand == "+"
            else record.query_length - record.query_start
        )
    if not record.cigar:
        return None  # Never interpolate through unknown CLR indels.
    target = record.target_start
    for length, operation in cigar_ops(record):
        if operation in {"M", "=", "X"}:
            if target <= cut <= target + length:
                return query + cut - target
            query += length
            target += length
        elif operation == "I":
            query += length
        elif operation in {"D", "N"}:
            if target <= cut <= target + length:
                return query
            target += length
    return None


def cigar_ops(record: PafRecord) -> list[tuple[int, str]]:
    tokens = re.findall(r"([1-9]\d*)([MIDN=X])", record.cigar)
    if "".join(n + op for n, op in tokens) != record.cigar:
        raise ValueError(f"invalid PAF CIGAR for {record.query}")
    result = [(int(n), op) for n, op in tokens]
    if record.cigar and (
        sum(n for n, op in result if op in "MI=X") != record.overlap_bp
        or sum(n for n, op in result if op in "MDN=X")
        != record.target_end - record.target_start
    ):
        raise ValueError(f"PAF CIGAR span mismatch for {record.query}")
    return result


def terminal_recruits(paf_path: Path, thresholds: dict[str, int]) -> set[str]:
    """No motif requirement: any sufficiently long terminal anchor can recruit."""
    return {
        record.query
        for record in iter_paf(paf_path)
        if min(record.target_start, record.target_length - record.target_end)
        <= thresholds["max_target_terminal_distance_bp"]
    }


def reconstruct(config: dict[str, Any]) -> None:
    import spoa  # type: ignore[import-not-found]

    work = Path(config["work_dir"])
    thresholds = config["thresholds"]
    recruits = terminal_recruits(work / "long_reads_to_assembly.paf", thresholds)
    signals = {
        row["read_id"]: row
        for row in _table(Path(config["reads_scan"]))
        if row["read_id"] in recruits
    }
    sequences = {
        name: sequence
        for path in config["reads"].values()
        for name, sequence in read_sequences(Path(path))
        if name in recruits
    }
    ends = {row["sequence"]: row for row in _table(Path(config["assembly_scan"]))}
    observations: list[dict[str, str]] = []
    rows = candidate_rows(
        work / "long_reads_to_assembly.paf",
        ends,
        signals,
        sequences,
        thresholds,
        config["motif"],
        set(config["chromosome_names"]) or None,
        config["mode"],
        observations=observations,
    )
    alignments: dict[tuple[str, str, str, str, str], PafRecord] = {}
    for record in iter_paf(work / "long_reads_to_assembly.paf"):
        if not record.cigar:
            raise ValueError(f"missing PAF CIGAR for {record.query}")
        cigar_ops(record)
        if record.query in recruits:
            key = (
                record.query,
                record.target,
                str(record.target_start),
                str(record.target_end),
                record.strand,
            )
            alignments[key] = record
    originals: dict[str, str] = {}
    for name, sequence in read_sequences(Path(config["assembly"])):
        if name in originals:
            raise ValueError(f"duplicate scaffold identifier: {name}")
        originals[name] = sequence
    plans: list[dict[str, Any]] = []
    baseline: dict[str, str] = {}
    consensus_records: dict[str, str] = {}
    for representative in rows:
        if representative["decision"] not in {"auto_patch", "proposed"}:
            continue
        cluster = [
            r for r in rows if r["candidate_id"] == representative["candidate_id"]
        ]
        # Every supporting alignment spans this common cut. The original
        # assembly is kept byte-for-byte on its retained side of the cut.
        cut = (max if representative["target_end"] == "left" else min)(
            int(r["junction_coordinate"]) for r in cluster
        )
        tails: list[str] = []
        used: list[str] = []
        seen_molecules: set[str] = set()
        for row in cluster:
            if len(tails) >= PROFILE["max_reads_per_cluster"]:
                break
            if row["molecule_id"] in seen_molecules:
                continue
            record = alignments[
                (
                    row["read_id"],
                    row["target"],
                    row["target_start"],
                    row["target_stop"],
                    row["strand"],
                )
            ]
            coordinate = aligned_query_cut(record, cut)
            if coordinate is None:
                continue
            sequence = sequences[row["read_id"]]
            oriented = (
                sequence if row["strand"] == "+" else reverse_complement(sequence)
            )
            tail = (
                oriented[:coordinate]
                if row["target_end"] == "left"
                else oriented[coordinate:]
            )
            if not tail or len(tail) > PROFILE["max_tail_bp"]:
                continue
            # Bound native POA graph size rather than silently truncating reads.
            if sum(map(len, tails)) * len(tail) > PROFILE["max_poa_cells"]:
                continue
            seen_molecules.add(row["molecule_id"])
            tails.append(tail)
            used.append(row["read_id"])
        representative["reconstruction_method"] = "poa_consensus"
        representative["construction_read_ids"] = json.dumps(used)
        representative["reconstruction_profile"] = PROFILE["version"]
        representative["evidence_scope"] = "in_sample"
        if len(tails) < thresholds["min_supporting_reads"]:
            representative["decision"] = "rejected"
            representative["reason"] = (
                "reconstruction_budget_or_common_anchor_insufficient"
            )
            continue
        # Needleman-Wunsch POA: tails have a shared assembly cut and orientation.
        consensus, _msa = spoa.poa(tails, algorithm=1, genmsa=False)
        if not consensus or not set(consensus) <= set("ACGTRYSWKMBDHVN"):
            raise ValueError("POA returned an empty or invalid DNA consensus")
        representative["consensus_sequence"] = consensus
        representative["baseline_sequence"] = tails[0]
        representative["junction_coordinate"] = str(cut)
        representative["extension_length"] = str(len(consensus))
        baseline[representative["candidate_id"]] = tails[0]
        consensus_records[representative["candidate_id"]] = consensus
        plans.append(
            {
                "candidate_id": representative["candidate_id"],
                "target": representative["target"],
                "end": representative["target_end"],
                "cut": cut,
                "tail": consensus,
                "construction_reads": used,
            }
        )
    selected = dict(originals)
    by_target: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for plan in plans:
        by_target[plan["target"]][plan["end"]] = plan
    for target, plan_by_end in by_target.items():
        source = originals[target]
        left = plan_by_end.get("left", {"cut": 0, "tail": ""})
        right = plan_by_end.get("right", {"cut": len(source), "tail": ""})
        if left["cut"] > right["cut"]:
            raise ValueError(f"conflicting reconstruction cuts: {target}")
        selected[target] = (
            left["tail"] + source[left["cut"] : right["cut"]] + right["tail"]
        )
        for plan in plan_by_end.values():
            plan["junction"] = (
                len(left["tail"])
                if plan["end"] == "left"
                else len(selected[target]) - len(right["tail"])
            )
    _write_fasta(work / "reconstructed_assembly.fa", selected)
    # Empty comparator FASTAs are allowed when nothing was reconstructed.
    _write_fasta(work / "representative_baseline.fa", baseline)
    _write_fasta(work / "consensus_extensions.fa", consensus_records)
    (work / "reconstruction.json").write_text(
        json.dumps(
            {
                "schema_version": RECONSTRUCTION_SCHEMA_VERSION,
                "profile": PROFILE,
                "clustering": {
                    "max_reads_per_end": CLUSTER_MAX_READS,
                    "junction_distance_bp": CLUSTER_JUNCTION_DISTANCE_BP,
                    "sample_bp": CLUSTER_SAMPLE_BP,
                    "min_identity": CLUSTER_MIN_IDENTITY,
                },
                "versions": {
                    "pyspoa": version("pyspoa"),
                    "minimap2": (work / "minimap2.version.txt").read_text().strip(),
                },
                "candidates": rows,
                "observations": observations,
                "plans": plans,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def readback_evidence(config: dict[str, Any]) -> None:
    """Coverage counts aligned bases only; deletions and skipped spans are holes."""
    work = Path(config["work_dir"])
    data = json.loads((work / "reconstruction.json").read_text())
    if data.get("schema_version") != RECONSTRUCTION_SCHEMA_VERSION:
        raise ValueError("unsupported reconstruction schema_version")
    plans = data["plans"]
    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for plan in plans:
        by_target[plan["target"]].append(plan)
    relevant_ids = {
        r.query
        for r in iter_paf(work / "readback.paf")
        for plan in by_target.get(r.target, [])
        if r.target_start
        < plan["junction"] + len(plan["tail"]) + PROFILE["junction_flank_bp"]
        and r.target_end
        > plan["junction"] - len(plan["tail"]) - PROFILE["junction_flank_bp"]
    }
    placements: dict[str, list[PafRecord]] = defaultdict(list)
    for record in iter_paf(work / "readback.paf"):
        if not record.cigar:
            raise ValueError(f"missing read-back PAF CIGAR for {record.query}")
        cigar_ops(record)
        if record.query in relevant_ids:
            placements[record.query].append(record)
    signals = {
        r["read_id"]: r
        for r in _table(Path(config["reads_scan"]))
        if r["read_id"] in relevant_ids
    }
    candidate = work / "reconstructed_assembly.fa"
    with candidate.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    evidence: list[dict[str, str]] = []
    for plan in plans:
        length = len(plan["tail"])
        junction = plan["junction"]
        start = 0 if plan["end"] == "left" else junction
        stop = junction if plan["end"] == "left" else junction + length
        # Difference arrays + per-read merged blocks avoid double-counting split
        # alignments and distinguish covered sequence from alignment bounding boxes.
        depth_delta: dict[int, int] = defaultdict(int)
        spanning: set[str] = set()
        molecules: set[str] = set()
        competing: set[str] = set()
        aligned: set[str] = set()
        matches = blocks = 0
        for query, records in placements.items():
            intervals: list[tuple[int, int]] = []
            for record in records:
                if record.target != plan["target"]:
                    continue
                if record.target_end <= start or record.target_start >= stop:
                    continue
                if has_competing_placement(record, records):
                    competing.add(query)
                    continue
                if record.mapq < config["thresholds"]["min_mapq"] or record.mapq == 255:
                    continue
                aligned.add(query)
                matches += record.matches
                blocks += record.block_length
                position = record.target_start
                covered: list[tuple[int, int]] = []
                for size, op in cigar_ops(record):
                    if op in {"M", "=", "X"}:
                        covered.append((position, position + size))
                        position += size
                    elif op in {"D", "N"}:
                        position += size
                # A CIGAR-less PAF is inadequate for junction/base coverage.
                flank = PROFILE["junction_flank_bp"]
                # CLR indels may split M blocks at the junction. Require aligned
                # bases on both flanks within one spanning alignment, not one M.
                left_support = sum(
                    max(0, min(b, junction) - max(a, junction - flank))
                    for a, b in covered
                )
                right_support = sum(
                    max(0, min(b, junction + flank) - max(a, junction))
                    for a, b in covered
                )
                if left_support >= 0.8 * flank and right_support >= 0.8 * flank:
                    spanning.add(query)
                    molecules.add(molecule_id(query, signals.get(query, {})))
                intervals.extend(
                    (max(start, a), min(stop, b))
                    for a, b in covered
                    if max(start, a) < min(stop, b)
                )
            merged: list[tuple[int, int]] = []
            for a, b in sorted(intervals):
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            for a, b in merged:
                depth_delta[a] += 1
                depth_delta[b] -= 1
        covered_bp = total_depth = depth = 0
        minimum = len(aligned)
        previous = start
        for position in sorted({start, stop, *depth_delta}):
            span = position - previous
            if span:
                covered_bp += span if depth else 0
                total_depth += span * depth
                minimum = min(minimum, depth)
            depth += depth_delta[position]
            previous = position
        reasons = []
        if len(molecules) < config["thresholds"]["min_supporting_reads"]:
            reasons.append("insufficient_junction_spanning_molecules")
        if covered_bp < length:
            reasons.append("uncovered_extension_bases")
        if competing:
            reasons.append("competing_readback_placements")
        if len(plan["construction_reads"]) == 1:
            reasons.append("single_read_consensus")
        evidence.append(
            {
                "candidate_id": plan["candidate_id"],
                "target": plan["target"],
                "target_end": plan["end"],
                "method": "poa_consensus",
                "evidence_scope": "in_sample",
                "construction_reads": str(len(plan["construction_reads"])),
                "readback_reads": str(len(aligned)),
                "junction_spanning_reads": str(len(spanning)),
                "junction_spanning_molecules": str(len(molecules)),
                "competing_reads": str(len(competing)),
                "extension_length": str(length),
                "extension_covered_bp": str(covered_bp),
                "extension_coverage_fraction": f"{covered_bp / length:.6f}",
                "extension_mean_depth": f"{total_depth / length:.6f}",
                "extension_min_depth": str(minimum),
                "alignment_identity": f"{matches / blocks:.6f}" if blocks else "",
                "telomere_motif_copies": str(
                    max_motif_copies(plan["tail"], config["motif"])
                ),
                "quality_status": "evidence_insufficient"
                if reasons
                else "supported_in_sample",
                "quality_reasons": ";".join(reasons),
                "candidate_sha256": digest,
            }
        )
    _write_table(work / "readback_validation.tsv", evidence, VALIDATION_FIELDS)
