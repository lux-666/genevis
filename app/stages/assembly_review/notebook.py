"""Render comparative NCBI related-species statistics into publication-style \
figures and a review notebook.

This module never produces an automatic pass/fail signal. All output is \
comparative
context intended to help a human reviewer decide whether to accept, rerun, or \
transform
a draft assembly.

Figure design: white background, Arial/Helvetica typography, a unified font \
hierarchy,
a colour-blind-safe palette (reference cohort in low-saturation grey-blue, target \
always
in a vivid red), light reference gridlines only, and 300+ dpi PNG plus vector PDF \
export.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.collections import PolyCollection
    from matplotlib.figure import Figure
    from nbformat import NotebookNode

try:
    import matplotlib

    matplotlib.use("Agg")

    import matplotlib.pyplot as plt  # noqa: E402
    import numpy as np  # noqa: E402
    from matplotlib.collections import PolyCollection  # noqa: E402
    from matplotlib.lines import Line2D  # noqa: E402
except ModuleNotFoundError as exc:
    _FIGURE_IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    _FIGURE_IMPORT_ERROR = None

try:
    import nbformat  # noqa: E402
    from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook  # noqa: E402
except ModuleNotFoundError as exc:
    _NOTEBOOK_IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    _NOTEBOOK_IMPORT_ERROR = None


class _NewCell(Protocol):
    def __call__(self, source: str) -> NotebookNode: ...


class _NewNotebook(Protocol):
    def __call__(self, *, cells: list[NotebookNode]) -> NotebookNode: ...


class _WriteNotebook(Protocol):
    def __call__(self, notebook: NotebookNode, path: Path) -> object: ...


def _notebook_api() -> tuple[_NewCell, _NewCell, _NewNotebook, _WriteNotebook]:
    """Return typed callables after the optional nbformat import succeeds."""

    _require_notebook_dependencies()
    return (
        cast(_NewCell, new_code_cell),
        cast(_NewCell, new_markdown_cell),
        cast(_NewNotebook, new_notebook),
        cast(_WriteNotebook, nbformat.write),
    )


DISCLAIMER = (
    "Comparative reference only — not an automatic threshold or assembly prior."
)
NOTEBOOK_DISCLAIMER_1 = (
    "This notebook is comparative only. It informs the human reviewer's decision; "
    "it is not an automatic threshold and not an assembly prior."
)
NOTEBOOK_DISCLAIMER_2 = (
    "`chromosome_count` values come only from NCBI assembly metadata and are not "
    "equivalent to a verified karyotype."
)
KARYOTYPE_CAPTION = "(NCBI assembly metadata, not verified karyotype)"

# --- Publication style ------------------------------------------------------

# Colour-blind-safe palette. Reference cohort reads as calm grey-blue; the target
# is always a single vivid red, held constant across every figure in the set.
C_REF = "#6B87A8"  # grey-blue — reference cohort points / bars
C_REF_FILL = "#AEC0D6"  # lighter grey-blue — violin / box fills
C_REF_DARK = "#3C5878"  # deep grey-blue — reference emphasis
C_TARGET = "#D1352B"  # vivid red — target, everywhere
C_MEDIAN = "#8A8F98"  # neutral grey — cohort median reference line
C_GRID = "#DDE1E6"  # very light grey — reference gridlines
C_TEXT = "#222629"  # near-black body text
# BUSCO category ramp (single-copy → duplicated → fragmented → missing).
C_BUSCO = ["#4C78A8", "#9ECAE1", "#F0B27A", "#D9534F"]

_EXPORT_DPI = 350
_FIG_SIZE = (5.4, 4.6)  # uniform standalone figure size (inches)
_SUMMARY_SIZE = (14.5, 9.0)  # multi-panel A–G summary


def _require_figure_dependencies() -> None:
    if _FIGURE_IMPORT_ERROR is not None:
        raise RuntimeError(
            "assembly_review comparative figure rendering requires matplotlib "
            "and numpy. "
            "Install them in the active environment before running assembly_review."
        ) from _FIGURE_IMPORT_ERROR


def _require_notebook_dependencies() -> None:
    if _NOTEBOOK_IMPORT_ERROR is not None:
        raise RuntimeError(
            "assembly_review comparative notebook rendering requires nbformat. "
            "Install it in the active environment before running assembly_review."
        ) from _NOTEBOOK_IMPORT_ERROR


def _apply_publication_style() -> None:
    """Set global rcParams for a clean, unified, Nature-style look."""
    _require_figure_dependencies()
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "axes.labelsize": 10.5,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 8.5,
            "figure.titlesize": 14,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
            "axes.edgecolor": "#4A4F55",
            "axes.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.color": "#4A4F55",
            "ytick.color": "#4A4F55",
            "text.color": C_TEXT,
            "axes.labelcolor": C_TEXT,
            "axes.titlecolor": C_TEXT,
            "legend.frameon": False,
            "pdf.fonttype": 42,  # editable (TrueType) text in vector PDF
            "ps.fonttype": 42,
            "svg.fonttype": "none",
        }
    )


def _grid(ax: Axes, axis: Literal["both", "x", "y"] = "y") -> None:
    """Apply light horizontal (or vertical) reference gridlines, drawn behind data."""
    ax.grid(axis=axis, color=C_GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)


def _panel_label(ax: Axes, letter: str) -> None:
    """Place a bold panel label (A, B, …) at the top-left of an axes."""
    ax.annotate(
        letter,
        xy=(0, 1),
        xycoords="axes fraction",
        xytext=(-32, 6),
        textcoords="offset points",
        fontsize=15,
        fontweight="bold",
        va="bottom",
        ha="left",
        color=C_TEXT,
    )


def _human_int(value: float) -> str:
    """Group-separated integer, friendly for axis annotations."""
    return f"{value:,.0f}"


def _add_disclaimer(fig: Figure) -> None:
    fig.text(
        0.5,
        0.012,
        DISCLAIMER,
        ha="center",
        va="bottom",
        fontsize=7,
        style="italic",
        color=C_MEDIAN,
    )


# --- Data extraction --------------------------------------------------------


def _n50_values(records: list[dict[str, Any]]) -> list[float]:
    values = []
    for record in records:
        # Prefer scaffold N50 to match the precedence used in percentile computation.
        n50 = record.get("scaffold_n50_bp")
        if n50 is None:
            n50 = record.get("contig_n50_bp")
        if n50 is not None:
            values.append(n50)
    return values


def _field_values(records: list[dict[str, Any]], key: str) -> list[float]:
    return [record[key] for record in records if record.get(key) is not None]


# Per-metric config for the four combined distribution figures. `scale` converts
# raw units to the friendly axis unit; `target_key`/`pct_key` pull the target value
# and its precomputed percentile; `fmt` renders values for annotations.
class _DistributionSpec(TypedDict):
    title: str
    ylabel: str
    scale: float
    target_key: str | None
    pct_key: str | None
    fmt: Callable[[float], str]


_DIST_SPECS: dict[str, _DistributionSpec] = {
    "n50_distribution": {
        "title": "Contig N50",
        "ylabel": "Contig N50 (Mb)",
        "scale": 1e-6,
        "target_key": "n50_bp",
        "pct_key": "n50_percentile",
        "fmt": lambda v: f"{v:.2f} Mb",
    },
    "assembly_size_distribution": {
        "title": "Assembly size",
        "ylabel": "Total sequence length (Mb)",
        "scale": 1e-6,
        "target_key": "assembly_size_bp",
        "pct_key": "assembly_size_percentile",
        "fmt": lambda v: f"{v:,.0f} Mb",
    },
    "gc_distribution": {
        "title": "GC content",
        "ylabel": "GC content (%)",
        "scale": 1.0,
        "target_key": "gc_percent",
        "pct_key": "gc_percentile",
        "fmt": lambda v: f"{v:.1f} %",
    },
    "chromosome_count_distribution": {
        "title": "Chromosome count",
        "ylabel": "Chromosome count (n)",
        "scale": 1.0,
        "target_key": None,  # no target chromosome count in the data model
        "pct_key": None,
        "fmt": lambda v: f"{v:.0f}",
    },
}


# --- Panel drawers (each renders into a single Axes so figures and the A–G
#     summary panel share one implementation) --------------------------------


def _draw_distribution(
    ax: Axes,
    spec: _DistributionSpec,
    raw_values: list[float],
    target_value: float | None,
    percentile: float | None,
) -> None:
    """Combined violin + box + jittered-point distribution with a target marker.

    Adapts to cohort size: violin density is only added when n >= 10 and the values
    have spread; for small cohorts the raw points and box carry the distribution.
    """
    scale = spec["scale"]
    vals = np.asarray([v * scale for v in raw_values], dtype=float)
    n = vals.size
    x = 1.0

    _grid(ax, axis="y")

    spread = float(np.ptp(vals)) if n else 0.0
    if n >= 10 and spread > 0:
        parts = ax.violinplot(
            [vals], positions=[x], widths=0.75, showextrema=False, showmedians=False
        )
        for body in cast(list[PolyCollection], parts["bodies"]):
            body.set_facecolor(C_REF_FILL)
            body.set_edgecolor(C_REF)
            body.set_alpha(0.45)
            body.set_linewidth(0.8)
            body.set_zorder(1)

    if n:
        bp = ax.boxplot(
            [vals],
            positions=[x],
            widths=0.24,
            patch_artist=True,
            showfliers=False,
            zorder=2,
        )
        for box in bp["boxes"]:
            box.set(facecolor="white", edgecolor=C_REF_DARK, linewidth=1.1, alpha=0.95)
        for element in ("whiskers", "caps"):
            for line in bp[element]:
                line.set(color=C_REF_DARK, linewidth=1.1)
        for median in bp["medians"]:
            median.set(color=C_REF_DARK, linewidth=1.6)

        rng = np.random.default_rng(42)
        jitter = rng.uniform(-0.11, 0.11, size=n)
        ax.scatter(
            np.full(n, x) + jitter,
            vals,
            s=26,
            facecolor=C_REF,
            edgecolor="white",
            linewidth=0.5,
            alpha=0.85,
            zorder=3,
        )

        cohort_median = float(np.median(vals))
        ax.axhline(
            cohort_median,
            color=C_MEDIAN,
            linestyle="--",
            linewidth=1.0,
            zorder=1,
            label="cohort median",
        )

    if target_value is not None:
        tv = target_value * scale
        ax.scatter(
            [x + 0.42],
            [tv],
            marker="*",
            s=340,
            color=C_TARGET,
            edgecolor="white",
            linewidth=0.8,
            zorder=5,
            label="target",
        )

    ax.set_xlim(0.4, 1.85)
    ax.set_xticks([])
    ax.set_ylabel(spec["ylabel"])
    ax.set_title(spec["title"])

    # Compact stats box: n, cohort median, and target position.
    lines = [f"cohort n = {n}"]
    if n:
        lines.append(f"median = {spec['fmt'](float(np.median(vals)))}")
    if target_value is not None:
        lines.append(f"target = {spec['fmt'](target_value * scale)}")
        if percentile is not None:
            lines.append(f"target pct = {percentile:.0f}th")
    ax.text(
        0.03,
        0.97,
        "\n".join(lines),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=8,
        color=C_TEXT,
        bbox=dict(
            boxstyle="round,pad=0.4",
            facecolor="white",
            edgecolor=C_GRID,
            linewidth=0.8,
            alpha=0.9,
        ),
    )


_BUSCO_LABELS = [
    "Complete (single-copy)",
    "Complete (duplicated)",
    "Fragmented",
    "Missing",
]
_BUSCO_ROW_CAP = 15  # cap reference rows so the chart stays readable


def _busco_segments(busco: dict[str, Any]) -> list[float] | None:
    """Return [single_copy, duplicated, fragmented, missing] percentages, or None.

    Falls back to complete - duplicated for single-copy, and folds any unknown
    remainder into Missing so each bar sums to ~100%.
    """
    if not busco:
        return None
    complete = busco.get("complete")
    duplicated = busco.get("duplicated")
    if complete is None and duplicated is None:
        return None
    complete = complete if complete is not None else 0.0
    duplicated = duplicated if duplicated is not None else 0.0
    single = busco.get("single_copy")
    if single is None:
        single = max(complete - duplicated, 0.0)
    fragmented = busco.get("fragmented")
    missing = busco.get("missing")
    if fragmented is None or missing is None:
        # Only complete/duplicated are known (e.g. the target): fold remainder
        # into Missing.
        fragmented = 0.0 if fragmented is None else fragmented
        missing = max(100.0 - single - duplicated - fragmented, 0.0)
    return [single, duplicated, fragmented, missing]


def _draw_busco(
    ax: Axes,
    records: list[dict[str, Any]],
    target_metrics: dict[str, float] | None,
) -> bool:
    "Horizontal stacked BUSCO bars; target on top, references sorted by completeness."
    rows: list[tuple[str, list[float], bool]] = []

    if target_metrics and (
        target_metrics.get("busco_complete_percent") is not None
        or target_metrics.get("busco_duplicated_percent") is not None
    ):
        seg = _busco_segments(
            {
                "complete": target_metrics.get("busco_complete_percent"),
                "duplicated": target_metrics.get("busco_duplicated_percent"),
            }
        )
        if seg is not None:
            rows.append(("Target assembly", seg, True))

    ref_rows: list[tuple[str, list[float], float]] = []
    for record in records:
        seg = _busco_segments(record.get("busco") or {})
        if seg is None:
            continue
        name = record.get("organism_name") or record.get("accession") or "reference"
        ref_rows.append((name, seg, seg[0] + seg[1]))
    ref_rows.sort(key=lambda r: r[2], reverse=True)  # best completeness first
    hidden = max(0, len(ref_rows) - _BUSCO_ROW_CAP)
    for name, seg, _complete in ref_rows[:_BUSCO_ROW_CAP]:
        rows.append((name, seg, False))

    if not rows:
        return False

    # rows = [target, best_ref, …]; reverse the y-axis so row 0 (target) sits on top.
    y = np.arange(len(rows))[::-1]
    for seg_idx in range(4):
        left = np.zeros(len(rows))
        for prior in range(seg_idx):
            left += np.array([r[1][prior] for r in rows])
        widths = np.array([r[1][seg_idx] for r in rows])
        ax.barh(
            y,
            widths,
            left=left,
            height=0.68,
            color=C_BUSCO[seg_idx],
            edgecolor="white",
            linewidth=0.6,
            zorder=2,
            label=_BUSCO_LABELS[seg_idx],
        )

    labels = [name if len(name) <= 26 else name[:24] + "…" for name, _seg, _t in rows]
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    for tick, (_name, _seg, is_target) in zip(ax.get_yticklabels(), rows):
        if is_target:
            tick.set_color(C_TARGET)
            tick.set_fontweight("bold")

    ax.set_xlim(0, 100)
    ax.set_xlabel("BUSCO genes (%)")
    ax.set_title("BUSCO completeness composition")
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color=C_GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.11),
        ncol=4,
        fontsize=8,
        columnspacing=1.2,
        handlelength=1.2,
        borderaxespad=0.0,
    )
    caption = (
        "Target Fragmented+Missing shown as one segment "
        "(only Complete/Duplicated reported)."
    )
    if hidden:
        caption += f"  {hidden} further reference(s) not shown."
    ax.text(
        0.0,
        -0.2,
        caption,
        transform=ax.transAxes,
        fontsize=7,
        color=C_MEDIAN,
        va="top",
        ha="left",
    )
    return True


def _draw_scatter(
    ax: Axes,
    records: list[dict[str, Any]],
    target_metrics: dict[str, float] | None,
) -> bool:
    """N50 vs assembly size scatter (Mb / log N50), quadrants at cohort medians.

    Reference points are grey-blue; point size encodes BUSCO complete% when available.
    Target is a red star.
    """
    xs, ys, comp = [], [], []
    for record in records:
        size = record.get("total_sequence_length_bp")
        # Prefer scaffold N50 to match the precedence used in percentile computation.
        n50 = record.get("scaffold_n50_bp")
        if n50 is None:
            n50 = record.get("contig_n50_bp")
        if size is None or n50 is None:
            continue
        xs.append(size * 1e-6)
        ys.append(n50 * 1e-6)
        busco = record.get("busco") or {}
        comp.append(busco.get("complete"))
    if not xs:
        return False

    xs_a, ys_a = np.asarray(xs), np.asarray(ys)
    comp_present = [c for c in comp if c is not None]
    size_encoded = len(comp_present) >= 3
    if size_encoded:
        lo, hi = min(comp_present), max(comp_present)
        span = (hi - lo) or 1.0
        sizes = np.array(
            [30 + 130 * ((c - lo) / span) if c is not None else 45 for c in comp]
        )
    else:
        sizes = np.full(len(xs), 55.0)

    _grid(ax, axis="both")
    ax.axvline(
        float(np.median(xs_a)), color=C_MEDIAN, linestyle="--", linewidth=1.0, zorder=1
    )
    ax.axhline(
        float(np.median(ys_a)), color=C_MEDIAN, linestyle="--", linewidth=1.0, zorder=1
    )
    ax.scatter(
        xs_a,
        ys_a,
        s=sizes,
        facecolor=C_REF,
        edgecolor="white",
        linewidth=0.5,
        alpha=0.85,
        zorder=3,
        label="reference",
    )

    if (
        target_metrics
        and target_metrics.get("assembly_size_bp") is not None
        and target_metrics.get("n50_bp") is not None
    ):
        ax.scatter(
            [target_metrics["assembly_size_bp"] * 1e-6],
            [target_metrics["n50_bp"] * 1e-6],
            marker="*",
            s=380,
            color=C_TARGET,
            edgecolor="white",
            linewidth=0.8,
            zorder=5,
            label="target",
        )

    ax.set_yscale("log")
    ax.set_xlabel("Total sequence length (Mb)")
    ax.set_ylabel("Contig N50 (Mb, log scale)")
    ax.set_title("N50 vs assembly size")
    handles = [
        Line2D(
            [],
            [],
            marker="o",
            linestyle="",
            markerfacecolor=C_REF,
            markeredgecolor="white",
            markersize=8,
            label="reference",
        ),
        Line2D(
            [],
            [],
            marker="*",
            linestyle="",
            markerfacecolor=C_TARGET,
            markeredgecolor="white",
            markersize=15,
            label="target",
        ),
        Line2D([], [], color=C_MEDIAN, linestyle="--", label="cohort median"),
    ]
    ax.legend(handles=handles, loc="best")
    if size_encoded:
        ax.text(
            0.03,
            0.03,
            "point size scales with BUSCO complete %",
            transform=ax.transAxes,
            fontsize=7,
            color=C_MEDIAN,
            va="bottom",
            ha="left",
        )
    return True


_TIER_LABELS = {
    "exact": "Same species",
    "same_genus": "Same genus",
    "same_family": "Same family",
    "same_order": "Same order",
}
_TIER_ORDER = ["exact", "same_genus", "same_family", "same_order"]


def _draw_composition(ax: Axes, computed_summary: dict[str, Any]) -> bool:
    """Horizontal bar chart of cohort composition by taxonomic relationship tier."""
    by_tier = (computed_summary.get("cohort_composition", {}) or {}).get(
        "by_tier", {}
    ) or {}
    items = [
        (tier, by_tier.get(tier, 0)) for tier in _TIER_ORDER if by_tier.get(tier, 0)
    ]
    if not items:
        return False

    labels = [_TIER_LABELS.get(t, t) for t, _ in items]
    counts = [c for _, c in items]
    total = sum(counts)
    y = np.arange(len(items))[::-1]  # first tier on top

    ax.grid(axis="x", color=C_GRID, linewidth=0.7, zorder=0)
    ax.set_axisbelow(True)
    ax.barh(
        y, counts, height=0.62, color=C_REF, edgecolor="white", linewidth=0.6, zorder=2
    )
    for yi, c in zip(y, counts):
        ax.text(
            c + max(counts) * 0.015,
            yi,
            str(c),
            va="center",
            ha="left",
            fontsize=9,
            color=C_TEXT,
        )
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlim(0, max(counts) * 1.16)
    ax.set_xlabel("Number of reference assemblies")
    ax.set_title(f"Cohort composition (n = {total})")
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)
    return True


# --- Orchestration ----------------------------------------------------------


def render_statistics_figures(
    statistics_pack: dict[str, Any],
    out_dir: Path,
    target_metrics: dict[str, float] | None = None,
) -> dict[str, str]:
    """Render the comparative publication-style charts for a related-species \
statistics pack.

    Produces the seven per-metric figures plus one combined A–G \
``summary_panel``.
    Each figure is written as a 300+ dpi PNG and a vector PDF sharing the same \
stem;
    the returned mapping points at the PNG path (logical name -> absolute path).

    Writes nothing and returns {} when the pack is unavailable or has no \
analysis
    cohort records. Never raises for missing/absent underlying data fields; \
skips
    individual figures whose data is entirely empty instead.
    """
    _require_figure_dependencies()
    _apply_publication_style()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if statistics_pack.get("status") != "available":
        return {}

    analysis_records = statistics_pack.get("analysis_cohort", {}).get("records", [])
    if not analysis_records:
        return {}

    target_position = statistics_pack.get("target_position", {}) or {}
    computed_summary = statistics_pack.get("computed_summary", {}) or {}
    target_metrics = target_metrics or {}
    figures: dict[str, str] = {}

    def _save(name: str, fig: Figure) -> None:
        _add_disclaimer(fig)
        png_path = out_dir / f"{name}.png"
        fig.savefig(png_path, dpi=_EXPORT_DPI)
        fig.savefig(out_dir / f"{name}.pdf")  # vector companion
        plt.close(fig)
        figures[name] = str(png_path.resolve())

    # Per-metric raw value lists, keyed by figure name.
    dist_values = {
        "n50_distribution": _n50_values(analysis_records),
        "assembly_size_distribution": _field_values(
            analysis_records, "total_sequence_length_bp"
        ),
        "gc_distribution": _field_values(analysis_records, "gc_percent"),
        "chromosome_count_distribution": _field_values(
            analysis_records, "chromosome_count"
        ),
    }

    # 1–3 + 5. Combined distribution figures.
    for name, spec in _DIST_SPECS.items():
        values = dist_values[name]
        if not values:
            continue
        target_key = spec["target_key"]
        percentile_key = spec["pct_key"]
        tv = target_metrics.get(target_key) if target_key else None
        pct = target_position.get(percentile_key) if percentile_key else None
        fig, ax = plt.subplots(figsize=_FIG_SIZE)
        _draw_distribution(ax, spec, values, tv, pct)
        if any(
            h.get_label() in ("cohort median", "target")
            for h in ax.get_children()
            if hasattr(h, "get_label")
        ):
            ax.legend(loc="upper right")
        title = spec["title"]
        if name == "chromosome_count_distribution":
            title += f" {KARYOTYPE_CAPTION}"
        ax.set_title(f"{title} — related-species cohort")
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        _save(name, fig)

    # 4. BUSCO composition.
    fig, ax = plt.subplots(figsize=(7.4, 5.2))
    if _draw_busco(ax, analysis_records, target_metrics):
        fig.tight_layout(rect=(0, 0.08, 1, 1))
        _save("busco_comparison", fig)
    else:
        plt.close(fig)

    # 6. N50 vs assembly size scatter.
    fig, ax = plt.subplots(figsize=_FIG_SIZE)
    if _draw_scatter(ax, analysis_records, target_metrics):
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        _save("n50_vs_assembly_size", fig)
    else:
        plt.close(fig)

    # 7. Cohort composition.
    fig, ax = plt.subplots(figsize=_FIG_SIZE)
    if _draw_composition(ax, computed_summary):
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        _save("cohort_composition", fig)
    else:
        plt.close(fig)

    # 8. Combined A–G multi-panel summary figure.
    _render_summary_panel(
        out_dir,
        _save,
        analysis_records,
        computed_summary,
        target_metrics,
        target_position,
        dist_values,
    )

    return figures


def _render_summary_panel(
    out_dir: Path,
    _save: Callable[[str, Figure], None],
    analysis_records: list[dict[str, Any]],
    computed_summary: dict[str, Any],
    target_metrics: dict[str, float],
    target_position: dict[str, Any],
    dist_values: dict[str, list[float]],
) -> None:
    """Assemble panels A–G into one publication-ready multi-panel figure."""
    fig = plt.figure(figsize=_SUMMARY_SIZE)
    gs = fig.add_gridspec(
        2, 4, hspace=0.55, wspace=0.55, left=0.145, right=0.985, top=0.90, bottom=0.08
    )

    letters = iter("ABCDEFG")

    # Top row (A–D): the four distributions.
    for col, (name, spec) in enumerate(_DIST_SPECS.items()):
        ax = fig.add_subplot(gs[0, col])
        _panel_label(ax, next(letters))
        values = dist_values[name]
        if not values:
            ax.set_axis_off()
            continue
        target_key = spec["target_key"]
        percentile_key = spec["pct_key"]
        tv = target_metrics.get(target_key) if target_key else None
        pct = target_position.get(percentile_key) if percentile_key else None
        _draw_distribution(ax, spec, values, tv, pct)

    # Bottom row: E busco (wide), F scatter, G composition.
    ax_e = fig.add_subplot(gs[1, 0:2])
    _panel_label(ax_e, next(letters))
    if not _draw_busco(ax_e, analysis_records, target_metrics):
        ax_e.set_axis_off()

    ax_f = fig.add_subplot(gs[1, 2])
    _panel_label(ax_f, next(letters))
    if not _draw_scatter(ax_f, analysis_records, target_metrics):
        ax_f.set_axis_off()

    ax_g = fig.add_subplot(gs[1, 3])
    _panel_label(ax_g, next(letters))
    if not _draw_composition(ax_g, computed_summary):
        ax_g.set_axis_off()

    fig.suptitle(
        "Assembly review — comparative related-species summary",
        fontsize=15,
        fontweight="bold",
        y=0.99,
    )
    _save("summary_panel", fig)


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
        lines.append(
            "- **WARNING**: the local NCBI snapshot failed verification "
            "(file hash mismatch); treat this comparison with caution."
        )
    return "\n".join(lines)


def _cohort_composition_markdown(
    statistics_pack: dict[str, Any], figures_relpaths: dict[str, str]
) -> str:
    tier_counts = statistics_pack.get("tier_counts", {}) or {}
    computed_summary = statistics_pack.get("computed_summary", {}) or {}
    cohort_composition = computed_summary.get("cohort_composition", {}) or {}
    display_cohort = statistics_pack.get("display_cohort", {}) or {}
    analysis_cohort = statistics_pack.get("analysis_cohort", {}) or {}

    lines = [
        "**Tier counts:**",
        f"- exact: {tier_counts.get('exact', 0)}, "
        f"same_genus: {tier_counts.get('same_genus', 0)}, "
        f"same_family: {tier_counts.get('same_family', 0)}, "
        f"same_order: {tier_counts.get('same_order', 0)}",
        "",
        f"**Record count (computed summary)**: "
        f"{computed_summary.get('record_count', 0)}",
        "",
        f"**Display cohort**: {len(display_cohort.get('records', []))} / limit "
        f"{display_cohort.get('size_limit')}",
        f"**Analysis cohort**: {len(analysis_cohort.get('records', []))} / limit "
        f"{analysis_cohort.get('size_limit')}",
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
    ("busco_comparison", "BUSCO completeness composition"),
    ("chromosome_count_distribution", "Chromosome count distribution"),
    ("n50_vs_assembly_size", "N50 vs assembly size"),
]


def _distribution_figures_markdown(figures_relpaths: dict[str, str]) -> str:
    lines = []
    if "summary_panel" in figures_relpaths:
        lines.append(
            f"![Comparative summary (A–G)]({figures_relpaths['summary_panel']})"
        )
        lines.append("")
        lines.append("*Multi-panel summary; individual panels follow below.*")
        lines.append("")
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
            lines.append(
                f"- **{label}**: {value if value is not None else 'not available'}"
            )
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
    return (
        "| "
        + " | ".join("" if value is None else str(value) for value in values)
        + " |"
    )


def _display_cohort_markdown(statistics_pack: dict[str, Any]) -> str:
    display_cohort = statistics_pack.get("display_cohort", {}) or {}
    records = display_cohort.get("records", [])
    excluded = display_cohort.get("excluded_alternate_haplotype_records", [])

    if not records:
        lines = [
            "No related-species assemblies were found in the local snapshot for "
            "this target."
        ]
    else:
        header = (
            "| organism_name | accession | assembly_level | "
            "total_sequence_length_bp | chromosome_count | contig_n50_bp | "
            "scaffold_n50_bp | gc_percent | busco_lineage | busco_complete | "
            "busco_duplicated | tier |"
        )
        separator = "| --- " * 12 + "|"
        lines = [header, separator]
        for record in records:
            lines.append(_record_row(record))
    if excluded:
        lines.append("")
        lines.append(
            f"*{len(excluded)} alternate-haplotype record(s) were excluded "
            "from this cohort.*"
        )
    return "\n".join(lines)


def _notes_markdown(
    statistics_pack: dict[str, Any], evidence_pack: dict[str, Any]
) -> str:
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
    return (
        "%matplotlib inline\n"
        "# The cell above enables inline plotting when this notebook is run "
        "interactively.\n"
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "import matplotlib.pyplot as plt\n"
        "\n"
        f"statistics_pack = json.loads(Path({statistics_pack_relpath!r})"
        '.read_text(encoding="utf-8"))\n'
        "\n"
        'analysis_records = statistics_pack.get("analysis_cohort", {})'
        '.get("records", [])\n'
        "\n"
        'total_sequence_length_bp = [r["total_sequence_length_bp"] for r in '
        "analysis_records "
        'if r.get("total_sequence_length_bp") is not None]\n'
        'contig_n50_bp = [r.get("contig_n50_bp") '
        'if r.get("contig_n50_bp") is not None '
        'else r.get("scaffold_n50_bp") for r in analysis_records]\n'
        "contig_n50_bp = [v for v in contig_n50_bp if v is not None]\n"
        'gc_percent = [r["gc_percent"] for r in analysis_records '
        'if r.get("gc_percent") is not None]\n'
        "\n"
        "fig, axes = plt.subplots(1, 2, figsize=(10, 4))\n"
        "\n"
        "if contig_n50_bp:\n"
        "    axes[0].hist(contig_n50_bp, "
        "bins=min(20, max(1, len(contig_n50_bp))))\n"
        'axes[0].set_title("N50 distribution")\n'
        'axes[0].set_xlabel("Contig N50 (bp)")\n'
        'axes[0].set_ylabel("Count")\n'
        "\n"
        "if total_sequence_length_bp:\n"
        "    axes[1].hist(total_sequence_length_bp, "
        "bins=min(20, max(1, len(total_sequence_length_bp))))\n"
        'axes[1].set_title("Assembly size distribution")\n'
        'axes[1].set_xlabel("Total sequence length (bp)")\n'
        'axes[1].set_ylabel("Count")\n'
        "\n"
        "fig.tight_layout()\n"
        "fig\n"
    )


def build_assembly_review_notebook(
    *,
    statistics_pack: dict[str, Any],
    evidence_pack: dict[str, Any],
    target_metrics: dict[str, float] | None,
    session_id: str,
    statistics_pack_relpath: str,
    figures_relpaths: dict[str, str],
) -> NotebookNode:
    """Build the comparative NCBI metazoa human-review notebook for a session."""
    (
        new_code_cell_typed,
        new_markdown_cell_typed,
        new_notebook_typed,
        _write_notebook_typed,
    ) = _notebook_api()
    evidence_pack = evidence_pack or {}
    cells: list[NotebookNode] = []

    disclaimer_block = (
        f"> **{NOTEBOOK_DISCLAIMER_1}**\n>\n> **{NOTEBOOK_DISCLAIMER_2}**"
    )
    cells.append(
        new_markdown_cell_typed(
            f"# Assembly Review — Comparative NCBI Metazoa Notebook\n\n"
            f"Session: `{session_id}`\n\n"
            f"{disclaimer_block}"
        )
    )

    if statistics_pack.get("status") != "available":
        notes = statistics_pack.get("notes", []) or []
        notes_text = (
            "\n".join(f"- {note}" for note in notes)
            if notes
            else "- No additional notes were recorded."
        )
        cells.append(
            new_markdown_cell_typed(
                "## Comparative statistics unavailable\n\n"
                "Related-species comparative statistics were not available for "
                "this session.\n\n"
                f"{notes_text}\n\n"
                f"**Reminder**: {NOTEBOOK_DISCLAIMER_1}"
            )
        )
        notebook = new_notebook_typed(cells=cells)
        return notebook

    cells.append(
        new_markdown_cell_typed(
            f"## Target assembly summary\n\n{_target_metrics_markdown(target_metrics)}"
        )
    )
    cells.append(
        new_markdown_cell_typed(
            "## Local NCBI snapshot provenance\n\n"
            f"{_source_provenance_markdown(statistics_pack.get('source', {}))}"
        )
    )
    cells.append(
        new_markdown_cell_typed(
            "## Related species cohort composition\n\n"
            f"{_cohort_composition_markdown(statistics_pack, figures_relpaths)}"
        )
    )
    cells.append(
        new_markdown_cell_typed(
            "## Distribution figures\n\n"
            f"{_distribution_figures_markdown(figures_relpaths)}"
        )
    )
    cells.append(new_code_cell_typed(_code_cell_source(statistics_pack_relpath)))
    raw_target_position = statistics_pack.get("target_position", {})
    target_position = (
        raw_target_position if isinstance(raw_target_position, dict) else {}
    )
    cells.append(
        new_markdown_cell_typed(
            f"## Target vs related-species percentile summary\n\n"
            f"{_percentile_summary_markdown(target_position)}"
        )
    )
    cells.append(
        new_markdown_cell_typed(
            "## Selected related assemblies (display cohort)\n\n"
            f"{_display_cohort_markdown(statistics_pack)}"
        )
    )
    cells.append(
        new_markdown_cell_typed(
            "## Notes and limitations\n\n"
            f"{_notes_markdown(statistics_pack, evidence_pack)}"
        )
    )

    return new_notebook_typed(cells=cells)


def write_notebook(notebook: NotebookNode, path: Path) -> None:
    """Write a notebook to disk, creating parent directories as needed."""
    _new_code_cell, _new_markdown_cell, _new_notebook, write_notebook_typed = (
        _notebook_api()
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_notebook_typed(notebook, path)
