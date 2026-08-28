"""Tests for local NCBI related-species statistics pack."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from app.reference.ncbi import build_related_species_statistics_pack

_HEADERS = [
    "Assembly Accession",
    "Current Accession",
    "Assembly Name",
    "Organism Name",
    "Organism Taxonomic ID",
    "Source Database",
    "Assembly Assembly Method",
    "Assembly Stats Total Number of Chromosomes",
    "Assembly Stats Total Sequence Length",
    "Assembly Stats Number of Contigs",
    "Assembly Stats Number of Scaffolds",
    "Assembly Stats Contig N50",
    "Assembly Stats Scaffold N50",
    "Assembly Stats GC Percent",
    "Annotation BUSCO Lineage",
    "Assembly Type",
    "Assembly Level",
]


class RelatedSpeciesStatisticsTests(unittest.TestCase):
    def _reference_dir(self, tmpdir: str) -> Path:
        root = Path(tmpdir) / "ncbi_metazoa"
        root.mkdir()
        return root

    def _write_records(
        self,
        root: Path,
        rows: list[dict[str, str]],
        lineage: dict[str, str],
        *,
        lineage_header: str = "Organism Taxonomic ID",
    ) -> None:
        with (root / "Metazoa.ncbi.tsv").open(
            "w", encoding="utf-8", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=_HEADERS, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)
        lineage_text = (
            lineage_header
            + "\n"
            + "".join(f"{taxid} {value}\n" for taxid, value in lineage.items())
        )
        (root / "Metazoa.ncbi.lineage").write_text(lineage_text, encoding="utf-8")
        (root / "Metazoa.ncbi.lineage2").write_text(lineage_text, encoding="utf-8")
        (root / "Metazoa.ncbi.json").write_text("{}\n", encoding="utf-8")

    def _row(
        self,
        accession: str,
        organism: str,
        taxid: str,
        *,
        assembly: str = "primary",
        scaffold_n50: str = "100",
        contig_n50: str = "10",
        total_length: str = "600",
        gc_percent: str = "41",
        chromosome_count: str = "21",
        assembly_level: str = "Chromosome",
    ) -> dict[str, str]:
        return {
            "Assembly Accession": accession,
            "Current Accession": accession,
            "Assembly Name": assembly,
            "Organism Name": organism,
            "Organism Taxonomic ID": taxid,
            "Source Database": "SOURCE_DATABASE_GENBANK",
            "Assembly Assembly Method": "assembler",
            "Assembly Stats Total Number of Chromosomes": chromosome_count,
            "Assembly Stats Total Sequence Length": total_length,
            "Assembly Stats Number of Contigs": "20",
            "Assembly Stats Number of Scaffolds": "10",
            "Assembly Stats Contig N50": contig_n50,
            "Assembly Stats Scaffold N50": scaffold_n50,
            "Assembly Stats GC Percent": gc_percent,
            "Annotation BUSCO Lineage": "actinopterygii_odb10",
            "Assembly Level": assembly_level,
        }

    def test_four_file_set_missing_is_unavailable_with_empty_cohorts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "ncbi_metazoa"
            root.mkdir()

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(pack["status"], "unavailable")
            self.assertEqual(pack["display_cohort"]["records"], [])
            self.assertEqual(pack["analysis_cohort"]["records"], [])
            self.assertTrue(
                any(
                    note.startswith("reference_four_file_set_invalid")
                    for note in pack["notes"]
                )
            )
            self.assertEqual(
                pack["tier_counts"],
                {"exact": 0, "same_genus": 0, "same_family": 0, "same_order": 0},
            )

    def test_json_lines_source_is_not_parsed_as_one_json_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [self._row("GCA_000000001.1", "Targetus absentus", "1")],
                {"1": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus absentus"},
            )
            (root / "Metazoa.ncbi.json").write_text("{}\n{}\n", encoding="utf-8")

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(pack["status"], "available")

    def test_alias_resolution_and_cross_tier_collection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row("GCA_000000001.1", "Targetus absentus", "1"),
                    self._row("GCA_000000002.1", "Targetus relative", "2"),
                    self._row("GCA_000000003.1", "Otherus familymember", "3"),
                    self._row("GCA_000000004.1", "Yetanother ordermember", "4"),
                ],
                {
                    "1": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus absentus",
                    "2": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus relative",
                    "3": (
                        "Metazoa;Targetiformes;Targetidae;Otherus;Otherus familymember"
                    ),
                    "4": (
                        "Metazoa;Targetiformes;Otheridae;"
                        "Yetanother;Yetanother ordermember"
                    ),
                },
            )

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(pack["status"], "available")
            self.assertEqual(pack["target"]["resolution"], "scientific_name_input")
            self.assertEqual(
                pack["tier_counts"],
                {"exact": 1, "same_genus": 1, "same_family": 1, "same_order": 1},
            )
            taxids_by_tier = {
                r["tier"]: r["taxid"] for r in pack["analysis_cohort"]["records"]
            }
            self.assertEqual(
                taxids_by_tier,
                {
                    "exact": "1",
                    "same_genus": "2",
                    "same_family": "3",
                    "same_order": "4",
                },
            )

    def test_single_field_header_preserves_cross_tier_collection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row("GCA_000000001.1", "Targetus absentus", "1"),
                    self._row("GCA_000000002.1", "Targetus relative", "2"),
                    self._row("GCA_000000003.1", "Otherus familymember", "3"),
                    self._row("GCA_000000004.1", "Yetanother ordermember", "4"),
                ],
                {
                    "1": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus absentus",
                    "2": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus relative",
                    "3": (
                        "Metazoa;Targetiformes;Targetidae;Otherus;Otherus familymember"
                    ),
                    "4": (
                        "Metazoa;Targetiformes;Otheridae;Yetanother;"
                        "Yetanother ordermember"
                    ),
                },
            )

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(
                pack["tier_counts"],
                {"exact": 1, "same_genus": 1, "same_family": 1, "same_order": 1},
            )

    def test_duplicate_taxid_uses_last_lineage_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [self._row("GCA_000000003.1", "Otherus familymember", "3")],
                {"3": "Metazoa;Otheridae;Otherus"},
            )
            (root / "Metazoa.ncbi.lineage").write_text(
                "Organism Taxonomic ID\n"
                "3 Metazoa;Targetiformes;Targetidae;Otherus\n"
                "3 Metazoa;Otheridae;Otherus\n",
                encoding="utf-8",
            )

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(pack["tier_counts"]["same_family"], 0)
            self.assertEqual(pack["tier_counts"]["same_order"], 0)

    def test_malformed_lineage_headers_are_unreadable(self):
        malformed_headers = {
            "empty_file": ("", "reference_source_empty:Metazoa.ncbi.lineage"),
            "wrong_first_header": (
                "Wrong Taxonomic ID\n",
                "reference_source_unreadable:ValueError",
            ),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [self._row("GCA_000000003.1", "Otherus familymember", "3")],
                {"3": "Metazoa;Targetiformes;Targetidae;Otherus"},
            )
            for label, (contents, expected_note) in malformed_headers.items():
                with self.subTest(header=label):
                    (root / "Metazoa.ncbi.lineage").write_text(
                        contents, encoding="utf-8"
                    )
                    pack = build_related_species_statistics_pack(
                        "Targetus absentus", reference_dir=root
                    )

                    self.assertEqual(pack["status"], "unavailable")
                    self.assertIn(expected_note, pack["notes"])

    def test_taxid_priority_dedup_across_tiers(self):
        # Taxid 2 organism name also matches genus criterion; it must be assigned to
        # the most specific tier (exact-equivalent handling doesn't apply here since
        # organism name differs, so it lands in same_genus) and not reappear in
        # same_family/same_order even though its lineage also matches those terms.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row("GCA_000000002.1", "Targetus relative", "2"),
                ],
                {
                    "2": "Metazoa;Targetiformes;Targetidae;Targetus;Targetus relative",
                },
            )

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(
                pack["tier_counts"],
                {"exact": 0, "same_genus": 1, "same_family": 0, "same_order": 0},
            )
            tiers_seen = {r["tier"] for r in pack["analysis_cohort"]["records"]}
            self.assertEqual(tiers_seen, {"same_genus"})
            taxids = [r["taxid"] for r in pack["analysis_cohort"]["records"]]
            self.assertEqual(taxids.count("2"), 1)

    def test_alternate_haplotype_exclusion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            self._write_records(
                root,
                [
                    self._row(
                        "GCA_000000011.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia_aenea.primary",
                    ),
                    self._row(
                        "GCA_000000012.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia_aenea.hap1",
                    ),
                    self._row(
                        "GCA_000000013.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia_aenea_hap2",
                    ),
                    self._row(
                        "GCA_000000014.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia aenea maternal haplotype",
                    ),
                    self._row(
                        "GCA_000000015.1",
                        "Abia aenea",
                        "11",
                        assembly="Abia aenea paternal haplotype",
                    ),
                    {
                        **self._row(
                            "GCA_000000016.1",
                            "Abia aenea",
                            "11",
                            assembly="Abia_aenea.clean_name",
                        ),
                        "Assembly Type": "alternate-pseudohaplotype",
                    },
                ],
                {"11": "Metazoa;Hymenoptera;Tenthredinidae;Abia;Abia aenea"},
            )

            pack = build_related_species_statistics_pack(
                "Abia aenea", reference_dir=root
            )

            self.assertEqual(
                [r["accession"] for r in pack["analysis_cohort"]["records"]],
                ["GCA_000000011.1"],
            )
            self.assertEqual(
                {
                    r["accession"]
                    for r in pack["analysis_cohort"][
                        "excluded_alternate_haplotype_records"
                    ]
                },
                {
                    "GCA_000000012.1",
                    "GCA_000000013.1",
                    "GCA_000000014.1",
                    "GCA_000000015.1",
                    "GCA_000000016.1",
                },
            )

    def test_display_cohort_truncates_to_25_analysis_holds_more(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            rows = [
                self._row(f"GCA_0000000{i:02d}.1", "Targetus absentus", str(i))
                for i in range(1, 31)
            ]
            lineage = {
                str(i): "Metazoa;Targetiformes;Targetidae;Targetus;Targetus absentus"
                for i in range(1, 31)
            }
            self._write_records(root, rows, lineage)

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            self.assertEqual(len(pack["display_cohort"]["records"]), 25)
            self.assertEqual(len(pack["analysis_cohort"]["records"]), 30)

    def test_computed_summary_quartiles_mode_and_composition(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            rows = [
                self._row(
                    "GCA_000000001.1",
                    "Targetus absentus",
                    "1",
                    total_length="100",
                    chromosome_count="10",
                ),
                self._row(
                    "GCA_000000002.1",
                    "Targetus absentus2",
                    "2",
                    total_length="200",
                    chromosome_count="10",
                ),
                self._row(
                    "GCA_000000003.1",
                    "Targetus absentus3",
                    "3",
                    total_length="300",
                    chromosome_count="12",
                ),
                self._row(
                    "GCF_000000004.1",
                    "Targetus absentus4",
                    "4",
                    total_length="400",
                    chromosome_count="12",
                ),
            ]
            lineage = {
                str(i): "Metazoa;Targetiformes;Targetidae;Targetus;Targetus absentusX"
                for i in range(1, 5)
            }
            self._write_records(root, rows, lineage)

            pack = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )

            values = [100, 200, 300, 400]
            expected_q1, expected_median, expected_q3 = self._hand_quantiles(values)
            summary = pack["computed_summary"]["metrics"]["total_sequence_length_bp"]
            self.assertEqual(summary["min"], 100)
            self.assertEqual(summary["max"], 400)
            self.assertAlmostEqual(summary["q1"], expected_q1, places=2)
            self.assertAlmostEqual(summary["median"], expected_median, places=2)
            self.assertAlmostEqual(summary["q3"], expected_q3, places=2)

            # chromosome counts: 10, 10, 12, 12 -> tie -> pick smaller value (10)
            self.assertEqual(pack["computed_summary"]["chromosome_count_mode"], 10)

            composition = pack["computed_summary"]["cohort_composition"]
            self.assertEqual(composition["by_accession_type"], {"GCF": 1, "GCA": 3})
            self.assertEqual(composition["by_assembly_level"]["chromosome"], 4)
            self.assertEqual(sum(composition["by_tier"].values()), 4)

    def _hand_quantiles(self, values: list[float]) -> tuple[float, float, float]:
        import statistics

        return tuple(statistics.quantiles(sorted(values), n=4, method="inclusive"))

    def test_target_position_percentiles_and_unavailable_without_target_metrics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._reference_dir(tmpdir)
            rows = [
                self._row(
                    "GCA_000000001.1",
                    "Targetus absentus",
                    "1",
                    scaffold_n50="10",
                    contig_n50="10",
                ),
                self._row(
                    "GCA_000000002.1",
                    "Targetus absentus2",
                    "2",
                    scaffold_n50="20",
                    contig_n50="20",
                ),
                self._row(
                    "GCA_000000003.1",
                    "Targetus absentus3",
                    "3",
                    scaffold_n50="30",
                    contig_n50="30",
                ),
                self._row(
                    "GCA_000000004.1",
                    "Targetus absentus4",
                    "4",
                    scaffold_n50="40",
                    contig_n50="40",
                ),
            ]
            lineage = {
                str(i): "Metazoa;Targetiformes;Targetidae;Targetus;Targetus absentusX"
                for i in range(1, 5)
            }
            self._write_records(root, rows, lineage)

            pack_no_metrics = build_related_species_statistics_pack(
                "Targetus absentus", reference_dir=root
            )
            self.assertFalse(pack_no_metrics["target_position"]["available"])
            self.assertIn(
                "target_metrics_not_provided",
                pack_no_metrics["target_position"]["notes"],
            )

            # target n50 of 25 -> 2 values below (10, 20), 0 equal, out of 4 -> 50.0
            pack = build_related_species_statistics_pack(
                "Targetus absentus",
                reference_dir=root,
                target_metrics={"n50_bp": 25},
            )
            self.assertTrue(pack["target_position"]["available"])
            self.assertEqual(pack["target_position"]["n50_percentile"], 50.0)
            self.assertIsNone(pack["target_position"]["gc_percentile"])
            self.assertTrue(
                any(
                    note.startswith("gc_percentile_unavailable:no_target_gc_percent")
                    for note in pack["target_position"]["notes"]
                )
            )


if __name__ == "__main__":
    unittest.main()
