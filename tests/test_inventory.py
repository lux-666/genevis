"""Tests for the inventory stage: manifest loading, validation, asset registration."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from app.review.base import ResumeReviewRequest
from app.review.persistence import load_review_request
from app.schemas.state import initial_state
from app.stages.inventory.builder import build_stage
from app.stages.inventory.nodes import apply_inventory_resume_decision
from app.storage.artifacts import resolve_artifact_ref
from tests.data_v2 import v2_run_dir


def _registry_path(state: dict) -> Path:
    return resolve_artifact_ref(
        state["data_dir"],
        state["session_id"],
        state["asset_registry_ref"],
    )


class InventoryStageTests(unittest.TestCase):
    def _build_manifest_file(
        self,
        tmpdir: str,
        manifest: dict,
        *,
        session_id: str | None = None,
    ) -> Path:
        run_dir = v2_run_dir(tmpdir, session_id or manifest["session_id"])
        path = run_dir / "control" / "manifest.yaml"
        path.write_text(yaml.dump(manifest), encoding="utf-8")
        return path

    @staticmethod
    def _state(session_id: str, manifest_path: Path):
        return initial_state(
            session_id=session_id,
            input_manifest_ref="control/manifest.yaml",
            data_dir=str(manifest_path.parents[1]),
        )

    def test_inventory_fails_without_manifest_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = initial_state(
                session_id="test",
                input_manifest_ref="",
                data_dir=str(v2_run_dir(tmpdir, "test")),
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "failed_terminal")
            inventory_out = result["stage_outputs"]["inventory"]
            self.assertEqual(inventory_out["status"], "failed")

    def test_inventory_fails_with_missing_manifest_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"session_id": "s1", "assets": []}
            path = self._build_manifest_file(tmpdir, manifest, session_id="test")
            path.unlink()
            state = self._state("test", path)
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "failed_terminal")

    def test_inventory_fails_before_read_qc_when_asset_path_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.fastq"
            manifest = {
                "session_id": "s1",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(missing)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s1", path)
            graph = build_stage()
            result = graph.invoke(state)

            self.assertEqual(result["current_stage"], "inventory")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIsNone(result.get("asset_registry_ref"))
            self.assertEqual(
                result["stage_outputs"]["inventory"]["status"], "validated"
            )
            self.assertFalse(result["stage_outputs"]["inventory"]["valid"])
            self.assertIn("inventory validation failed", result["last_error"])
            self.assertIn("raw_long_reads: missing files", result["last_error"])
            self.assertIn(str(missing), result["last_error"])

    def test_inventory_succeeds_with_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "ont.fq"
            fq.write_text("@r1\nACGT\n+\n!!!!\n")
            manifest = {
                "session_id": "s1",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(fq)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s1", path)
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "read_qc")
            self.assertIn("asset_registry_ref", result)
            self.assertIsNotNone(result["asset_registry_ref"])
            registry_path = _registry_path(result)
            self.assertTrue(registry_path.exists())
            registry = json.loads(registry_path.read_text())
            self.assertEqual(registry["session_id"], "s1")
            self.assertEqual(len(registry["assets"]), 1)
            asset = registry["assets"][0]
            self.assertEqual(
                asset["provenance"]["manifest_ref"], "control/manifest.yaml"
            )
            self.assertEqual(
                asset["provenance"]["declarations"][0]["declared_path"],
                str(fq),
            )
            self.assertNotIn("manifest", result["stage_outputs"]["inventory"])

    def test_inventory_does_not_block_for_unknown_long_read_platform(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "_250F701999011_barcode10_01.fastq.gz"
            fq.write_bytes(gzip.compress(b"@r1\nACGT\n+\n!!!!\n"))
            manifest = {
                "session_id": "unknown-platform",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(fq)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("unknown-platform", path)

            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "read_qc")
            self.assertEqual(result["supervision_state"], "running_stage")
            self.assertIsNone(result.get("active_interrupt"))
            registry = json.loads(_registry_path(result).read_text())
            self.assertEqual(registry["assets"][0]["platform"], "unknown")
            warnings = result["stage_outputs"]["inventory"]["warnings"]
            self.assertTrue(any("platform unknown" in warning for warning in warnings))

    def test_inventory_shared_platform_overrides_registered_raw_long_read_platform(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "_250F701999011_barcode10_01.fastq.gz"
            fq.write_bytes(gzip.compress(b"@r1\nACGT\n+\n!!!!\n"))
            manifest = {
                "session_id": "override-platform",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(fq)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("override-platform", path)
            state["shared_parameters"] = {"platform": "ont"}

            result = build_stage().invoke(state)

            registry = json.loads(_registry_path(result).read_text())
            self.assertEqual(registry["assets"][0]["platform"], "ont")
            self.assertFalse(
                any(
                    "platform unknown" in warning
                    for warning in result["stage_outputs"]["inventory"]["warnings"]
                )
            )

    def test_inventory_registers_cycloneseq_fastq_as_raw_long_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "BGI_CycloneSEQ_sample.fastq.gz"

            fq.write_bytes(gzip.compress(b"@r1\nACGT\n+\n!!!!\n"))
            manifest = {
                "session_id": "cyclone-s1",
                "assets": [
                    {
                        "asset_type": "raw_long_reads",
                        "paths": [str(fq)],
                        "platform": "cycloneseq",
                        "metadata": {"technology": "BGI CycloneSEQ"},
                    },
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("cyclone-s1", path)
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "read_qc")
            registry = json.loads(_registry_path(result).read_text())
            asset = registry["assets"][0]
            self.assertEqual(asset["asset_type"], "raw_long_reads")
            self.assertEqual(asset["platform"], "cycloneseq")
            self.assertEqual(asset["format"], "fastq")
            self.assertEqual(asset["metadata"]["compression"], "gzip")

    def test_inventory_registers_multiple_assets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ont = Path(tmpdir) / "ont.fq"
            ont.write_text("@r1\nACGT\n+\n!!!!\n")
            short1 = Path(tmpdir) / "R1.fq"
            short2 = Path(tmpdir) / "R2.fq"
            short1.write_text("@s1\nACGT\n+\n!!!!\n")
            short2.write_text("@s2\nTGCA\n+\n!!!!\n")
            manifest = {
                "session_id": "s2",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(ont)]},
                    {
                        "asset_type": "raw_short_dna_reads",
                        "paths": [str(short1), str(short2)],
                    },
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s2", path)
            graph = build_stage()
            result = graph.invoke(state)
            registry = json.loads(_registry_path(result).read_text())
            self.assertEqual(len(registry["assets"]), 3)
            asset_types = {a["asset_type"] for a in registry["assets"]}
            self.assertEqual(asset_types, {"raw_long_reads", "raw_short_dna_reads"})

    def test_inventory_triggers_review_for_ambiguous_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            genomic = Path(tmpdir) / "a.fq"
            transcript = Path(tmpdir) / "b.fq"
            genomic.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            transcript.write_text("@r2\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = {
                "session_id": "s3",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(genomic)]},
                    {
                        "asset_type": "raw_isoseq_or_long_rna_reads",
                        "paths": [str(transcript)],
                    },
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s3", path)
            graph = build_stage()
            result = graph.invoke(state)
            self.assertIsNotNone(result.get("active_interrupt"))
            self.assertEqual(result["active_interrupt"]["stage"], "inventory")
            self.assertEqual(result["active_interrupt"]["gate_id"], "inventory_review")
            request = load_review_request(
                result["data_dir"], result["active_interrupt"]["request_ref"]
            )
            self.assertIn("continue", request["payload"]["decisions_allowed"])
            self.assertEqual(result["current_stage"], "inventory")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNone(result.get("asset_registry_ref"))

    def test_inventory_review_continue_registers_assets_and_advances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            genomic = Path(tmpdir) / "a.fq"
            transcript = Path(tmpdir) / "b.fq"
            genomic.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
            transcript.write_text("@r2\nACGT\n+\n!!!!\n", encoding="utf-8")
            manifest = {
                "session_id": "s3-continue",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(genomic)]},
                    {
                        "asset_type": "raw_isoseq_or_long_rna_reads",
                        "paths": [str(transcript)],
                    },
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s3-continue", path)
            blocked = build_stage().invoke(state)
            decision = ResumeReviewRequest(
                session_id="s3-continue",
                stage="inventory",
                gate_id="inventory_review",
                decision="continue",
                notes=["confirmed inputs"],
                review_request_id=blocked["active_interrupt"]["review_request_id"],
                review_request_hash=blocked["active_interrupt"]["request_hash"],
            )

            update = apply_inventory_resume_decision(blocked, decision)

            self.assertIsNone(update["active_interrupt"])
            self.assertEqual(update["current_stage"], "read_qc")
            self.assertEqual(update["supervision_state"], "running_stage")
            self.assertTrue(_registry_path({**blocked, **update}).exists())

    def test_inventory_with_existing_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fa = Path(tmpdir) / "asm.fa"
            fa.write_text(">ctg1\nACGTACGT\n")
            manifest = {
                "session_id": "s4",
                "target_assembly_level": "chromosome",
                "assets": [
                    {"asset_type": "assembly_fasta_registered", "paths": [str(fa)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s4", path)
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(
                result["skip_reasons"]["read_qc"], "existing_assembly_input"
            )
            self.assertEqual(
                result["skip_reasons"]["genome_survey"], "existing_assembly_input"
            )
            self.assertEqual(
                result["skip_reasons"]["assembly"], "existing_assembly_input"
            )
            self.assertEqual(
                result["stage_outputs"]["inventory"]["target_assembly_level"],
                "chromosome",
            )
            registry = json.loads(_registry_path(result).read_text())
            self.assertEqual(
                registry["assets"][0]["asset_type"], "assembly_fasta_registered"
            )

    def test_inventory_existing_assembly_with_provided_gene_annotation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fa = Path(tmpdir) / "asm.fa"
            gff3 = Path(tmpdir) / "genes.gff3"
            proteins = Path(tmpdir) / "proteins.fa"
            transcripts = Path(tmpdir) / "transcripts.fa"
            fa.write_text(">ctg1\nACGTACGT\n")
            gff3.write_text("##gff-version 3\nctg1\tsrc\tgene\t1\t8\t.\t+\t.\tID=g1\n")
            proteins.write_text(">p1\nMKT\n")
            transcripts.write_text(">t1\nAUG\n")
            manifest = {
                "session_id": "s4-annotation",
                "assets": [
                    {"asset_type": "assembly_fasta_registered", "paths": [str(fa)]},
                    {"asset_type": "gene_models", "paths": [str(gff3)]},
                    {"asset_type": "predicted_proteins", "paths": [str(proteins)]},
                    {"asset_type": "transcripts_fasta", "paths": [str(transcripts)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s4-annotation", path)
            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "assembly_review")
            registry = json.loads(_registry_path(result).read_text())
            assets = {asset["asset_type"]: asset for asset in registry["assets"]}
            self.assertEqual(assets["gene_models"]["format"], "gff3")
            self.assertEqual(assets["predicted_proteins"]["format"], "fasta")
            self.assertEqual(assets["transcripts_fasta"]["format"], "fasta")

    def test_inventory_rejects_missing_provided_annotation_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assembly = Path(tmpdir) / "asm.fa"
            assembly.write_text(">ctg1\nACGT\n")
            missing = Path(tmpdir) / "missing_proteins.fa"
            manifest = {
                "session_id": "s4-missing-annotation",
                "assets": [
                    {
                        "asset_type": "assembly_fasta_registered",
                        "paths": [str(assembly)],
                    },
                    {"asset_type": "predicted_proteins", "paths": [str(missing)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s4-missing-annotation", path)
            result = build_stage().invoke(state)

            self.assertEqual(result["current_stage"], "inventory")
            self.assertEqual(result["supervision_state"], "failed_terminal")
            self.assertIn("predicted_proteins", result["last_error"])
            self.assertIn(str(missing), result["last_error"])

    def test_inventory_writes_ledger_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "ont.fq"
            fq.write_text("@r1\nACGT\n+\n!!!!\n")
            manifest = {
                "session_id": "s5",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": [str(fq)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = self._state("s5", path)
            graph = build_stage()
            graph.invoke(state)
            events_path = Path(state["data_dir"]) / "ledger" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            self.assertTrue(any(e["event_type"] == "assets_registered" for e in events))
            asset_event = next(
                e for e in events if e["event_type"] == "assets_registered"
            )
            self.assertEqual(asset_event["summary"]["asset_count"], 1)


if __name__ == "__main__":
    unittest.main()
