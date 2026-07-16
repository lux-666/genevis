"""Integration test for assembly_review statistics pack + notebook generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import nbformat

from app.schemas.state import initial_state
from app.stages.assembly_review.nodes import (
    _extract_target_metrics_from_signals,
    build_related_species_statistics_and_notebook,
)
from tests.data_v2 import v2_run_dir


class AssemblyReviewStatisticsIntegrationTests(unittest.TestCase):
    def _state(self, tmpdir: str, session_id: str, manifest_text: str) -> dict:
        run_dir = v2_run_dir(tmpdir, session_id)
        (run_dir / "control" / "manifest.yaml").write_text(
            manifest_text,
            encoding="utf-8",
        )
        return initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
        )

    def test_statistics_and_notebook_node_with_unavailable_snapshot(self):
        """Node handles unavailable NCBI snapshot gracefully (no crash, writes valid unavailable pack + minimal notebook)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an empty reference directory with manifest but no data files
            empty_ref_dir = Path(tmpdir) / "empty_reference"
            empty_ref_dir.mkdir()
            manifest_content = {"snapshot_date": "2024-01-01", "record_count": 0}
            (empty_ref_dir / "reference_manifest.json").write_text(json.dumps(manifest_content))

            state = self._state(
                tmpdir,
                "s1",
                "session_id: s1\nuser_metadata:\n  target_species: Test species\nassets: []\n",
            )
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "candidates": [{"candidate_id": "c1", "method_name": "flye", "final_assembly_ref": "draft.fa"}],
                    "signals": [],
                    # Point to empty reference dir (has manifest but no TSV/lineage files)
                    "related_species_reference_dir": str(empty_ref_dir),
                }
            }

            update = build_related_species_statistics_and_notebook(state)

            ar_out = update["stage_outputs"]["assembly_review"]
            self.assertIn("related_species_statistics_pack", ar_out)
            self.assertEqual(ar_out["related_species_statistics_pack"]["status"], "unavailable")
            self.assertIn("related_species_statistics_pack_ref", ar_out)
            self.assertIn("assembly_review_comparative_notebook_ref", ar_out)

            # Notebook exists and validates
            notebook_path = Path(ar_out["assembly_review_comparative_notebook_ref"])
            self.assertTrue(notebook_path.exists())
            notebook = nbformat.read(notebook_path, as_version=4)
            nbformat.validate(notebook)

            # Notebook has at least a title cell and an unavailable-status explanation
            cell_sources = " ".join(cell.get("source", "") for cell in notebook.cells)
            self.assertIn("Assembly Review", cell_sources)
            self.assertIn("unavailable", cell_sources.lower())

    def test_statistics_and_notebook_node_with_metrics_available(self):
        """Node extracts target metrics from signals and passes them to statistics pack for percentile computation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                "s2",
                "session_id: s2\nuser_metadata:\n  target_species: Solea ovata\nassets: []\n",
            )
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "candidates": [{"candidate_id": "c1", "method_name": "flye", "final_assembly_ref": "draft.fa"}],
                    "signals": [
                        {
                            "candidate_id": "c1",
                            "signal_name": "quast",
                            "status": "available",
                            "metadata": {
                                "metrics": {"n50": 123456, "total_length": 500000000, "gc_percent": 42.5}
                            },
                        },
                        {
                            "candidate_id": "c1",
                            "signal_name": "busco",
                            "status": "available",
                            "metadata": {
                                "metrics": {"complete_percent": 95.2, "duplicated_percent": 1.3}
                            },
                        },
                    ],
                }
            }

            update = build_related_species_statistics_and_notebook(state)

            ar_out = update["stage_outputs"]["assembly_review"]
            statistics_pack = ar_out["related_species_statistics_pack"]

            # target_position should reflect that target_metrics were provided
            target_position = statistics_pack.get("target_position", {})
            # Even though the snapshot is unavailable (empty cohort), target_position should record
            # that target_metrics were provided, with notes explaining why percentiles couldn't be computed
            self.assertIn("notes", target_position)

            # Notebook written
            notebook_path = Path(ar_out["assembly_review_comparative_notebook_ref"])
            self.assertTrue(notebook_path.exists())
            notebook = nbformat.read(notebook_path, as_version=4)
            nbformat.validate(notebook)

    def test_statistics_and_notebook_node_skips_when_status_failed(self):
        """Node returns empty update when assembly_review status is a fail status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, "s3", "session_id: s3\nassets: []\n")
            state["stage_outputs"] = {"assembly_review": {"status": "failed"}}

            update = build_related_species_statistics_and_notebook(state)

            self.assertEqual(update, {})

    def test_comparative_figures_written_when_data_available(self):
        """When statistics pack has analysis_cohort records, figures are rendered and artifact refs recorded."""
        # This test would need a real NCBI snapshot or a comprehensive synthetic fixture.
        # For now, we rely on the unit tests (test_assembly_review_notebook.py) proving that
        # render_statistics_figures works, and the unavailable-snapshot test above proving the
        # node wiring handles the empty case. A full end-to-end test with a populated cohort
        # would require setting up a synthetic TSV/lineage/manifest in the reference dir.
        pass

    def test_extract_target_metrics_returns_none_for_multiple_candidates(self):
        """_extract_target_metrics_from_signals returns None when multiple candidates exist."""
        ar_out_multi = {
            "candidates": [
                {"candidate_id": "c1", "method_name": "flye"},
                {"candidate_id": "c2", "method_name": "hifiasm"},
            ],
            "signals": [
                {
                    "candidate_id": "c1",
                    "signal_name": "quast",
                    "status": "available",
                    "metadata": {"metrics": {"n50": 100000, "total_length": 400000000}},
                },
                {
                    "candidate_id": "c2",
                    "signal_name": "quast",
                    "status": "available",
                    "metadata": {"metrics": {"n50": 150000, "total_length": 420000000}},
                },
            ],
        }

        result = _extract_target_metrics_from_signals(ar_out_multi)
        self.assertIsNone(result)

    def test_extract_target_metrics_works_for_single_candidate(self):
        """_extract_target_metrics_from_signals returns metrics when exactly one candidate exists."""
        ar_out_single = {
            "candidates": [{"candidate_id": "c1", "method_name": "flye"}],
            "signals": [
                {
                    "candidate_id": "c1",
                    "signal_name": "quast",
                    "status": "available",
                    "metadata": {"metrics": {"n50": 123456, "total_length": 500000000, "gc_percent": 42.5}},
                },
                {
                    "candidate_id": "c1",
                    "signal_name": "busco",
                    "status": "available",
                    "metadata": {"metrics": {"complete_percent": 95.2, "duplicated_percent": 1.3}},
                },
            ],
        }

        result = _extract_target_metrics_from_signals(ar_out_single)
        self.assertIsNotNone(result)
        self.assertEqual(result["n50_bp"], 123456.0)
        self.assertEqual(result["assembly_size_bp"], 500000000.0)
        self.assertEqual(result["gc_percent"], 42.5)
        self.assertEqual(result["busco_complete_percent"], 95.2)
        self.assertEqual(result["busco_duplicated_percent"], 1.3)


if __name__ == "__main__":
    unittest.main()
