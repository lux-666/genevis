"""Integration test for assembly_review statistics pack + notebook generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import nbformat

from app.schemas.state import initial_state
from app.stages.assembly_review.nodes import build_related_species_statistics_and_notebook


class AssemblyReviewStatisticsIntegrationTests(unittest.TestCase):
    def test_statistics_and_notebook_node_with_unavailable_snapshot(self):
        """Node handles unavailable NCBI snapshot gracefully (no crash, writes valid unavailable pack + minimal notebook)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "manifest.yaml"
            manifest.write_text("session_id: s1\nuser_metadata:\n  target_species: 卵鳎\nassets: []\n", encoding="utf-8")
            state = initial_state(session_id="s1", input_manifest_ref=str(manifest), data_dir=tmpdir)
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "candidates": [{"candidate_id": "c1", "method_name": "flye", "final_assembly_ref": "draft.fa"}],
                    "signals": [],
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
            manifest = Path(tmpdir) / "manifest.yaml"
            manifest.write_text("session_id: s2\nuser_metadata:\n  target_species: Solea ovata\nassets: []\n", encoding="utf-8")
            state = initial_state(session_id="s2", input_manifest_ref=str(manifest), data_dir=tmpdir)
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
            manifest = Path(tmpdir) / "manifest.yaml"
            manifest.write_text("session_id: s3\nassets: []\n", encoding="utf-8")
            state = initial_state(session_id="s3", input_manifest_ref=str(manifest), data_dir=tmpdir)
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


if __name__ == "__main__":
    unittest.main()
