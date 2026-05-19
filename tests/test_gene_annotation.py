"""Tests for Phase 2 gene annotation planning contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.gene_annotation.builder import build_stage


class GeneAnnotationDescriptorTests(unittest.TestCase):
    def test_descriptor_registry_loads_gene_annotation_placeholders(self):
        registry = DescriptorRegistry.load(Path("descriptors/registry.yaml"))

        expected = {
            "gene_annotation.candidate.braker3": "braker3",
            "gene_annotation.candidate.maker": "maker",
            "gene_annotation.candidate.augustus": "augustus",
            "gene_annotation.candidate.miniprot": "miniprot",
            "gene_annotation.helper.hisat2": "hisat2",
            "gene_annotation.helper.stringtie": "stringtie",
            "gene_annotation.helper.isoquant": "isoquant",
            "gene_annotation.helper.gemoma": "gemoma",
        }
        for method_name, package_name in expected.items():
            self.assertIn(package_name, registry.packages)
            self.assertIn(method_name, registry.methods)
            self.assertEqual(registry.methods[method_name].stage, "gene_annotation")
            self.assertEqual(registry.methods[method_name].package_ref, package_name)
            self.assertIn(method_name, registry.by_stage["gene_annotation"])


class GeneAnnotationStageTests(unittest.TestCase):
    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(
            json.dumps({"session_id": "s1", "assets": assets}, indent=2),
            encoding="utf-8",
        )
        return path

    def _state(self, tmpdir: str, registry_ref: str | None):
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "gene_annotation"
        state["asset_registry_ref"] = registry_ref
        return state

    def _softmasked_state(self, tmpdir: str, registry_ref: str | None):
        softmasked = Path(tmpdir) / "softmasked.fa"
        repeats = Path(tmpdir) / "repeats.gff3"
        softmasked.write_text(">ctg1\nacgt\n", encoding="utf-8")
        repeats.write_text("##gff-version 3\n", encoding="utf-8")
        state = self._state(tmpdir, registry_ref)
        state["stage_outputs"] = {
            "repeat_annotation": {
                "status": "completed",
                "softmasked_genome_ref": str(softmasked),
                "repeat_annotation_gff3_ref": str(repeats),
            }
        }
        return state

    def _provided_annotation_assets(self, tmpdir: str, *, transcripts: bool = True, extra: list[dict] | None = None) -> tuple[list[dict], Path, Path, Path | None]:
        genes = Path(tmpdir) / "provided_genes.gff3"
        proteins = Path(tmpdir) / "provided_proteins.fa"
        transcript_ref = Path(tmpdir) / "provided_transcripts.fa" if transcripts else None
        genes.write_text("##gff-version 3\nctg1\tsrc\tgene\t1\t4\t.\t+\t.\tID=g1\n", encoding="utf-8")
        proteins.write_text(">p1\nMKT\n", encoding="utf-8")
        assets = [
            {"asset_id": "s1:gene_models:provided", "asset_type": "gene_models", "path": str(genes)},
            {"asset_id": "s1:predicted_proteins:provided", "asset_type": "predicted_proteins", "path": str(proteins)},
        ]
        if transcript_ref:
            transcript_ref.write_text(">t1\nAUG\n", encoding="utf-8")
            assets.append({"asset_id": "s1:transcripts_fasta:provided", "asset_type": "transcripts_fasta", "path": str(transcript_ref)})
        if extra:
            assets.extend(extra)
        return assets, genes, proteins, transcript_ref

    def test_missing_softmasked_genome_without_explicit_fallback_fails_terminally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "gene_annotation")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("softmasked_genome", result["last_error"])

    def test_softmasked_genome_no_evidence_plans_ab_initio_without_default_method(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "function_annotation")
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "completed")
            self.assertEqual(gene["genome_input_source"], "softmasked_genome")
            self.assertEqual(gene["evidence_profile"], "no_evidence")
            self.assertIn("gene_annotation.candidate.augustus", gene["candidate_methods"])
            self.assertNotIn("primary_method", gene)
            self.assertTrue(gene["method_selection_required"])
            self.assertIn("species_or_training_policy_required", gene["review_triggers"])
            self.assertTrue(gene["predicted_proteins_ref"].endswith("proteins.fa"))
            self.assertTrue(Path(gene["plan_artifact_ref"]).exists())
            self.assertTrue(Path(gene["bundle_artifact_ref"]).exists())
            plan = json.loads(Path(gene["plan_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertFalse(plan["metadata"]["execution_enabled"])

    def test_provided_gene_annotation_adopts_real_artifacts_without_planned_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assets, genes, proteins, transcripts = self._provided_annotation_assets(tmpdir)
            registry = self._write_registry(tmpdir, assets=assets)
            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "function_annotation")
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "completed")
            self.assertEqual(gene["bundle_status"], "adopted_existing_annotation")
            self.assertEqual(gene["genes_gff3_ref"], str(genes))
            self.assertEqual(gene["gene_models"], str(genes))
            self.assertEqual(gene["predicted_proteins_ref"], str(proteins))
            self.assertEqual(gene["predicted_proteins"], str(proteins))
            self.assertEqual(gene["proteins_fasta_ref"], str(proteins))
            self.assertEqual(gene["transcripts_fasta_ref"], str(transcripts))
            self.assertFalse(gene["method_selection_required"])
            self.assertEqual(gene["candidate_methods"], [])
            self.assertNotIn("expected_outputs", gene)
            bundle = json.loads(Path(gene["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertEqual(bundle["status"], "adopted_existing_annotation")
            self.assertEqual(bundle["predicted_proteins_ref"], str(proteins))
            artifact_types = [
                json.loads(line)["artifact_type"]
                for line in (Path(tmpdir) / "artifacts" / "s1" / "artifacts.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("gene_annotation.provided_gene_models", artifact_types)
            self.assertIn("gene_annotation.provided_predicted_proteins", artifact_types)
            self.assertIn("gene_annotation.provided_transcripts_fasta", artifact_types)

    def test_provided_gene_annotation_without_transcripts_is_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assets, _genes, proteins, _transcripts = self._provided_annotation_assets(tmpdir, transcripts=False)
            registry = self._write_registry(tmpdir, assets=assets)
            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["bundle_status"], "adopted_existing_annotation")
            self.assertIsNone(gene.get("transcripts_fasta_ref"))
            self.assertEqual(gene["proteins_fasta_ref"], str(proteins))
            bundle = json.loads(Path(gene["bundle_artifact_ref"]).read_text(encoding="utf-8"))
            self.assertIsNone(bundle["transcripts_fasta_ref"])

    def test_incomplete_provided_gene_annotation_assets_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            genes = Path(tmpdir) / "provided_genes.gff3"
            proteins = Path(tmpdir) / "provided_proteins.fa"
            transcripts = Path(tmpdir) / "provided_transcripts.fa"
            genes.write_text("##gff-version 3\n", encoding="utf-8")
            proteins.write_text(">p1\nMKT\n", encoding="utf-8")
            transcripts.write_text(">t1\nAUG\n", encoding="utf-8")
            cases = [
                ([{"asset_id": "genes", "asset_type": "gene_models", "path": str(genes)}], "predicted_proteins"),
                ([{"asset_id": "proteins", "asset_type": "predicted_proteins", "path": str(proteins)}], "gene_models"),
                ([{"asset_id": "transcripts", "asset_type": "transcripts_fasta", "path": str(transcripts)}], "gene_models"),
            ]
            for assets, expected_missing in cases:
                with self.subTest(expected_missing=expected_missing):
                    registry = self._write_registry(tmpdir, assets=assets)
                    result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))
                    self.assertEqual(result["supervision_state"], "failed_terminal")
                    self.assertIn(expected_missing, result["last_error"])

    def test_provided_gene_annotation_with_extra_evidence_records_review_trigger_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna.fq"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            assets, _genes, _proteins, _transcripts = self._provided_annotation_assets(
                tmpdir,
                extra=[{"asset_id": "s1:raw_rnaseq_reads:rna", "asset_type": "raw_rnaseq_reads", "path": str(rnaseq)}],
            )
            registry = self._write_registry(tmpdir, assets=assets)
            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertTrue(gene["review_required"])
            self.assertIn("provided_annotation_with_additional_evidence_review", gene["review_triggers"])
            self.assertNotIn("merge_policy", gene)
            self.assertNotIn("primary_method", gene)

    def test_rnaseq_evidence_adds_hisat2_stringtie_and_evidence_aware_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna_R1.fq.gz"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:clean_rnaseq_reads:rna", "asset_type": "clean_rnaseq_reads", "path": str(rnaseq)}],
            )

            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["evidence_profile"], "rnaseq")
            self.assertIn("gene_annotation.helper.hisat2", gene["helper_methods"])
            self.assertIn("gene_annotation.helper.stringtie", gene["helper_methods"])
            self.assertIn("gene_annotation.candidate.braker3", gene["candidate_methods"])
            self.assertIn("gene_annotation.candidate.maker", gene["candidate_methods"])
            self.assertIn("gene_annotation.candidate.augustus", gene["candidate_methods"])

    def test_long_rna_evidence_adds_isoquant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            long_rna = Path(tmpdir) / "isoseq.fq.gz"
            long_rna.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:clean_isoseq_reads:isoseq", "asset_type": "clean_isoseq_reads", "path": str(long_rna)}],
            )

            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["evidence_profile"], "long_rna")
            self.assertIn("gene_annotation.helper.isoquant", gene["helper_methods"])
            self.assertIn("gene_annotation.candidate.braker3", gene["candidate_methods"])

    def test_genome_long_reads_do_not_enable_isoquant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            long_reads = Path(tmpdir) / "cycloneseq.fastq.gz"
            long_reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {
                        "asset_id": "s1:raw_long_reads:cycloneseq",
                        "asset_type": "raw_long_reads",
                        "path": str(long_reads),
                        "platform": "cycloneseq",
                    }
                ],
            )

            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["evidence_profile"], "no_evidence")
            self.assertNotIn("gene_annotation.helper.isoquant", gene["helper_methods"])

    def test_protein_evidence_adds_miniprot_and_gemoma(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proteins = Path(tmpdir) / "ref_proteins.fa"
            proteins.write_text(">p1\nMKK\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[{"asset_id": "s1:reference_proteins:ref", "asset_type": "reference_proteins", "path": str(proteins)}],
            )

            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["evidence_profile"], "protein")
            self.assertIn("gene_annotation.candidate.miniprot", gene["candidate_methods"])
            self.assertIn("gene_annotation.helper.gemoma", gene["helper_methods"])

    def test_mixed_evidence_records_review_triggers_without_inventing_merge_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rnaseq = Path(tmpdir) / "rna.fq.gz"
            proteins = Path(tmpdir) / "ref_proteins.fa"
            rnaseq.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            proteins.write_text(">p1\nMKK\n", encoding="utf-8")
            registry = self._write_registry(
                tmpdir,
                assets=[
                    {"asset_id": "s1:raw_rnaseq_reads:rna", "asset_type": "raw_rnaseq_reads", "path": str(rnaseq)},
                    {"asset_id": "s1:reference_proteins:ref", "asset_type": "reference_proteins", "path": str(proteins)},
                ],
            )

            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "function_annotation")
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["evidence_profile"], "mixed_evidence")
            self.assertTrue(gene["review_required"])
            self.assertIn("evidence_merge_policy_required", gene["review_triggers"])
            self.assertIn("rna_protein_evidence_conflict_review", gene["review_triggers"])
            self.assertNotIn("merge_policy", gene)
            self.assertNotIn("primary_method", gene)

    def test_explicit_unmasked_fallback_uses_final_assembly_with_review_trigger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["skip_reasons"] = {"repeat_annotation": "repeat_annotation_skipped_by_user"}
            state["stage_outputs"] = {
                "hic_scaffolding": {"final_assembly_for_annotation": str(assembly)}
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "function_annotation")
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["genome_input_source"], "final_assembly_for_annotation")
            self.assertEqual(gene["unmasked_fallback_reason"], "repeat_annotation_skipped_by_user")
            self.assertIn("unmasked_genome_fallback_requires_review", gene["review_triggers"])

    def test_gene_annotation_routes_to_function_annotation_and_publishes_proteins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "function_annotation")
            self.assertIn("gene_annotation_stage", result["route_trace"])
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["predicted_proteins"], gene["predicted_proteins_ref"])
            self.assertEqual(gene["proteins_fasta_ref"], gene["predicted_proteins_ref"])
            self.assertEqual(gene["gene_models"], gene["genes_gff3_ref"])


if __name__ == "__main__":
    unittest.main()
