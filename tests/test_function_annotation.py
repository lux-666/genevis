"""Tests for Phase 2 function annotation planning contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.control.ledger import Ledger
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.function_annotation.builder import build_stage


class FunctionAnnotationDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_function_annotation_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "function_annotation.candidate.interproscan": "interproscan",
            "function_annotation.candidate.eggnog_mapper": "eggnog_mapper",
            "function_annotation.candidate.diamond": "diamond",
            "function_annotation.candidate.kofamscan": "kofamscan",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "function_annotation")
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["function_annotation"])


class FunctionAnnotationStageTests(unittest.TestCase):
    def _state(self, tmpdir: str):
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "function_annotation"
        return state

    def _protein_state(
        self,
        tmpdir: str,
        *,
        protein_key: str = "proteins_fasta_ref",
        provenance: list[dict] | None = None,
        create_proteins: bool = True,
    ):
        state = self._state(tmpdir)
        proteins_ref = str(Path(tmpdir) / "proteins.fa")
        if create_proteins:
            Path(proteins_ref).write_text(">prot1\nMPEPTIDE\n", encoding="utf-8")
        state["stage_outputs"] = {
            "gene_annotation": {
                "status": "completed",
                protein_key: proteins_ref,
                "genes_gff3_ref": str(Path(tmpdir) / "genes.gff3"),
                "transcripts_fasta_ref": str(Path(tmpdir) / "transcripts.fa"),
            }
        }
        if provenance is not None:
            state["stage_outputs"]["function_annotation"] = {"database_provenance": provenance}
        return state

    def _provenance(self, proteins_ref: str) -> list[dict]:
        return [
            {
                "method_name": "function_annotation.candidate.interproscan",
                "database_name": "InterPro",
                "database_version": "99.0",
                "snapshot_date": "2026-05-05",
                "database_source_ref": "/db/interpro/99.0",
                "descriptor_version": "interproscan.yaml",
                "input_proteins_ref": proteins_ref,
            },
            {
                "method_name": "function_annotation.candidate.kofamscan",
                "database_name": "KOfam",
                "database_version": "2026-04",
                "snapshot_date": "2026-05-05",
                "database_source_ref": "/db/kofam/2026-04",
                "command": "exec_annotation --version",
                "input_proteins_ref": proteins_ref,
            },
        ]

    def test_missing_protein_input_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            state["stage_outputs"] = {"gene_annotation": {"status": "completed"}}

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "function_annotation")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("proteins.fa", result["last_error"])

    def test_missing_database_provenance_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = build_stage().invoke(self._protein_state(tmpdir))

            self.assertEqual(result["current_stage"], "function_annotation")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("database_provenance", result["last_error"])

    def test_empty_database_provenance_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = build_stage().invoke(self._protein_state(tmpdir, provenance=[]))

            self.assertEqual(result["current_stage"], "function_annotation")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("non-empty list", result["last_error"])

    def test_incomplete_database_provenance_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins_ref = str(Path(tmpdir) / "proteins.fa")
            state = self._protein_state(
                tmpdir,
                provenance=[
                    {
                        "method_name": "function_annotation.candidate.interproscan",
                        "database_name": "InterPro",
                        "snapshot_date": "2026-05-05",
                        "database_source_ref": "/db/interpro/99.0",
                        "descriptor_version": "interproscan.yaml",
                        "input_proteins_ref": proteins_ref,
                    }
                ],
            )

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("database_version", result["last_error"])

    def test_provenance_requires_command_or_descriptor_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins_ref = str(Path(tmpdir) / "proteins.fa")
            state = self._protein_state(
                tmpdir,
                provenance=[
                    {
                        "method_name": "function_annotation.candidate.interproscan",
                        "database_name": "InterPro",
                        "database_version": "99.0",
                        "snapshot_date": "2026-05-05",
                        "database_source_ref": "/db/interpro/99.0",
                        "input_proteins_ref": proteins_ref,
                    }
                ],
            )

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("command or descriptor_version", result["last_error"])

    def test_provenance_input_proteins_ref_must_match_resolved_proteins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._protein_state(
                tmpdir,
                provenance=self._provenance(str(Path(tmpdir) / "other_proteins.fa")),
            )

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("input_proteins_ref", result["last_error"])

    def test_provenance_omitted_input_proteins_ref_adopts_resolved_proteins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins_ref = str(Path(tmpdir) / "proteins.fa")
            state = self._protein_state(
                tmpdir,
                provenance=[
                    {
                        "method_name": "function_annotation.candidate.interproscan",
                        "database_name": "InterPro",
                        "database_version": "99.0",
                        "snapshot_date": "2026-05-05",
                        "database_source_ref": "/db/interpro/99.0",
                        "descriptor_version": "interproscan.yaml",
                    }
                ],
            )

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "completed")
            function = result["stage_outputs"]["function_annotation"]
            self.assertEqual(function["database_provenance"][0]["input_proteins_ref"], proteins_ref)

        with tempfile.TemporaryDirectory() as tmpdir:
            proteins_ref = str(Path(tmpdir) / "proteins.fa")
            state = self._protein_state(
                tmpdir,
                provenance=self._provenance(proteins_ref),
                create_proteins=False,
            )

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("protein artifact not found", result["last_error"])

    def test_valid_provenance_commits_and_marks_workflow_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins_ref = str(Path(tmpdir) / "proteins.fa")
            result = build_stage().invoke(self._protein_state(tmpdir, provenance=self._provenance(proteins_ref)))

            self.assertEqual(result["current_stage"], "done")
            self.assertEqual(result["supervision_state"], "completed")
            self.assertIn("function_annotation_stage", result["route_trace"])
            function = result["stage_outputs"]["function_annotation"]
            self.assertEqual(function["status"], "completed")
            self.assertEqual(function["proteins_fasta_ref"], proteins_ref)
            self.assertIsNone(function["gene_function_tsv_ref"])
            self.assertIsNone(function["go_annotation_table_ref"])
            self.assertIsNone(function["kegg_annotation_table_ref"])
            self.assertIsNone(function["pfam_interpro_table_ref"])
            self.assertIsNone(function["annotated_gene_models_gff3_ref"])
            self.assertTrue(Path(function["plan_artifact_ref"]).exists())
            self.assertTrue(Path(function["bundle_artifact_ref"]).exists())
            self.assertEqual(len(function["database_provenance"]), 2)
            plan = json.loads(Path(function["plan_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertFalse(plan["metadata"]["execution_enabled"])
            self.assertFalse(plan["metadata"]["database_download_enabled"])
            bundle = json.loads(Path(function["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(bundle["database_provenance"][0]["input_proteins_ref"], proteins_ref)
            self.assertEqual(bundle["database_provenance"][0]["database_source_ref"], "/db/interpro/99.0")
            self.assertIsNone(bundle["diamond_hits_ref"])
            self.assertIsNone(bundle["gene_function_tsv_ref"])
            self.assertIsNone(bundle["go_annotation_table_ref"])
            self.assertIsNone(bundle["kegg_annotation_table_ref"])
            self.assertIsNone(bundle["pfam_interpro_table_ref"])
            self.assertFalse(bundle["metadata"]["materialized_outputs"]["go_annotation_table"])
            self.assertTrue(Path(function["final_review_report_artifact_ref"]).exists())
            events = Ledger(tmpdir).read_events("s1")
            registered_types = [
                event["artifact"]["artifact_type"]
                for event in events
                if event["event_type"] == "artifact_registered"
            ]
            self.assertIn("function_annotation.plan", registered_types)
            self.assertIn("function_annotation.bundle", registered_types)
            self.assertIn("final_report.review_decisions", registered_types)

    def test_predicted_proteins_alias_is_accepted_when_provenance_matches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins_ref = str(Path(tmpdir) / "proteins.fa")
            result = build_stage().invoke(
                self._protein_state(
                    tmpdir,
                    protein_key="predicted_proteins",
                    provenance=self._provenance(proteins_ref),
                )
            )

            self.assertEqual(result["current_stage"], "done")
            function = result["stage_outputs"]["function_annotation"]
            self.assertEqual(function["proteins_fasta_ref"], proteins_ref)


if __name__ == "__main__":
    unittest.main()
