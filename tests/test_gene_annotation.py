"""Tests for Phase 2 gene annotation planning contracts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.control.lifecycle import resume_review_state
from app.review.base import ResumeReviewRequest
from app.review.gates import get_review_gate
from app.schemas.descriptors import DescriptorRegistry
from app.schemas.state import initial_state
from app.stages.gene_annotation.builder import build_stage
from app.stages.gene_annotation.nodes import (
    GENE_REVIEW_GATES,
    apply_candidate_evidence_review_decision,
    apply_merge_review_decision,
    build_candidate_evidence_review_payload,
    build_final_gene_set_review_payload,
    build_merge_review_payload,
    _review_triggers,
)


def _fake_gene_method_bins(tmpdir: str) -> Path:
    """Create fake miniprot and augustus binaries for testing the execution path."""
    bin_dir = Path(tmpdir) / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, gff_line in [
        ("miniprot", "ctg1\tminiprot\tgene\t1\t100\t.\t+\t.\tID=g1\n"),
        ("augustus", "ctg1\tAUGUSTUS\tgene\t1\t500\t.\t+\t.\tID=g1\n"),
    ]:
        bin_path = bin_dir / name
        if not bin_path.exists():
            bin_path.write_text(
                "#!/bin/sh\n"
                f"printf '##gff-version 3\\n{gff_line}'\n",
                encoding="utf-8",
            )
            bin_path.chmod(0o755)
    return bin_dir


def _write_intake_plan(tmpdir: str) -> str:
    path = Path(tmpdir) / "annotation_intake_plan.json"
    path.write_text(
        json.dumps(
            {
                "artifact_type": "annotation.intake_plan",
                "recommended_plan": {},
                "final_plan": {},
                "final_requested_stages": ["gene_annotation", "function_annotation"],
                "changed": False,
            }
        ),
        encoding="utf-8",
    )
    return str(path)


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
        state["requested_stages"] = ["assembly_review", "gene_annotation", "function_annotation"]
        state["stage_outputs"] = {
            "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
        }
        return state

    def _softmasked_state(self, tmpdir: str, registry_ref: str | None):
        softmasked = Path(tmpdir) / "softmasked.fa"
        repeats = Path(tmpdir) / "repeats.gff3"
        softmasked.write_text(">ctg1\nacgt\n", encoding="utf-8")
        repeats.write_text("##gff-version 3\n", encoding="utf-8")
        state = self._state(tmpdir, registry_ref)
        state["stage_outputs"]["repeat_annotation"] = {
            "status": "completed",
            "softmasked_genome_ref": str(softmasked),
            "repeat_annotation_gff3_ref": str(repeats),
        }
        bin_dir = _fake_gene_method_bins(tmpdir)
        state["stage_outputs"]["gene_annotation"] = {
            "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
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

            # Mixed evidence yields multiple candidate methods, so the method
            # planning gate is not auto-decidable and blocks for human selection.
            self.assertEqual(result["current_stage"], "gene_annotation")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertEqual(result["active_interrupt"]["gate_id"], "gene_method_planning_review")
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["evidence_profile"], "mixed_evidence")
            triggers = _review_triggers(gene)
            self.assertIn("evidence_merge_policy_required", triggers)
            self.assertIn("rna_protein_evidence_conflict_review", triggers)
            self.assertNotIn("merge_policy", gene)
            self.assertNotIn("primary_method", gene)

    def test_explicit_unmasked_fallback_uses_final_assembly_with_review_trigger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "final.fa"
            assembly.write_text(">ctg1\nACGT\n", encoding="utf-8")
            registry = self._write_registry(tmpdir, assets=[])
            state = self._state(tmpdir, str(registry))
            state["skip_reasons"] = {"repeat_annotation": "repeat_annotation_skipped_by_user"}
            state["stage_outputs"]["hic_scaffolding"] = {"final_assembly_for_annotation": str(assembly)}
            bin_dir = _fake_gene_method_bins(tmpdir)
            state["stage_outputs"]["gene_annotation"] = {
                "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
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


class GeneAnnotationReviewChainTests(unittest.TestCase):
    def _write_registry(self, tmpdir: str, *, assets: list[dict]) -> Path:
        path = Path(tmpdir) / "registry.json"
        path.write_text(json.dumps({"session_id": "s1", "assets": assets}, indent=2), encoding="utf-8")
        return path

    def _state(self, tmpdir: str, registry_ref: str | None):
        state = initial_state(
            session_id="s1",
            input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"),
            data_dir=tmpdir,
        )
        state["current_stage"] = "gene_annotation"
        state["asset_registry_ref"] = registry_ref
        state["requested_stages"] = ["assembly_review", "gene_annotation", "function_annotation"]
        state["stage_outputs"] = {
            "assembly_review": {"annotation_intake_plan_ref": _write_intake_plan(tmpdir)},
        }
        return state

    def _softmasked_state(self, tmpdir: str, registry_ref: str | None):
        softmasked = Path(tmpdir) / "softmasked.fa"
        repeats = Path(tmpdir) / "repeats.gff3"
        softmasked.write_text(">ctg1\nacgt\n", encoding="utf-8")
        repeats.write_text("##gff-version 3\n", encoding="utf-8")
        state = self._state(tmpdir, registry_ref)
        state["stage_outputs"]["repeat_annotation"] = {
            "status": "completed",
            "softmasked_genome_ref": str(softmasked),
            "repeat_annotation_gff3_ref": str(repeats),
        }
        state["stage_outputs"]["hic_scaffolding"] = {"status": "completed", "final_assembly_for_annotation": str(softmasked)}
        bin_dir = _fake_gene_method_bins(tmpdir)
        state["stage_outputs"]["gene_annotation"] = {
            "env": {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        }
        return state

    def test_enter_rejects_missing_intake_plan_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            state = self._softmasked_state(tmpdir, str(registry))
            state["stage_outputs"]["assembly_review"] = {}

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertNotIn("active_interrupt", {k: v for k, v in result.items() if v})

    def test_enter_rejects_intake_plan_ref_to_missing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            state = self._softmasked_state(tmpdir, str(registry))
            state["stage_outputs"]["assembly_review"] = {
                "annotation_intake_plan_ref": str(Path(tmpdir) / "does_not_exist.json")
            }

            result = build_stage().invoke(state)

            self.assertEqual(result["supervision_state"], "failed_terminal")

    def test_all_four_gates_resolvable(self):
        for gate_id in GENE_REVIEW_GATES:
            with self.subTest(gate_id=gate_id):
                gate = get_review_gate("gene_annotation", gate_id)
                self.assertEqual(gate.gate_id, gate_id)

    def test_planning_only_auto_chain_runs_enter_to_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            result = build_stage().invoke(self._softmasked_state(tmpdir, str(registry)))

            self.assertEqual(result["current_stage"], "function_annotation")
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["status"], "completed")
            trace = result["route_trace"]
            ordered = [node for node in trace if node in GENE_REVIEW_GATES or node == "gene_annotation_stage"]
            self.assertEqual(
                ordered,
                [
                    "gene_method_planning_review",
                    "candidate_evidence_review",
                    "merge_review",
                    "final_gene_set_review",
                    "gene_annotation_stage",
                ],
            )

    def test_strict_mode_blocks_gate1_then_resumes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            state = self._softmasked_state(tmpdir, str(registry))
            state["stage_outputs"]["gene_annotation"] = {"review_policy": {"mode": "strict"}}

            blocked = build_stage().invoke(state)

            self.assertEqual(blocked["supervision_state"], "blocked_human")
            self.assertEqual(blocked["active_interrupt"]["gate_id"], "gene_method_planning_review")

            resume_state = {**state, **blocked}
            request = ResumeReviewRequest(
                session_id="s1",
                stage="gene_annotation",
                gate_id="gene_method_planning_review",
                decision="select_methods",
                metadata={"selected_methods": ["gene_annotation.candidate.augustus"]},
            )
            update = resume_review_state(resume_state, request)
            self.assertEqual(update["stage_outputs"]["gene_annotation"]["status"], "methods_confirmed")
            self.assertEqual(update["stage_outputs"]["gene_annotation"]["confirmed_methods"], ["gene_annotation.candidate.augustus"])
            # Re-invoke the graph so routing runs method stub → downstream gates.
            merged = {**resume_state, **update, "supervision_state": "running_stage"}
            result = build_stage().invoke(merged)
            gene = result["stage_outputs"]["gene_annotation"]
            self.assertEqual(list(gene["candidate_refs"].keys()), ["gene_annotation.candidate.augustus"])

    def test_candidate_evidence_review_blocks_on_conflict_and_accepts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._softmasked_state(tmpdir, None)
            state["stage_outputs"]["gene_annotation"] = {
                "evidence_profile": "mixed_evidence",
                "available_evidence": ["rnaseq", "protein"],
                "genome_input_ref": str(Path(tmpdir) / "softmasked.fa"),
                "genome_input_source": "softmasked_genome",
                "candidate_refs": {"gene_annotation.candidate.braker3": "/tmp/braker3.gff3"},
                "candidate_gene_counts": {"gene_annotation.candidate.braker3": 0},
                "candidate_methods": ["gene_annotation.candidate.braker3"],
                "expected_outputs": {
                    "genes_gff3": str(Path(tmpdir) / "genes.gff3"),
                    "transcripts_fasta": str(Path(tmpdir) / "transcripts.fa"),
                    "predicted_proteins": str(Path(tmpdir) / "proteins.fa"),
                    "evidence_report": str(Path(tmpdir) / "evidence_report.json"),
                },
            }

            payload = build_candidate_evidence_review_payload(state)
            self.assertFalse(payload.auto_decidable)

            request = ResumeReviewRequest(
                session_id="s1",
                stage="gene_annotation",
                gate_id="candidate_evidence_review",
                decision="proceed_merge",
            )
            update = apply_candidate_evidence_review_decision(state, request)
            gene = update["stage_outputs"]["gene_annotation"]
            self.assertTrue(gene["accepted_candidate_refs"])

    def test_merge_review_blocks_on_merge_policy_and_adjusts_weights(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._softmasked_state(tmpdir, None)
            state["stage_outputs"]["gene_annotation"] = {
                "evidence_profile": "mixed_evidence",
                "available_evidence": ["rnaseq", "protein"],
                "status": "merge_ready",
                "genome_input_ref": str(Path(tmpdir) / "softmasked.fa"),
                "genome_input_source": "softmasked_genome",
                "candidate_methods": ["gene_annotation.candidate.braker3"],
                "expected_outputs": {
                    "genes_gff3": str(Path(tmpdir) / "genes.gff3"),
                    "transcripts_fasta": str(Path(tmpdir) / "transcripts.fa"),
                    "predicted_proteins": str(Path(tmpdir) / "proteins.fa"),
                    "evidence_report": str(Path(tmpdir) / "evidence_report.json"),
                },
            }

            payload = build_merge_review_payload(state)
            self.assertFalse(payload.auto_decidable)

            request = ResumeReviewRequest(
                session_id="s1",
                stage="gene_annotation",
                gate_id="merge_review",
                decision="adjust_weights",
                metadata={"merge_weights": {"ab_initio": 2, "rnaseq": 8}},
            )
            update = apply_merge_review_decision(state, request)
            gene = update["stage_outputs"]["gene_annotation"]
            self.assertEqual(gene["merge_weights"], {"ab_initio": 2, "rnaseq": 8})

    def test_final_gene_set_review_planning_only_auto_decidable_and_commits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            state = self._softmasked_state(tmpdir, str(registry))
            state["stage_outputs"]["gene_annotation"] = {
                "status": "merge_accepted",
                "evidence_profile": "no_evidence",
                "candidate_methods": ["gene_annotation.candidate.augustus"],
                "available_evidence": [],
                "genome_input_ref": str(Path(tmpdir) / "softmasked.fa"),
                "genome_input_source": "softmasked_genome",
                "expected_outputs": {
                    "genes_gff3": str(Path(tmpdir) / "genes.gff3"),
                    "transcripts_fasta": str(Path(tmpdir) / "transcripts.fa"),
                    "predicted_proteins": str(Path(tmpdir) / "proteins.fa"),
                    "evidence_report": str(Path(tmpdir) / "evidence_report.json"),
                },
            }

            payload = build_final_gene_set_review_payload(state)
            self.assertTrue(payload.auto_decidable)

            request = ResumeReviewRequest(
                session_id="s1",
                stage="gene_annotation",
                gate_id="final_gene_set_review",
                decision="accept",
                decided_by="auto_rule",
                rationale_artifact_ref="rationale.json",
            )
            from app.stages.gene_annotation.nodes import apply_final_gene_set_review_decision

            update = apply_final_gene_set_review_decision(state, request)
            self.assertEqual(update["stage_outputs"]["gene_annotation"]["status"], "review_passed")
            # Re-invoke the graph; routing sends review_passed → commit.
            merged = {**state, **update}
            result = build_stage().invoke(merged)
            self.assertEqual(result["current_stage"], "function_annotation")
            self.assertEqual(result["stage_outputs"]["gene_annotation"]["status"], "completed")

    def test_durable_block_does_not_modify_upstream_stage_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._write_registry(tmpdir, assets=[])
            state = self._softmasked_state(tmpdir, str(registry))
            state["stage_outputs"]["gene_annotation"] = {"review_policy": {"mode": "strict"}}
            repeat_before = dict(state["stage_outputs"]["repeat_annotation"])
            hic_before = dict(state["stage_outputs"]["hic_scaffolding"])

            blocked = build_stage().invoke(state)

            resume_state = {**state, **blocked}
            request = ResumeReviewRequest(
                session_id="s1",
                stage="gene_annotation",
                gate_id="gene_method_planning_review",
                decision="accept",
            )
            update = resume_review_state(resume_state, request)
            self.assertEqual(update["stage_outputs"]["repeat_annotation"], repeat_before)
            self.assertEqual(update["stage_outputs"]["hic_scaffolding"], hic_before)


if __name__ == "__main__":
    unittest.main()
