"""Tests for the inventory stage: manifest loading, validation, asset registration."""

from __future__ import annotations

import json
import gzip
import tempfile
import unittest
from pathlib import Path

from app.stages.inventory.builder import build_stage
from app.stages.inventory.nodes import commit_inventory
from app.schemas.state import initial_state
import yaml


class InventoryStageTests(unittest.TestCase):
    def _build_manifest_file(self, tmpdir: str, manifest: dict) -> Path:
        path = Path(tmpdir) / "manifest.yaml"
        path.write_text(yaml.dump(manifest), encoding="utf-8")
        return path

    def test_inventory_fails_without_manifest_ref(self):
        state = initial_state(session_id="test", input_manifest_ref="")
        graph = build_stage()
        result = graph.invoke(state)
        self.assertEqual(result["supervision_state"], "failed_terminal")
        inventory_out = result["stage_outputs"]["inventory"]
        self.assertEqual(inventory_out["status"], "failed")

    def test_inventory_fails_with_missing_manifest_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {"session_id": "s1", "assets": []}
            path = self._build_manifest_file(tmpdir, manifest)
            path.unlink()
            state = initial_state(
                session_id="test",
                input_manifest_ref=str(path),
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["supervision_state"], "failed_terminal")

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
            state = initial_state(
                session_id="s1",
                input_manifest_ref=str(path),
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "read_qc")
            self.assertIn("asset_registry_ref", result)
            self.assertIsNotNone(result["asset_registry_ref"])
            registry_path = Path(result["asset_registry_ref"])
            self.assertTrue(registry_path.exists())
            registry = json.loads(registry_path.read_text())
            self.assertEqual(registry["session_id"], "s1")
            self.assertEqual(len(registry["assets"]), 1)
            asset = registry["assets"][0]
            self.assertEqual(asset["provenance"]["manifest_ref"], str(path))
            self.assertEqual(asset["provenance"]["declared_path"], str(fq))
            self.assertNotIn("manifest", result["stage_outputs"]["inventory"])

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
            state = initial_state(
                session_id="cyclone-s1",
                input_manifest_ref=str(path),
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "read_qc")
            registry = json.loads(Path(result["asset_registry_ref"]).read_text())
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
                    {"asset_type": "raw_short_dna_reads", "paths": [str(short1), str(short2)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = initial_state(
                session_id="s2",
                input_manifest_ref=str(path),
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            registry = json.loads(Path(result["asset_registry_ref"]).read_text())
            self.assertEqual(len(registry["assets"]), 3)
            asset_types = {a["asset_type"] for a in registry["assets"]}
            self.assertEqual(asset_types, {"raw_long_reads", "raw_short_dna_reads"})

    def test_inventory_triggers_review_for_ambiguous_reads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = {
                "session_id": "s3",
                "assets": [
                    {"asset_type": "raw_long_reads", "paths": ["a.fq"]},
                    {"asset_type": "raw_isoseq_or_long_rna_reads", "paths": ["b.fq"]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = initial_state(
                session_id="s3",
                input_manifest_ref=str(path),
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertIsNotNone(result.get("active_interrupt"))
            self.assertEqual(result["active_interrupt"]["stage"], "inventory")
            self.assertEqual(result["current_stage"], "inventory")
            self.assertEqual(result["supervision_state"], "blocked_human")
            self.assertIsNone(result.get("asset_registry_ref"))

    def test_inventory_with_existing_assembly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fa = Path(tmpdir) / "asm.fa"
            fa.write_text(">ctg1\nACGTACGT\n")
            manifest = {
                "session_id": "s4",
                "assets": [
                    {"asset_type": "assembly_fasta_registered", "paths": [str(fa)]},
                ],
            }
            path = self._build_manifest_file(tmpdir, manifest)
            state = initial_state(
                session_id="s4",
                input_manifest_ref=str(path),
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            self.assertEqual(result["current_stage"], "assembly_review")
            self.assertEqual(result["skip_reasons"]["read_qc"], "existing_assembly_input")
            self.assertEqual(result["skip_reasons"]["genome_survey"], "existing_assembly_input")
            self.assertEqual(result["skip_reasons"]["assembly"], "existing_assembly_input")
            registry = json.loads(Path(result["asset_registry_ref"]).read_text())
            self.assertEqual(registry["assets"][0]["asset_type"], "assembly_fasta_registered")

    def test_commit_inventory_preserves_registered_asset_state(self):
        state = initial_state(
            session_id="s1",
            input_manifest_ref="manifest.yaml",
            data_dir="data",
        )
        state["asset_registry_ref"] = "data/artifacts/asset_registry/s1/registry.json"
        state["stage_outputs"] = {
            "inventory": {
                "status": "completed",
                "registered_assets": ["s1:raw_long_reads:ont.fq"],
            }
        }

        result = commit_inventory(state)

        self.assertEqual(result["current_stage"], "read_qc")
        self.assertEqual(
            result["asset_registry_ref"],
            "data/artifacts/asset_registry/s1/registry.json",
        )
        self.assertEqual(
            result["stage_outputs"]["inventory"]["registered_assets"],
            ["s1:raw_long_reads:ont.fq"],
        )

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
            state = initial_state(
                session_id="s5",
                input_manifest_ref=str(path),
                data_dir=tmpdir,
            )
            graph = build_stage()
            result = graph.invoke(state)
            events_path = Path(tmpdir) / "ledger" / "s5" / "events.jsonl"
            self.assertTrue(events_path.exists())
            lines = events_path.read_text().strip().split("\n")
            events = [json.loads(line) for line in lines]
            self.assertTrue(any(e["event_type"] == "assets_registered" for e in events))
            asset_event = next(e for e in events if e["event_type"] == "assets_registered")
            self.assertEqual(asset_event["asset_count"], 1)


if __name__ == "__main__":
    unittest.main()
