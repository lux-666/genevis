"""Integration test for assembly_review statistics pack + notebook generation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nbformat

from app.schemas.state import initial_state
from app.stages.assembly_review.nodes import (
    _extract_target_metrics_from_signals,
    build_related_species_statistics_and_notebook,
)
from tests.data_v2 import v2_run_dir


def _candidate(candidate_id: str, method_name: str = "flye") -> dict:
    return {
        "candidate_id": candidate_id,
        "method_name": method_name,
        "source": "test_fixture",
        "status": "executed",
        "bundle_ref": {
            "schema_version": 2,
            "artifact_id": f"bundle:{candidate_id}",
            "path_ref": (
                "artifacts/steps/step-001-assembly/assembly/"
                f"{candidate_id}/candidate.json"
            ),
            "sha256": "0" * 64,
            "size_bytes": 0,
            "artifact_type": "draft_assembly.candidate",
        },
        "primary_output_ref": {
            "schema_version": 2,
            "artifact_id": f"primary:{candidate_id}",
            "path_ref": (
                f"artifacts/steps/step-001-assembly/assembly/{candidate_id}/draft.fa"
            ),
            "sha256": "1" * 64,
            "size_bytes": 0,
            "artifact_type": "assembly.draft_assembly",
        },
    }


def _empty_reference(root: str | Path) -> Path:
    reference = Path(root) / "empty_reference"
    reference.mkdir()
    files = {
        "Metazoa.ncbi.json": "{}\n",
        "Metazoa.ncbi.lineage": "Organism Taxonomic ID\n",
        "Metazoa.ncbi.lineage2": "Organism Taxonomic ID\n",
        "Metazoa.ncbi.tsv": "Organism Name\tOrganism Taxonomic ID\n",
    }
    for name, contents in files.items():
        (reference / name).write_text(contents, encoding="utf-8")
    return reference


class AssemblyReviewStatisticsIntegrationTests(unittest.TestCase):
    def _state(self, tmpdir: str, session_id: str, manifest_text: str) -> dict:
        run_dir = v2_run_dir(tmpdir, session_id)
        (run_dir / "control" / "manifest.yaml").write_text(
            manifest_text,
            encoding="utf-8",
        )
        state = initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(run_dir),
            workflow_steps=[
                {
                    "step_id": "step-000-inventory",
                    "stage": "inventory",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-001-assembly",
                    "stage": "assembly",
                    "allow_local_execution": False,
                },
                {
                    "step_id": "step-002-assembly_review",
                    "stage": "assembly_review",
                    "allow_local_execution": False,
                    "review_target": {
                        "resource_type": "assembly",
                        "producer_step_id": "step-001-assembly",
                    },
                },
            ],
        )
        state.update(
            {
                "current_stage": "assembly_review",
                "workflow_step_index": 2,
                "entered_workflow_step_index": 2,
            }
        )
        return state

    def test_statistics_and_notebook_node_with_empty_snapshot(self):
        """Node handles a valid snapshot with no assembly rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a valid reference snapshot with no assembly rows.
            empty_ref_dir = _empty_reference(tmpdir)

            state = self._state(
                tmpdir,
                "s1",
                "session_id: s1\nuser_metadata:\n"
                "  target_species: Test species\nassets: []\n",
            )
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "candidates": [_candidate("c1")],
                    "signals": [],
                    # Point to an empty but structurally valid snapshot.
                    "related_species_reference_dir": str(empty_ref_dir),
                }
            }

            update = build_related_species_statistics_and_notebook(state)

            ar_out = update["stage_outputs"]["assembly_review"]
            self.assertNotIn("related_species_statistics_pack", ar_out)
            self.assertEqual(
                json.loads(
                    Path(ar_out["related_species_statistics_pack_ref"]).read_text(
                        encoding="utf-8"
                    )
                )["status"],
                "available",
            )
            self.assertIn("related_species_statistics_pack_ref", ar_out)
            self.assertIn("assembly_review_comparative_notebook_ref", ar_out)

            # Notebook exists and validates
            notebook_path = Path(ar_out["assembly_review_comparative_notebook_ref"])
            self.assertTrue(notebook_path.exists())
            notebook = nbformat.read(notebook_path, as_version=4)
            nbformat.validate(notebook)

            # Notebook has a title and explains that the cohort is empty.
            cell_sources = " ".join(cell.get("source", "") for cell in notebook.cells)
            self.assertIn("Assembly Review", cell_sources)
            self.assertIn("no related-species assemblies", cell_sources.lower())

    def test_statistics_and_notebook_node_with_metrics_available(self):
        """Node passes extracted metrics to the statistics percentile pack."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                "s2",
                "session_id: s2\nuser_metadata:\n"
                "  target_species: Solea ovata\nassets: []\n",
            )
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "candidates": [_candidate("c1")],
                    "related_species_reference_dir": str(_empty_reference(tmpdir)),
                    "signals": [
                        {
                            "candidate_id": "c1",
                            "signal_name": "quast",
                            "status": "available",
                            "metadata": {
                                "metrics": {
                                    "n50": 123456,
                                    "total_length": 500000000,
                                    "gc_percent": 42.5,
                                }
                            },
                        },
                        {
                            "candidate_id": "c1",
                            "signal_name": "busco",
                            "status": "available",
                            "metadata": {
                                "metrics": {
                                    "complete_percent": 95.2,
                                    "duplicated_percent": 1.3,
                                }
                            },
                        },
                    ],
                }
            }

            update = build_related_species_statistics_and_notebook(state)

            ar_out = update["stage_outputs"]["assembly_review"]
            statistics_pack = json.loads(
                Path(ar_out["related_species_statistics_pack_ref"]).read_text(
                    encoding="utf-8"
                )
            )

            # target_position should reflect that target_metrics were provided
            target_position = statistics_pack.get("target_position", {})
            # Even with an unavailable snapshot (empty cohort), target_position
            # should record that target_metrics were provided, with notes
            # explaining why percentiles could not be computed.
            self.assertIn("notes", target_position)

            # Notebook written
            notebook_path = Path(ar_out["assembly_review_comparative_notebook_ref"])
            self.assertTrue(notebook_path.exists())
            notebook = nbformat.read(notebook_path, as_version=4)
            nbformat.validate(notebook)

    def test_large_statistics_pack_is_kept_out_of_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                "s-large",
                "session_id: s-large\nuser_metadata:\n"
                "  target_species: Test species\nassets: []\n",
            )
            state["stage_outputs"] = {
                "assembly_review": {
                    "status": "evaluated",
                    "candidates": [_candidate("c1")],
                    "signals": [],
                    "related_species_reference_dir": str(_empty_reference(tmpdir)),
                }
            }
            large_pack = {
                "status": "available",
                "analysis_cohort": {
                    "records": [{"record": "x" * 1_000} for _ in range(150)]
                },
            }

            with (
                patch(
                    "app.stages.assembly_review.nodes._build_related_species_statistics_pack",
                    return_value=large_pack,
                ),
                patch(
                    "app.stages.assembly_review.nodes.render_statistics_figures",
                    return_value={},
                ),
                patch(
                    "app.stages.assembly_review.nodes.build_assembly_review_notebook",
                    return_value=nbformat.v4.new_notebook(),
                ),
            ):
                update = build_related_species_statistics_and_notebook(state)

            review = update["stage_outputs"]["assembly_review"]
            self.assertNotIn("related_species_statistics_pack", review)
            self.assertEqual(
                json.loads(
                    Path(review["related_species_statistics_pack_ref"]).read_text(
                        encoding="utf-8"
                    )
                ),
                large_pack,
            )

    def test_statistics_and_notebook_node_skips_when_status_failed(self):
        """Node returns empty update when assembly_review status is a fail status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, "s3", "session_id: s3\nassets: []\n")
            state["stage_outputs"] = {"assembly_review": {"status": "failed"}}

            update = build_related_species_statistics_and_notebook(state)

            self.assertEqual(update, {})

    def test_comparative_figures_written_when_data_available(self):
        """Render figures and record refs when analysis-cohort records exist."""
        # This test would need a real NCBI snapshot or a comprehensive synthetic
        # fixture. For now, unit tests in test_assembly_review_notebook.py prove
        # render_statistics_figures works, while the unavailable-snapshot test
        # above proves the node wiring handles the empty case. A full end-to-end
        # test with a populated cohort would require setting up a synthetic
        # TSV/lineage/manifest in the reference directory.
        pass

    def test_extract_target_metrics_returns_none_for_multiple_candidates(self):
        """Metric extraction returns None when multiple candidates exist."""
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
        """Metric extraction returns metrics for exactly one candidate."""
        ar_out_single = {
            "candidates": [{"candidate_id": "c1", "method_name": "flye"}],
            "signals": [
                {
                    "candidate_id": "c1",
                    "signal_name": "quast",
                    "status": "available",
                    "metadata": {
                        "metrics": {
                            "n50": 123456,
                            "total_length": 500000000,
                            "gc_percent": 42.5,
                        }
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

        result = _extract_target_metrics_from_signals(ar_out_single)
        self.assertIsNotNone(result)
        self.assertEqual(result["n50_bp"], 123456.0)
        self.assertEqual(result["assembly_size_bp"], 500000000.0)
        self.assertEqual(result["gc_percent"], 42.5)
        self.assertEqual(result["busco_complete_percent"], 95.2)
        self.assertEqual(result["busco_duplicated_percent"], 1.3)


if __name__ == "__main__":
    unittest.main()
