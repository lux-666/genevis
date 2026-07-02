"""Tests for the assembly review comparative statistics notebook module."""

from __future__ import annotations

import ast
import statistics
import tempfile
import unittest
from pathlib import Path

import nbformat

from app.stages.assembly_review.notebook import (
    NOTEBOOK_DISCLAIMER_1,
    NOTEBOOK_DISCLAIMER_2,
    build_assembly_review_notebook,
    render_statistics_figures,
    write_notebook,
)

_PNG_MAGIC = b"\x89PNG"

_ALL_FIGURE_KEYS = [
    "n50_distribution",
    "assembly_size_distribution",
    "gc_distribution",
    "busco_comparison",
    "chromosome_count_distribution",
    "n50_vs_assembly_size",
    "cohort_composition",
    "summary_panel",
]


def _quartiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "q1": statistics.quantiles(ordered, n=4)[0],
        "median": statistics.median(ordered),
        "q3": statistics.quantiles(ordered, n=4)[2],
        "max": ordered[-1],
    }


def _make_record(
    index: int,
    *,
    total_sequence_length_bp: int | None,
    contig_n50_bp: int | None,
    gc_percent: float | None,
    chromosome_count: int | None,
    busco_complete: float | None = 95.0,
    busco_duplicated: float | None = 1.0,
    tier: str = "same_genus",
) -> dict:
    return {
        "accession": f"GCF_0000000{index:02d}.1",
        "assembly_name": f"assembly_{index}",
        "organism_name": f"Targetus relative {index}",
        "taxid": str(1000 + index),
        "assembly_method": "hifiasm",
        "total_sequence_length_bp": total_sequence_length_bp,
        "chromosome_count": chromosome_count,
        "contig_count": 100 + index,
        "scaffold_count": 50 + index,
        "contig_n50_bp": contig_n50_bp,
        "scaffold_n50_bp": (contig_n50_bp * 2) if contig_n50_bp else None,
        "gc_percent": gc_percent,
        "busco": (
            {
                "lineage": "actinopterygii_odb10",
                "complete": busco_complete,
                "single_copy": (busco_complete - 1.0) if busco_complete is not None else None,
                "duplicated": busco_duplicated,
                "fragmented": 1.0,
                "missing": 3.0,
                "total_count": 3640,
                "version": "5.4.3",
            }
            if busco_complete is not None or busco_duplicated is not None
            else None
        ),
        "is_alternate_haplotype": False,
        "alternate_haplotype_reason": None,
        "assembly_level": "scaffold",
        "tier": tier,
    }


def _make_statistics_pack(*, records: list[dict] | None = None, status: str = "available") -> dict:
    if records is None:
        n50_values = [10_000_000, 11_000_000, 9_500_000, 12_000_000, 10_500_000, 9_000_000, 13_000_000, 8_500_000, 10_200_000, 11_500_000]
        size_values = [500_000_000 + i * 10_000_000 for i in range(10)]
        gc_values = [38.0 + i * 0.5 for i in range(10)]
        chromosome_values = [21, 21, 21, 22, 21, 20, 21, 21, 22, 21]
        records = [
            _make_record(
                i,
                total_sequence_length_bp=size_values[i],
                contig_n50_bp=n50_values[i],
                gc_percent=gc_values[i],
                chromosome_count=chromosome_values[i],
            )
            for i in range(10)
        ]

    n50_values_present = [r["contig_n50_bp"] for r in records if r.get("contig_n50_bp") is not None]
    size_values_present = [r["total_sequence_length_bp"] for r in records if r.get("total_sequence_length_bp") is not None]
    gc_values_present = [r["gc_percent"] for r in records if r.get("gc_percent") is not None]
    busco_complete_present = [r["busco"]["complete"] for r in records if r.get("busco") and r["busco"].get("complete") is not None]
    busco_duplicated_present = [r["busco"]["duplicated"] for r in records if r.get("busco") and r["busco"].get("duplicated") is not None]

    metrics = {}
    if n50_values_present:
        metrics["contig_n50_bp"] = _quartiles(n50_values_present)
    if size_values_present:
        metrics["total_sequence_length_bp"] = _quartiles(size_values_present)
    if gc_values_present:
        metrics["gc_percent"] = _quartiles(gc_values_present)
    if busco_complete_present:
        metrics["busco_complete_percent"] = _quartiles(busco_complete_present)
    if busco_duplicated_present:
        metrics["busco_duplicated_percent"] = _quartiles(busco_duplicated_present)

    chromosome_counts = [r["chromosome_count"] for r in records if r.get("chromosome_count") is not None]
    mode = None
    if chromosome_counts:
        mode = max(set(chromosome_counts), key=chromosome_counts.count)

    return {
        "schema_version": 1,
        "status": status,
        "target": {
            "input_name": "Targetus absentus",
            "canonical_scientific_name": "Targetus absentus",
            "resolution": "alias_table",
            "taxonomic_hints": {"genus": "Targetus", "family": "Targetidae", "order": "Targetiformes"},
        },
        "source": {
            "tsv": "Metazoa.ncbi.tsv",
            "lineage": "Metazoa.ncbi.lineage",
            "alias": "species_alias.tsv",
            "manifest": "reference_manifest.json",
            "verification": "verified",
            "dataset_id": "ncbi_metazoa_test",
            "source_provider": "test",
            "source_version": "test-v1",
            "exported_at": "2026-06-23T00:00:00Z",
        },
        "tier_counts": {"exact": 0, "same_genus": len(records), "same_family": 0, "same_order": 0},
        "display_cohort": {
            "size_limit": 25,
            "records": records[:5],
            "excluded_alternate_haplotype_records": [],
        },
        "analysis_cohort": {
            "size_limit": 150,
            "records": records,
            "excluded_alternate_haplotype_records": [],
        },
        "computed_summary": {
            "cohort": "analysis_cohort",
            "record_count": len(records),
            "metrics": metrics,
            "chromosome_count_mode": mode,
            "busco_lineages": ["actinopterygii_odb10"],
            "cohort_composition": {
                "by_tier": {"exact": 0, "same_genus": len(records), "same_family": 0, "same_order": 0},
                "by_accession_type": {"GCF": len(records), "GCA": 0},
                "by_assembly_level": {"chromosome": 0, "scaffold": len(records), "contig": 0, "unknown": 0},
            },
        },
        "target_position": {
            "available": True,
            "n50_percentile": 55.0,
            "assembly_size_percentile": 40.0,
            "gc_percentile": None,
            "busco_complete_percentile": 60.0,
            "busco_duplicated_percentile": None,
            "basis": {
                "n50": "contig_n50_bp",
                "assembly_size": "total_sequence_length_bp",
                "gc": "gc_percent",
                "busco_complete": "busco_complete_percent",
                "busco_duplicated": "busco_duplicated_percent",
            },
            "notes": ["percentiles computed against analysis_cohort"],
        },
        "notes": ["synthetic test fixture"],
    }


def _unavailable_statistics_pack() -> dict:
    return {
        "schema_version": 1,
        "status": "unavailable",
        "target": {
            "input_name": None,
            "canonical_scientific_name": None,
            "resolution": "unresolved",
            "taxonomic_hints": {},
        },
        "source": {
            "tsv": "Metazoa.ncbi.tsv",
            "lineage": "Metazoa.ncbi.lineage",
            "alias": "species_alias.tsv",
            "manifest": "reference_manifest.json",
            "verification": "unverified",
            "dataset_id": None,
            "source_provider": None,
            "source_version": None,
            "exported_at": None,
        },
        "tier_counts": {"exact": 0, "same_genus": 0, "same_family": 0, "same_order": 0},
        "display_cohort": {"size_limit": 25, "records": [], "excluded_alternate_haplotype_records": []},
        "analysis_cohort": {"size_limit": 150, "records": [], "excluded_alternate_haplotype_records": []},
        "computed_summary": {
            "cohort": "analysis_cohort",
            "record_count": 0,
            "metrics": {},
            "chromosome_count_mode": None,
            "busco_lineages": [],
            "cohort_composition": {
                "by_tier": {"exact": 0, "same_genus": 0, "same_family": 0, "same_order": 0},
                "by_accession_type": {"GCF": 0, "GCA": 0},
                "by_assembly_level": {"chromosome": 0, "scaffold": 0, "contig": 0, "unknown": 0},
            },
        },
        "target_position": {
            "available": False,
            "n50_percentile": None,
            "assembly_size_percentile": None,
            "gc_percentile": None,
            "busco_complete_percentile": None,
            "busco_duplicated_percentile": None,
            "basis": {},
            "notes": [],
        },
        "notes": ["target_species_not_provided"],
    }


def _evidence_pack() -> dict:
    return {
        "matching_mode": "same_genus",
        "selected_records": [{"accession": "GCF_000000001.1", "organism_name": "Targetus relative 1"}],
        "notes": ["evidence pack note"],
    }


class RenderStatisticsFiguresTests(unittest.TestCase):
    def test_unavailable_pack_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "figures"
            result = render_statistics_figures(_unavailable_statistics_pack(), out_dir)

            self.assertEqual(result, {})
            self.assertTrue(out_dir.exists())
            self.assertEqual(list(out_dir.iterdir()), [])

    def test_empty_analysis_cohort_writes_nothing(self):
        pack = _make_statistics_pack()
        pack["analysis_cohort"]["records"] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "figures"
            result = render_statistics_figures(pack, out_dir)

            self.assertEqual(result, {})
            self.assertEqual(list(out_dir.iterdir()), [])

    def test_populated_pack_writes_all_seven_pngs(self):
        pack = _make_statistics_pack()
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "figures"
            result = render_statistics_figures(pack, out_dir)

            self.assertEqual(set(result.keys()), set(_ALL_FIGURE_KEYS))
            for key in _ALL_FIGURE_KEYS:
                path = Path(result[key])
                self.assertTrue(path.exists(), f"missing figure file for {key}")
                self.assertTrue(path.is_absolute())
                with path.open("rb") as handle:
                    self.assertEqual(handle.read(4), _PNG_MAGIC)

    def test_skips_figure_with_entirely_absent_field(self):
        records = []
        n50_values = [10_000_000, 11_000_000, 9_500_000, 12_000_000, 10_500_000, 9_000_000, 13_000_000, 8_500_000]
        size_values = [500_000_000 + i * 10_000_000 for i in range(8)]
        chromosome_values = [21, 21, 22, 21, 20, 21, 21, 22]
        for i in range(8):
            record = _make_record(
                i,
                total_sequence_length_bp=size_values[i],
                contig_n50_bp=n50_values[i],
                gc_percent=None,
                chromosome_count=chromosome_values[i],
            )
            records.append(record)
        pack = _make_statistics_pack(records=records)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "figures"
            result = render_statistics_figures(pack, out_dir)

            self.assertNotIn("gc_distribution", result)
            self.assertFalse((out_dir / "gc_distribution.png").exists())
            for key in ("n50_distribution", "assembly_size_distribution", "chromosome_count_distribution", "n50_vs_assembly_size", "cohort_composition"):
                self.assertIn(key, result)


class BuildAssemblyReviewNotebookTests(unittest.TestCase):
    def _markdown_sources(self, notebook) -> list[str]:
        return [cell.source for cell in notebook.cells if cell.cell_type == "markdown"]

    def _code_sources(self, notebook) -> list[str]:
        return [cell.source for cell in notebook.cells if cell.cell_type == "code"]

    def test_populated_pack_produces_valid_notebook_with_expected_sections(self):
        pack = _make_statistics_pack()
        figures_relpaths = {key: f"figures/{key}.png" for key in _ALL_FIGURE_KEYS}
        target_metrics = {
            "n50_bp": 10_800_000,
            "assembly_size_bp": 540_000_000,
            "gc_percent": 40.1,
            "busco_complete_percent": 96.2,
            "busco_duplicated_percent": 1.5,
        }

        notebook = build_assembly_review_notebook(
            statistics_pack=pack,
            evidence_pack=_evidence_pack(),
            target_metrics=target_metrics,
            session_id="session-123",
            statistics_pack_relpath="related_species_statistics_pack.json",
            figures_relpaths=figures_relpaths,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "notebooks" / "review.ipynb"
            write_notebook(notebook, path)

            self.assertTrue(path.exists())
            read_back = nbformat.read(path, as_version=4)
            nbformat.validate(read_back)

        markdown_sources = self._markdown_sources(notebook)
        joined_markdown = "\n".join(markdown_sources)
        for expected in (
            "Target assembly summary",
            "Local NCBI snapshot provenance",
            "cohort composition",
            "percentile summary",
            "Notes and limitations",
        ):
            self.assertIn(expected, joined_markdown)

        code_sources = self._code_sources(notebook)
        self.assertTrue(code_sources, "expected at least one code cell")
        parseable = False
        for source in code_sources:
            lines = source.splitlines()
            filtered = "\n".join(line for line in lines if not line.strip().startswith("%"))
            ast.parse(filtered)
            parseable = True
        self.assertTrue(parseable)

    def test_unavailable_pack_still_produces_valid_notebook(self):
        pack = _unavailable_statistics_pack()

        notebook = build_assembly_review_notebook(
            statistics_pack=pack,
            evidence_pack={},
            target_metrics=None,
            session_id="session-456",
            statistics_pack_relpath="related_species_statistics_pack.json",
            figures_relpaths={},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.ipynb"
            write_notebook(notebook, path)
            read_back = nbformat.read(path, as_version=4)
            nbformat.validate(read_back)

        joined_markdown = "\n".join(self._markdown_sources(notebook))
        self.assertIn("unavailable", joined_markdown.lower())
        self.assertIn("target_species_not_provided", joined_markdown)

    def test_disclaimers_present_in_markdown_cells(self):
        pack = _make_statistics_pack()

        notebook = build_assembly_review_notebook(
            statistics_pack=pack,
            evidence_pack=_evidence_pack(),
            target_metrics=None,
            session_id="session-789",
            statistics_pack_relpath="related_species_statistics_pack.json",
            figures_relpaths={},
        )

        joined_markdown = "\n".join(self._markdown_sources(notebook))
        self.assertIn(NOTEBOOK_DISCLAIMER_1, joined_markdown)
        self.assertIn(NOTEBOOK_DISCLAIMER_2, joined_markdown)

    def test_code_cell_source_runs_against_synthetic_json_fixture(self):
        import json
        import matplotlib
        matplotlib.use("Agg")

        pack = _make_statistics_pack()

        notebook = build_assembly_review_notebook(
            statistics_pack=pack,
            evidence_pack=_evidence_pack(),
            target_metrics=None,
            session_id="session-run",
            statistics_pack_relpath="related_species_statistics_pack.json",
            figures_relpaths={},
        )
        code_sources = [cell.source for cell in notebook.cells if cell.cell_type == "code"]
        self.assertTrue(code_sources)
        source = code_sources[0]
        source = "\n".join(line for line in source.splitlines() if not line.strip().startswith("%"))

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "related_species_statistics_pack.json"
            json_path.write_text(json.dumps(pack), encoding="utf-8")
            import os
            cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                exec(compile(source, "<notebook-code-cell>", "exec"), {})
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
