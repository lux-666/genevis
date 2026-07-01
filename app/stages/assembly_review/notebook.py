"""Render comparative NCBI related-species statistics into PNG charts and a review notebook.

This module never produces an automatic pass/fail signal. All output is comparative
context intended to help a human reviewer decide whether to accept, rerun, or transform
a draft assembly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import nbformat  # noqa: E402
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook  # noqa: E402

DISCLAIMER = "Comparative reference only — not an automatic threshold or assembly prior."
NOTEBOOK_DISCLAIMER_1 = (
    "This notebook is comparative only. It informs the human reviewer's decision; "
    "it is not an automatic threshold and not an assembly prior."
)
NOTEBOOK_DISCLAIMER_2 = (
    "`chromosome_count` values come only from NCBI assembly metadata and are not "
    "equivalent to a verified karyotype."
)
KARYOTYPE_CAPTION = "(NCBI assembly metadata, not verified karyotype)"


def _n50_values(records: list[dict[str, Any]]) -> list[float]:
    values = []
    for record in records:
        n50 = record.get("contig_n50_bp")
        if n50 is None:
            n50 = record.get("scaffold_n50_bp")
        if n50 is not None:
            values.append(n50)
    return values


def _field_values(records: list[dict[str, Any]], key: str) -> list[float]:
    return [record[key] for record in records if record.get(key) is not None]


def _add_disclaimer(fig) -> None:
    fig.text(0.5, 0.01, DISCLAIMER, ha="center", va="bottom", fontsize=7, style="italic")


def render_statistics_figures(statistics_pack: dict, out_dir: Path) -> dict[str, str]:
    """Render the 7 comparative PNG charts for a related-species statistics pack.

    Writes nothing and returns {} when the pack is unavailable or has no analysis
    cohort records. Never raises for missing/absent underlying data fields; skips
    individual figures whose data is entirely empty instead.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if statistics_pack.get("status") != "available":
        return {}

    analysis_records = statistics_pack.get("analysis_cohort", {}).get("records", [])
    if not analysis_records:
        return {}

    target_position = statistics_pack.get("target_position", {}) or {}
    computed_summary = statistics_pack.get("computed_summary", {}) or {}
    figures: dict[str, str] = {}

    def _save(name: str, fig) -> None:
        _add_disclaimer(fig)
        path = out_dir / f"{name}.png"
        fig.savefig(path)
        plt.close(fig)
        figures[name] = str(path.resolve())

    # 1. n50_distribution
    n50_values = _n50_values(analysis_records)
    if n50_values:
        fig, ax = plt.subplots()
        ax.hist(n50_values, bins=min(20, max(1, len(n50_values))))
        ax.set_xlabel("Contig N50 (bp)")
        ax.set_ylabel("Count")
        ax.set_title("N50 distribution (related species cohort)")
        percentile = target_position.get("n50_percentile")
        if percentile is not None:
            xmin, xmax = ax.get_xlim()
            target_x = xmin + (xmax - xmin) * (percentile / 100.0)
            ax.axvline(x=target_x, color="red", linestyle="--", label=f"target ({percentile:.1f}th pct)")
            ax.legend()
        _save("n50_distribution", fig)

    # 2. assembly_size_distribution
    size_values = _field_values(analysis_records, "total_sequence_length_bp")
    if size_values:
        fig, ax = plt.subplots()
        ax.hist(size_values, bins=min(20, max(1, len(size_values))))
        ax.set_xlabel("Total sequence length (bp)")
        ax.set_ylabel("Count")
        ax.set_title("Assembly size distribution (related species cohort)")
        percentile = target_position.get("assembly_size_percentile")
        if percentile is not None:
            xmin, xmax = ax.get_xlim()
            target_x = xmin + (xmax - xmin) * (percentile / 100.0)
            ax.axvline(x=target_x, color="red", linestyle="--", label=f"target ({percentile:.1f}th pct)")
            ax.legend()
        _save("assembly_size_distribution", fig)

    # 3. gc_distribution
    gc_values = _field_values(analysis_records, "gc_percent")
    if gc_values:
        fig, ax = plt.subplots()
        ax.hist(gc_values, bins=min(20, max(1, len(gc_values))))
        ax.set_xlabel("GC percent")
        ax.set_ylabel("Count")
        ax.set_title("GC% distribution (related species cohort)")
        percentile = target_position.get("gc_percentile")
        if percentile is not None:
            xmin, xmax = ax.get_xlim()
            target_x = xmin + (xmax - xmin) * (percentile / 100.0)
            ax.axvline(x=target_x, color="red", linestyle="--", label=f"target ({percentile:.1f}th pct)")
            ax.legend()
        _save("gc_distribution", fig)

    # 4. busco_comparison
    busco_complete = []
    busco_duplicated = []
    for record in analysis_records:
        busco = record.get("busco")
        if not busco:
            continue
        if busco.get("complete") is not None:
            busco_complete.append(busco["complete"])
        if busco.get("duplicated") is not None:
            busco_duplicated.append(busco["duplicated"])
    if busco_complete or busco_duplicated:
        fig, ax = plt.subplots()
        data = [values for values in (busco_complete, busco_duplicated) if values]
        labels = [label for label, values in (("complete", busco_complete), ("duplicated", busco_duplicated)) if values]
        ax.boxplot(data, tick_labels=labels)
        ax.set_ylabel("BUSCO percent")
        ax.set_title("BUSCO complete vs duplicated (related species cohort)")
        complete_pct = target_position.get("busco_complete_percentile")
        duplicated_pct = target_position.get("busco_duplicated_percentile")
        marker_notes = []
        if complete_pct is not None:
            marker_notes.append(f"complete target pct={complete_pct:.1f}")
        if duplicated_pct is not None:
            marker_notes.append(f"duplicated target pct={duplicated_pct:.1f}")
        if marker_notes:
            ax.annotate(
                "; ".join(marker_notes),
                xy=(0.5, 1.02),
                xycoords="axes fraction",
                ha="center",
                fontsize=8,
            )
        _save("busco_comparison", fig)

    # 5. chromosome_count_distribution
    chromosome_counts = _field_values(analysis_records, "chromosome_count")
    if chromosome_counts:
        fig, ax = plt.subplots()
        counts_by_value: dict[float, int] = {}
        for value in chromosome_counts:
            counts_by_value[value] = counts_by_value.get(value, 0) + 1
        values_sorted = sorted(counts_by_value)
        frequencies = [counts_by_value[value] for value in values_sorted]
        bar_colors = ["tab:blue"] * len(values_sorted)
        mode = computed_summary.get("chromosome_count_mode")
        if mode is not None and mode in values_sorted:
            bar_colors[values_sorted.index(mode)] = "tab:orange"
        ax.bar([str(v) for v in values_sorted], frequencies, color=bar_colors)
        ax.set_xlabel("Chromosome count")
        ax.set_ylabel("Number of assemblies")
        ax.set_title(f"Chromosome count distribution {KARYOTYPE_CAPTION}")
        _save("chromosome_count_distribution", fig)

    # 6. n50_vs_assembly_size
    scatter_x = []
    scatter_y = []
    for record in analysis_records:
        size = record.get("total_sequence_length_bp")
        n50 = record.get("contig_n50_bp")
        if n50 is None:
            n50 = record.get("scaffold_n50_bp")
        if size is not None and n50 is not None:
            scatter_x.append(size)
            scatter_y.append(n50)
    if scatter_x:
        fig, ax = plt.subplots()
        ax.scatter(scatter_x, scatter_y)
        ax.set_xlabel("Total sequence length (bp)")
        ax.set_ylabel("Contig N50 (bp)")
        ax.set_title("N50 vs assembly size (related species cohort)")
        _save("n50_vs_assembly_size", fig)

    # 7. cohort_composition
    by_tier = computed_summary.get("cohort_composition", {}).get("by_tier", {})
    tier_items = [(tier, count) for tier, count in by_tier.items() if count]
    if tier_items:
        fig, ax = plt.subplots()
        ax.bar([tier for tier, _ in tier_items], [count for _, count in tier_items])
        ax.set_xlabel("Tier")
        ax.set_ylabel("Count")
        ax.set_title("Cohort composition by tier")
        _save("cohort_composition", fig)

    return figures


def _target_metrics_markdown(target_metrics: dict[str, float] | None) -> str:
    if not target_metrics:
        return "Target assembly metrics were not available at report-generation time."
    labels = {
        "n50_bp": "N50 (bp)",
        "assembly_size_bp": "Assembly size (bp)",
        "gc_percent": "GC percent",
        "busco_complete_percent": "BUSCO complete percent",
        "busco_duplicated_percent": "BUSCO duplicated percent",
    }
    lines = []
    for key, label in labels.items():
        if key in target_metrics and target_metrics[key] is not None:
            lines.append(f"- **{label}**: {target_metrics[key]}")
    if not lines:
        return "Target assembly metrics were not available at report-generation time."
    return "\n".join(lines)


def _source_provenance_markdown(source: dict[str, Any]) -> str:
    lines = [
        f"- **dataset_id**: {source.get('dataset_id')}",
        f"- **source_provider**: {source.get('source_provider')}",
        f"- **source_version**: {source.get('source_version')}",
        f"- **exported_at**: {source.get('exported_at')}",
        f"- **verification**: {source.get('verification')}",
    ]
    if source.get("verification") == "mismatch":
        lines.append("- **WARNING**: the local NCBI snapshot failed verification (file hash mismatch); treat this comparison with caution.")
    return "\n".join(lines)


def _cohort_composition_markdown(statistics_pack: dict, figures_relpaths: dict[str, str]) -> str:
    tier_counts = statistics_pack.get("tier_counts", {}) or {}
    computed_summary = statistics_pack.get("computed_summary", {}) or {}
    cohort_composition = computed_summary.get("cohort_composition", {}) or {}
    display_cohort = statistics_pack.get("display_cohort", {}) or {}
    analysis_cohort = statistics_pack.get("analysis_cohort", {}) or {}

    lines = [
        "**Tier counts:**",
        f"- exact: {tier_counts.get('exact', 0)}, same_genus: {tier_counts.get('same_genus', 0)}, "
        f"same_family: {tier_counts.get('same_family', 0)}, same_order: {tier_counts.get('same_order', 0)}",
        "",
        f"**Record count (computed summary)**: {computed_summary.get('record_count', 0)}",
        "",
        f"**Display cohort**: {len(display_cohort.get('records', []))} / limit {display_cohort.get('size_limit')}",
        f"**Analysis cohort**: {len(analysis_cohort.get('records', []))} / limit {analysis_cohort.get('size_limit')}",
        "",
        "**Cohort composition:**",
        f"- by_tier: {cohort_composition.get('by_tier', {})}",
        f"- by_accession_type: {cohort_composition.get('by_accession_type', {})}",
        f"- by_assembly_level: {cohort_composition.get('by_assembly_level', {})}",
    ]
    if "cohort_composition" in figures_relpaths:
        lines.append("")
        lines.append(f"![Cohort composition]({figures_relpaths['cohort_composition']})")
    return "\n".join(lines)


_DISTRIBUTION_FIGURE_TITLES = [
    ("n50_distribution", "N50 distribution"),
    ("assembly_size_distribution", "Assembly size distribution"),
    ("gc_distribution", "GC% distribution"),
    ("busco_comparison", "BUSCO complete vs duplicated"),
    ("chromosome_count_distribution", "Chromosome count distribution"),
    ("n50_vs_assembly_size", "N50 vs assembly size"),
]


def _distribution_figures_markdown(figures_relpaths: dict[str, str]) -> str:
    lines = []
    for key, title in _DISTRIBUTION_FIGURE_TITLES:
        if key not in figures_relpaths:
            continue
        lines.append(f"![{title}]({figures_relpaths[key]})")
        if key == "chromosome_count_distribution":
            lines.append(f"*{KARYOTYPE_CAPTION}*")
        lines.append("")
    if not lines:
        return "No distribution figures were available to render."
    return "\n".join(lines)


def _percentile_summary_markdown(target_position: dict[str, Any]) -> str:
    if not target_position.get("available"):
        lines = ["Target percentile position was not available."]
    else:
        fields = [
            ("n50_percentile", "N50 percentile"),
            ("assembly_size_percentile", "Assembly size percentile"),
            ("gc_percentile", "GC percentile"),
            ("busco_complete_percentile", "BUSCO complete percentile"),
            ("busco_duplicated_percentile", "BUSCO duplicated percentile"),
        ]
        lines = []
        for key, label in fields:
            value = target_position.get(key)
            lines.append(f"- **{label}**: {value if value is not None else 'not available'}")
    notes = target_position.get("notes", []) or []
    if notes:
        lines.append("")
        lines.append("**Notes:**")
        for note in notes:
            lines.append(f"- {note}")
    return "\n".join(lines)


def _record_row(record: dict[str, Any]) -> str:
    busco = record.get("busco") or {}
    values = [
        record.get("organism_name"),
        record.get("accession"),
        record.get("assembly_level"),
        record.get("total_sequence_length_bp"),
        record.get("chromosome_count"),
        record.get("contig_n50_bp"),
        record.get("scaffold_n50_bp"),
        record.get("gc_percent"),
        busco.get("lineage"),
        busco.get("complete"),
        busco.get("duplicated"),
        record.get("tier"),
    ]
    return "| " + " | ".join("" if value is None else str(value) for value in values) + " |"


def _display_cohort_markdown(statistics_pack: dict) -> str:
    display_cohort = statistics_pack.get("display_cohort", {}) or {}
    records = display_cohort.get("records", [])
    excluded = display_cohort.get("excluded_alternate_haplotype_records", [])

    if not records:
        lines = ["No related-species assemblies were found in the local snapshot for this target."]
    else:
        header = "| organism_name | accession | assembly_level | total_sequence_length_bp | chromosome_count | contig_n50_bp | scaffold_n50_bp | gc_percent | busco_lineage | busco_complete | busco_duplicated | tier |"
        separator = "| --- " * 12 + "|"
        lines = [header, separator]
        for record in records:
            lines.append(_record_row(record))
    if excluded:
        lines.append("")
        lines.append(f"*{len(excluded)} alternate-haplotype record(s) were excluded from this cohort.*")
    return "\n".join(lines)


def _notes_markdown(statistics_pack: dict, evidence_pack: dict) -> str:
    notes = list(statistics_pack.get("notes", []) or [])
    for note in evidence_pack.get("notes", []) or []:
        if note not in notes:
            notes.append(note)
    lines = []
    if notes:
        for note in notes:
            lines.append(f"- {note}")
    else:
        lines.append("- No additional notes were recorded.")
    lines.append("")
    lines.append(f"**Reminder**: {NOTEBOOK_DISCLAIMER_1}")
    return "\n".join(lines)


def _code_cell_source(statistics_pack_relpath: str) -> str:
    return f'''%matplotlib inline
# The cell above enables inline plotting when this notebook is run interactively.
import json
from pathlib import Path

import matplotlib.pyplot as plt

statistics_pack = json.loads(Path({statistics_pack_relpath!r}).read_text(encoding="utf-8"))

analysis_records = statistics_pack.get("analysis_cohort", {{}}).get("records", [])

total_sequence_length_bp = [r["total_sequence_length_bp"] for r in analysis_records if r.get("total_sequence_length_bp") is not None]
contig_n50_bp = [r.get("contig_n50_bp") if r.get("contig_n50_bp") is not None else r.get("scaffold_n50_bp") for r in analysis_records]
contig_n50_bp = [v for v in contig_n50_bp if v is not None]
gc_percent = [r["gc_percent"] for r in analysis_records if r.get("gc_percent") is not None]

fig, axes = plt.subplots(1, 2, figsize=(10, 4))

if contig_n50_bp:
    axes[0].hist(contig_n50_bp, bins=min(20, max(1, len(contig_n50_bp))))
axes[0].set_title("N50 distribution")
axes[0].set_xlabel("Contig N50 (bp)")
axes[0].set_ylabel("Count")

if total_sequence_length_bp:
    axes[1].hist(total_sequence_length_bp, bins=min(20, max(1, len(total_sequence_length_bp))))
axes[1].set_title("Assembly size distribution")
axes[1].set_xlabel("Total sequence length (bp)")
axes[1].set_ylabel("Count")

fig.tight_layout()
plt.show()
'''


def build_assembly_review_notebook(
    *,
    statistics_pack: dict,
    evidence_pack: dict,
    target_metrics: dict[str, float] | None,
    session_id: str,
    statistics_pack_relpath: str,
    figures_relpaths: dict[str, str],
) -> "nbformat.NotebookNode":
    """Build the comparative NCBI metazoa human-review notebook for a session."""
    evidence_pack = evidence_pack or {}
    cells = []

    disclaimer_block = (
        f"> **{NOTEBOOK_DISCLAIMER_1}**\n"
        f">\n"
        f"> **{NOTEBOOK_DISCLAIMER_2}**"
    )
    cells.append(new_markdown_cell(
        f"# Assembly Review — Comparative NCBI Metazoa Notebook\n\n"
        f"Session: `{session_id}`\n\n"
        f"{disclaimer_block}"
    ))

    if statistics_pack.get("status") != "available":
        notes = statistics_pack.get("notes", []) or []
        notes_text = "\n".join(f"- {note}" for note in notes) if notes else "- No additional notes were recorded."
        cells.append(new_markdown_cell(
            "## Comparative statistics unavailable\n\n"
            "Related-species comparative statistics were not available for this session.\n\n"
            f"{notes_text}\n\n"
            f"**Reminder**: {NOTEBOOK_DISCLAIMER_1}"
        ))
        notebook = new_notebook(cells=cells)
        return notebook

    cells.append(new_markdown_cell(f"## Target assembly summary\n\n{_target_metrics_markdown(target_metrics)}"))
    cells.append(new_markdown_cell(
        f"## Local NCBI snapshot provenance\n\n{_source_provenance_markdown(statistics_pack.get('source', {}))}"
    ))
    cells.append(new_markdown_cell(
        f"## Related species cohort composition\n\n{_cohort_composition_markdown(statistics_pack, figures_relpaths)}"
    ))
    cells.append(new_markdown_cell(
        f"## Distribution figures\n\n{_distribution_figures_markdown(figures_relpaths)}"
    ))
    cells.append(new_code_cell(_code_cell_source(statistics_pack_relpath)))
    cells.append(new_markdown_cell(
        f"## Target vs related-species percentile summary\n\n"
        f"{_percentile_summary_markdown(statistics_pack.get('target_position', {}) or {{}})}"
    ))
    cells.append(new_markdown_cell(
        f"## Selected related assemblies (display cohort)\n\n{_display_cohort_markdown(statistics_pack)}"
    ))
    cells.append(new_markdown_cell(
        f"## Notes and limitations\n\n{_notes_markdown(statistics_pack, evidence_pack)}"
    ))

    return new_notebook(cells=cells)


def write_notebook(notebook: "nbformat.NotebookNode", path: Path) -> None:
    """Write a notebook to disk, creating parent directories as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, path)
