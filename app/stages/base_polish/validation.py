"""Paired, source-bound polishing evidence; deliberately not an independent QV."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.schemas.artifacts import ArtifactRef
from app.stages.sequences import fasta_records, read_sequences, sequence_summary
from app.storage.artifacts import resolve_artifact_ref
from app.storage.assembly_candidates import load_executed_candidate_bundle


def load_comparison(
    data_dir: str | Path,
    session_id: str,
    value: Any,
    *,
    candidate_refs: list[ArtifactRef],
) -> dict[str, Any]:
    """Verify the immutable pair, not merely two independently valid candidates."""
    ref = ArtifactRef.model_validate(value)
    if ref.artifact_type != "base_polish.comparison":
        raise ValueError("incorrect polishing comparison type")
    report = json.loads(resolve_artifact_ref(data_dir, session_id, ref).read_text())
    source = load_executed_candidate_bundle(
        data_dir, session_id, report["source_candidate_ref"]
    )
    polished = load_executed_candidate_bundle(
        data_dir,
        session_id,
        report["polished_candidate_ref"],
        expected_method_name="base_polish.racon",
    )
    if set(candidate_refs) != {source.ref, polished.ref} or len(candidate_refs) != 2:
        raise ValueError("polishing comparison candidate pair mismatch")
    if ArtifactRef.model_validate(report["output_ref"]) != polished.primary_output_ref:
        raise ValueError("polishing comparison output mismatch")
    inputs = ArtifactRef.model_validate(report["input_record_ref"])
    record = json.loads(resolve_artifact_ref(data_dir, session_id, inputs).read_text())
    if (
        ArtifactRef.model_validate(record["source_candidate_ref"]) != source.ref
        or ArtifactRef.model_validate(record["assembly_ref"])
        != source.primary_output_ref
    ):
        raise ValueError("polishing comparison source mismatch")
    resolve_artifact_ref(
        data_dir, session_id, ArtifactRef.model_validate(record["normalized_reads_ref"])
    )
    for raw in report["evidence_refs"]:
        resolve_artifact_ref(data_dir, session_id, ArtifactRef.model_validate(raw))
    return dict(report)


def _covered(intervals: list[tuple[int, int]]) -> int:
    total = end = 0
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total


def alignment_summary(
    path: Path, targets: dict[str, str], reads: dict[str, int]
) -> dict[str, Any]:
    query_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    target_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    count = matches = blocks = 0
    with path.open(encoding="ascii") as handle:
        for line in handle:
            fields = line.rstrip().split("\t")
            if len(fields) < 12:
                raise ValueError("malformed polishing PAF")
            query, target = fields[0], fields[5]
            qlen, qs, qe = map(int, fields[1:4])
            tlen, ts, te, matching, block, mapq = map(int, fields[6:12])
            if (
                reads.get(query) != qlen
                or target not in targets
                or tlen != len(targets[target])
                or fields[4] not in {"+", "-"}
                or not 0 <= qs < qe <= qlen
                or not 0 <= ts < te <= tlen
                or not 0 <= matching <= block
                or block <= 0
                or not 0 <= mapq <= 255
            ):
                raise ValueError(
                    "PAF identities, lengths or coordinates differ from inputs"
                )
            tags = fields[12:]
            if "tp:A:S" in tags:
                raise ValueError(
                    "secondary alignment violates paired comparison settings"
                )
            cigar = [item[5:] for item in tags if item.startswith("cg:Z:")]
            if len(cigar) != 1 or not re.fullmatch(r"(?:[1-9][0-9]*[=XID])+", cigar[0]):
                raise ValueError("comparison requires minimap2 base-level eqx CIGAR")
            ops = [(int(n), op) for n, op in re.findall(r"(\d+)([=XID])", cigar[0])]
            if (
                sum(n for n, op in ops if op != "D") != qe - qs
                or sum(n for n, op in ops if op != "I") != te - ts
            ):
                raise ValueError("PAF CIGAR span mismatch")
            ambiguous_tags = [item[5:] for item in tags if item.startswith("nn:i:")]
            if len(ambiguous_tags) > 1:
                raise ValueError("duplicate PAF ambiguous-base count")
            ambiguous = int(ambiguous_tags[0]) if ambiguous_tags else 0
            equal_bases = sum(n for n, op in ops if op == "=")
            # Minimap2 excludes ambiguous bases from mlen/blen, but its eqx
            # CIGAR can include them in '=' (including N aligned to N).
            if (
                ambiguous < 0
                or sum(n for n, _ in ops) != block + ambiguous
                or not 0 <= equal_bases - matching <= ambiguous
            ):
                raise ValueError("PAF match/block counts differ from CIGAR")
            query_intervals[query].append((qs, qe))
            target_intervals[target].append((ts, te))
            count += 1
            matches += matching
            blocks += block
    if not count:
        raise ValueError("empty read-alignment evidence; polishing cannot be evaluated")
    mapped = sum(_covered(intervals) for intervals in query_intervals.values())
    covered = sum(_covered(intervals) for intervals in target_intervals.values())
    return {
        "alignment_records": count,
        "mapped_reads": len(query_intervals),
        "total_reads": len(reads),
        "mapped_read_bases": mapped,
        "total_read_bases": sum(reads.values()),
        "matching_alignment_bases": matches,
        "alignment_block_bases": blocks,
        "base_identity": matches / blocks,
        "covered_target_bases": covered,
        "target_coverage_fraction": covered / sum(map(len, targets.values())),
    }


def compare_outputs(
    source: Path, candidate: Path, reads: Path, before_paf: Path, after_paf: Path
) -> dict[str, Any]:
    before, after = fasta_records(source), fasta_records(candidate)
    if set(before) != set(after):
        raise ValueError("polishing must preserve every scaffold identity")
    lengths = {name: len(seq) for name, seq in read_sequences(reads, "fasta")}
    before_metrics = alignment_summary(before_paf, before, lengths)
    after_metrics = alignment_summary(after_paf, after, lengths)
    return {
        "schema_version": 1,
        "before": {"sequence": sequence_summary(before), "alignments": before_metrics},
        "after": {"sequence": sequence_summary(after), "alignments": after_metrics},
        "delta": {
            key: after_metrics[key] - before_metrics[key]
            for key in (
                "mapped_reads",
                "mapped_read_bases",
                "base_identity",
                "target_coverage_fraction",
            )
        },
        "changed_scaffolds": sum(before[name] != after[name] for name in before),
        "independent_qv": None,
        "limitations": [
            "Same reads used for correction and comparison; "
            "not independent accuracy/QV.",
            "Alignment identity is weighted over records; "
            "covered bases use interval unions.",
            "No automatic improvement verdict. Compare QUAST/BUSCO in final Review.",
        ],
    }
