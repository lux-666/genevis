"""Pure rendering of Assembly Review evidence."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _render_assembly_review_markdown(payload: dict[str, Any], *, title: str) -> str:
    lines = [
        f"# {title}",
        "",
        (
            "No automatic biological thresholds are applied. This report informs "
            "the human decision only."
        ),
        "",
        "## Candidate Metrics",
        "",
        (
            "| Candidate | Method | Source | Assembly ref | QUAST contigs | "
            "Total length | N50 | Largest contig | BUSCO C% | BUSCO S% | "
            "BUSCO D% | BUSCO F% | BUSCO M% | Available signals | "
            "Missing/failed/pending signals |"
        ),
        (
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | "
            "--- | --- | --- | --- | --- |"
        ),
    ]
    for candidate in payload.get("candidates", []):
        metrics = candidate.get("metrics") or {}
        quast = metrics.get("quast") or {}
        busco = metrics.get("busco") or {}
        signal_buckets = candidate.get("signals") or {}
        unavailable = []
        for status in ("missing", "failed", "pending"):
            for signal_name in signal_buckets.get(status, []):
                unavailable.append(f"{signal_name} ({status})")
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in [
                    candidate.get("candidate_id"),
                    candidate.get("method_name"),
                    candidate.get("source"),
                    candidate.get("final_assembly_ref"),
                    quast.get("contig_count"),
                    quast.get("total_length"),
                    quast.get("n50"),
                    quast.get("largest_contig"),
                    busco.get("complete_percent"),
                    busco.get("single_copy_percent"),
                    busco.get("duplicated_percent"),
                    busco.get("fragmented_percent"),
                    busco.get("missing_percent"),
                    ", ".join(signal_buckets.get("available", [])),
                    ", ".join(unavailable),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Parameter Provenance",
            "",
            (
                "| Candidate | Read profile | Estimated genome size | Threads | "
                "Rerun index |"
            ),
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for candidate in payload.get("candidates", []):
        params = candidate.get("parameters") or {}
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in [
                    candidate.get("candidate_id"),
                    params.get("read_profile"),
                    params.get("estimated_genome_size"),
                    params.get("threads"),
                    params.get("rerun_index"),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Reference-dependent QUAST metrics", ""])
    lines.append("Missing values mean unavailable, not zero errors or QV.")
    for candidate in payload.get("candidates", []):
        quast = (candidate.get("metrics") or {}).get("quast") or {}
        lines.append(
            f"- {candidate.get('candidate_id')}: "
            + json.dumps(
                {
                    key: value
                    for key, value in quast.items()
                    if key
                    not in {"contig_count", "total_length", "n50", "largest_contig"}
                },
                sort_keys=True,
            )
        )
    polish = payload.get("polishing_comparison")
    if polish:
        lines.extend(
            [
                "",
                "## Paired polishing evidence",
                "",
                "Before = source; after = Racon. Same reads were used for correction "
                "and comparison; these values are not independent QV.",
                "",
                "```json",
                json.dumps(polish, indent=2, sort_keys=True),
                "```",
                "Retaining the source is a valid final decision; "
                "no benefit is assumed.",
            ]
        )

    _append_related_species_sanity_check(
        lines, payload.get("related_species_sanity_check") or {}
    )

    lines.extend(["", "## Evidence Refs", ""])
    raw_refs = payload.get("raw_refs") or {}
    for key, value in raw_refs.items():
        if value:
            lines.append(f"- {key}: `{value}`")
    for candidate in payload.get("candidates", []):
        metric_refs = candidate.get("metric_refs_by_signal") or {}
        for signal_name, refs in metric_refs.items():
            for ref in refs:
                lines.append(
                    f"- {candidate.get('candidate_id')} {signal_name}: `{ref}`"
                )

    lines.extend(["", "## Missing Evidence", ""])
    for candidate in payload.get("candidates", []):
        signal_buckets = candidate.get("signals") or {}
        notes_by_signal = candidate.get("signal_notes_by_signal") or {}
        for status in ("missing", "failed", "pending"):
            for signal_name in signal_buckets.get(status, []):
                notes = ", ".join(notes_by_signal.get(signal_name, []))
                suffix = f" — {notes}" if notes else ""
                lines.append(
                    f"- {candidate.get('candidate_id')} {signal_name}: {status}{suffix}"
                )

    lines.extend(["", "## Report Notes", ""])
    for note in payload.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _append_related_species_sanity_check(
    lines: list[str], pack: dict[str, Any]
) -> None:
    lines.extend(["", "## Related-species sanity check", ""])
    if not pack:
        lines.append("- Reference evidence was not generated.")
        return
    target = pack.get("target") or {}
    target_name = target.get("canonical_scientific_name") or target.get("input_name")
    lines.extend(
        [
            f"- Target: {_markdown_cell(target_name)}",
            f"- Matching mode: {_markdown_cell(pack.get('matching_mode'))}",
            (
                "- This evidence is comparative only; it is not an assembly prior "
                "or automatic-failure threshold."
            ),
            "",
        ]
    )
    records = pack.get("selected_records") or []
    if records:
        lines.extend(
            [
                (
                    "| Organism | Accession | Assembly | Total length | "
                    "Chromosomes | Contig N50 | Scaffold N50 | GC | "
                    "BUSCO lineage |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for record in records:
            busco = record.get("busco") or {}
            lines.append(
                "| "
                + " | ".join(
                    _markdown_cell(value)
                    for value in [
                        record.get("organism_name"),
                        record.get("accession"),
                        record.get("assembly_name"),
                        record.get("total_sequence_length_bp"),
                        record.get("chromosome_count"),
                        record.get("contig_n50_bp"),
                        record.get("scaffold_n50_bp"),
                        record.get("gc_percent"),
                        busco.get("lineage"),
                    ]
                )
                + " |"
            )
    else:
        lines.append("- No useful related-species primary assemblies were found.")
    excluded = pack.get("excluded_alternate_haplotype_records") or []
    if excluded:
        excluded_accessions = ", ".join(
            str(record.get("accession")) for record in excluded
        )
        lines.append(f"- Excluded alternate-haplotype records: {excluded_accessions}")
    summary = pack.get("computed_summary") or {}
    if summary:
        lines.extend(
            [
                "",
                "Computed summary:",
                "",
                "```json",
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                "```",
            ]
        )
    for note in pack.get("notes") or []:
        lines.append(f"- Reference note: {note}")


def _markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")
