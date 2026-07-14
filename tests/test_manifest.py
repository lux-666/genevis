"""Tests for input manifest schema, validation, and asset inference."""

from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from app.schemas.manifest import (
    Compression,
    Format,
    InputAssetSpec,
    InputManifest,
    Layout,
    Platform,
    _detect_compression,
    _detect_format,
    _detect_layout,
    _detect_platform,
    _infer_asset_properties,
    validate_manifest,
)


class ManifestSchemaTests(unittest.TestCase):
    def test_input_manifest_minimal(self):
        manifest = InputManifest(session_id="test-01")
        self.assertEqual(manifest.session_id, "test-01")
        self.assertEqual(manifest.assets, [])
        report = validate_manifest(manifest)
        self.assertFalse(report["valid"])
        self.assertTrue(any("manifest must declare at least one asset" in e for e in report["errors"]))

    def test_input_manifest_with_assets(self):
        manifest = InputManifest(
            session_id="test-02",
            sample_id="sample_A",
            assets=[
                InputAssetSpec(asset_type="raw_long_reads", paths=["reads/ont.fq.gz"]),
                InputAssetSpec(asset_type="raw_short_dna_reads", paths=["reads/R1.fq.gz", "reads/R2.fq.gz"]),
            ],
            target_assembly_level="chromosome",
            genome_size_estimate=450_000_000,
        )
        self.assertEqual(len(manifest.assets), 2)
        self.assertEqual(manifest.assets[0].asset_type, "raw_long_reads")
        self.assertEqual(manifest.assets[1].asset_type, "raw_short_dna_reads")

    def test_invalid_asset_type_rejected(self):
        with self.assertRaises(ValueError):
            InputAssetSpec(asset_type="invalid_type", paths=["x.fq"])

    def test_genome_size_estimate_must_be_positive(self):
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                InputManifest(session_id="invalid-size", genome_size_estimate=value)

    def test_manifest_supports_all_expected_asset_types(self):
        types = [
            "raw_long_reads",
            "raw_short_dna_reads",
            "raw_rnaseq_reads",
            "raw_isoseq_or_long_rna_reads",
            "hic_reads_registered",
            "hic_alignment_bam",
            "assembly_fasta_registered",
            "repeat_library",
            "reference_proteins",
            "gene_models",
            "predicted_proteins",
            "transcripts_fasta",
        ]
        for t in types:
            with self.subTest(asset_type=t):
                spec = InputAssetSpec(asset_type=t, paths=["dummy"])
                self.assertEqual(spec.asset_type, t)

    def test_ambiguous_long_reads_flag(self):
        manifest = InputManifest(
            session_id="test-03",
            assets=[
                InputAssetSpec(asset_type="raw_long_reads", paths=["reads.fq"]),
                InputAssetSpec(asset_type="raw_isoseq_or_long_rna_reads", paths=["rna.fq"]),
            ],
        )
        raw = manifest.assets[0]
        rna = manifest.assets[1]
        self.assertTrue(raw.metadata.get("ambiguous") or rna.metadata.get("ambiguous"))


class FileInferenceTests(unittest.TestCase):
    def test_detect_compression_gzip(self):
        self.assertEqual(_detect_compression(Path("x.fq.gz")), "gzip")
        self.assertEqual(_detect_compression(Path("x.fastq.gz")), "gzip")

    def test_detect_compression_none(self):
        self.assertEqual(_detect_compression(Path("x.fq")), "none")
        self.assertEqual(_detect_compression(Path("x.fasta")), "none")

    def test_detect_format_fastq(self):
        self.assertEqual(_detect_format(Path("x.fq")), "fastq")
        self.assertEqual(_detect_format(Path("x.fastq.gz")), "fastq")

    def test_detect_format_fasta(self):
        self.assertEqual(_detect_format(Path("x.fa")), "fasta")
        self.assertEqual(_detect_format(Path("x.fasta.gz")), "fasta")
        self.assertEqual(_detect_format(Path("x.fna")), "fasta")
        self.assertEqual(_detect_format(Path("x.fna.gz")), "fasta")

    def test_detect_format_bam(self):
        self.assertEqual(_detect_format(Path("x.bam")), "bam")

    def test_detect_format_gene_annotation_formats(self):
        self.assertEqual(_detect_format(Path("genes.gff3")), "gff3")
        self.assertEqual(_detect_format(Path("genes.gff3.gz")), "gff3")
        self.assertEqual(_detect_format(Path("genes.gff")), "gff")
        self.assertEqual(_detect_format(Path("genes.gtf")), "gtf")

    def test_detect_format_faa_is_fasta(self):
        self.assertEqual(_detect_format(Path("proteins.faa")), "fasta")
        self.assertEqual(_detect_format(Path("proteins.faa.gz")), "fasta")

    def test_detect_format_unknown(self):
        self.assertEqual(_detect_format(Path("x.txt")), "unknown")

    def test_detect_platform_from_name(self):
        self.assertEqual(_detect_platform(Path("ont_reads.fq"), "unknown"), "ont")
        self.assertEqual(_detect_platform(Path("clr_reads.fq"), "unknown"), "clr")
        self.assertEqual(_detect_platform(Path("hifi_reads.fq"), "unknown"), "pacbio_hifi")
        self.assertEqual(_detect_platform(Path("pacbio_hifi_reads.fq"), "unknown"), "pacbio_hifi")
        self.assertEqual(_detect_platform(Path("pacbio-hifi.fastq"), "unknown"), "pacbio_hifi")
        self.assertEqual(_detect_platform(Path("pacbio.ccs.fastq"), "unknown"), "pacbio_hifi")
        self.assertEqual(_detect_platform(Path("sample_CycloneSEQ.fastq.gz"), "unknown"), "cycloneseq")

    def test_detect_platform_respects_declared(self):
        self.assertEqual(_detect_platform(Path("x.fq"), "illumina"), "illumina")
        self.assertEqual(_detect_platform(Path("x.fq"), "cycloneseq"), "cycloneseq")

    def test_detect_layout_paired(self):
        self.assertEqual(
            _detect_layout([Path("R1.fq"), Path("R2.fq")]),
            "paired",
        )
        self.assertEqual(
            _detect_layout([Path("sample_R1.fastq.gz"), Path("sample_R2.fastq.gz")]),
            "paired",
        )

    def test_detect_layout_single(self):
        self.assertEqual(_detect_layout([Path("reads.fq")]), "single")


class ManifestValidationTests(unittest.TestCase):
    def test_validate_missing_files(self):
        manifest = InputManifest(
            session_id="test-missing",
            assets=[
                InputAssetSpec(asset_type="raw_long_reads", paths=["nonexistent.fq"]),
            ],
        )
        report = validate_manifest(manifest)
        self.assertFalse(report["valid"])
        self.assertTrue(any("missing files" in e for e in report["errors"]))

    def test_validate_existing_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "ont.fq"
            fq.write_text("@read1\nACGT\n+\n!!!!\n")
            manifest = InputManifest(
                session_id="test-existing",
                assets=[
                    InputAssetSpec(asset_type="raw_long_reads", paths=[str(fq)]),
                ],
            )
            report = validate_manifest(manifest)
            self.assertTrue(report["valid"])
            self.assertEqual(report["errors"], [])
            self.assertIn("raw_long_reads", report["inferred"])
            inferred = report["inferred"]["raw_long_reads"]
            self.assertEqual(inferred["paths_exist"], [str(fq)])
            self.assertEqual(inferred["detected_format"][str(fq)], "fastq")
            self.assertEqual(inferred["detected_compression"][str(fq)], "none")

    def test_validate_warns_unknown_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "reads.xyz"
            p.write_text("data")
            manifest = InputManifest(
                session_id="test-format",
                assets=[
                    InputAssetSpec(asset_type="raw_long_reads", paths=[str(p)]),
                ],
            )
            report = validate_manifest(manifest)
            self.assertTrue(report["valid"])
            self.assertTrue(any("could not infer format" in w for w in report["warnings"]))

    def test_validate_warns_ambiguous(self):
        manifest = InputManifest(
            session_id="test-ambiguous",
            assets=[
                InputAssetSpec(asset_type="raw_long_reads", paths=["a.fq"]),
                InputAssetSpec(asset_type="raw_isoseq_or_long_rna_reads", paths=["b.fq"]),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertTrue(any("ambiguous" in w for w in report["warnings"]))

    def test_validate_existing_assembly_skips_reads(self):
        manifest = InputManifest(
            session_id="test-assembly",
            assets=[
                InputAssetSpec(asset_type="assembly_fasta_registered", paths=["asm.fa"]),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertTrue(report["valid"])

    def test_validate_hic_alignment_bam_input(self):
        manifest = InputManifest(
            session_id="test-hic-bam",
            assets=[
                InputAssetSpec(asset_type="hic_alignment_bam", paths=["hic.bam"], format="bam"),
            ],
            target_assembly_level="chromosome",
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertTrue(report["valid"])
        self.assertIn("hic_alignment_bam", report["inferred"])

    def test_validate_provided_gene_annotation_assets(self):
        manifest = InputManifest(
            session_id="test-provided-annotation",
            assets=[
                InputAssetSpec(asset_type="assembly_fasta_registered", paths=["asm.fa"]),
                InputAssetSpec(asset_type="gene_models", paths=["genes.gff3"]),
                InputAssetSpec(asset_type="predicted_proteins", paths=["proteins.fa"]),
                InputAssetSpec(asset_type="transcripts_fasta", paths=["transcripts.fa"]),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertTrue(report["valid"])
        self.assertEqual(report["inferred"]["gene_models"]["inferred_format"], "gff3")
        self.assertEqual(report["inferred"]["predicted_proteins"]["inferred_format"], "fasta")
        self.assertEqual(report["inferred"]["transcripts_fasta"]["inferred_format"], "fasta")

    def test_validate_rejects_wrong_provided_annotation_formats(self):
        manifest = InputManifest(
            session_id="test-bad-provided-annotation",
            assets=[
                InputAssetSpec(asset_type="gene_models", paths=["genes.bam"]),
                InputAssetSpec(asset_type="predicted_proteins", paths=["proteins.fq"]),
                InputAssetSpec(asset_type="transcripts_fasta", paths=["transcripts.gtf"]),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertFalse(report["valid"])
        self.assertTrue(any("gene_models" in error and "GFF" in error for error in report["errors"]))
        self.assertTrue(any("predicted_proteins" in error and "FASTA" in error for error in report["errors"]))
        self.assertTrue(any("transcripts_fasta" in error and "FASTA" in error for error in report["errors"]))

    def test_validate_rejects_unknown_format_for_existing_annotation_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            genes = Path(tmpdir) / "genes.weird"
            proteins = Path(tmpdir) / "proteins.notfasta"
            genes.write_text("##gff-version 3\n")
            proteins.write_text(">p1\nMKT\n")
            manifest = InputManifest(
                session_id="test-unknown-format",
                assets=[
                    InputAssetSpec(asset_type="gene_models", paths=[str(genes)]),
                    InputAssetSpec(asset_type="predicted_proteins", paths=[str(proteins)]),
                ],
            )
            report = validate_manifest(manifest, require_existing_files=False)
            self.assertFalse(report["valid"])
            self.assertTrue(any("gene_models" in e and "GFF" in e for e in report["errors"]))
            self.assertTrue(any("predicted_proteins" in e and "FASTA" in e for e in report["errors"]))

    def test_validate_unknown_format_nonexistent_annotation_files_is_error(self):
        manifest = InputManifest(
            session_id="test-unknown-format-missing",
            assets=[
                InputAssetSpec(asset_type="gene_models", paths=["genes.weird"]),
                InputAssetSpec(asset_type="predicted_proteins", paths=["proteins.notfasta"]),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertFalse(report["valid"])
        self.assertTrue(any("gene_models" in e and "GFF" in e for e in report["errors"]))
        self.assertTrue(any("predicted_proteins" in e and "FASTA" in e for e in report["errors"]))

    def test_validate_rejects_duplicate_adoption_asset_entries(self):
        manifest = InputManifest(
            session_id="test-duplicate-adoption",
            assets=[
                InputAssetSpec(asset_type="gene_models", paths=["a.gff3"]),
                InputAssetSpec(asset_type="gene_models", paths=["b.gff3"]),
                InputAssetSpec(asset_type="predicted_proteins", paths=["p.faa"]),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertFalse(report["valid"])
        self.assertTrue(any("gene_models" in e and "exactly once" in e for e in report["errors"]))


    def test_validate_rejects_multi_path_adoption_assets(self):
        manifest = InputManifest(
            session_id="test-multi-path",
            assets=[
                InputAssetSpec(asset_type="gene_models", paths=["a.gff3", "b.gff3"]),
                InputAssetSpec(asset_type="predicted_proteins", paths=["p1.faa", "p2.faa"]),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertFalse(report["valid"])
        self.assertTrue(any("gene_models" in e and "exactly one path" in e for e in report["errors"]))
        self.assertTrue(any("predicted_proteins" in e and "exactly one path" in e for e in report["errors"]))

    def test_infer_asset_properties_combined(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            r1 = Path(tmpdir) / "sample_R1.fq.gz"
            r2 = Path(tmpdir) / "sample_R2.fq.gz"
            r1.write_bytes(gzip.compress(b"@r1\nACGT\n+\n!!!!\n"))
            r2.write_bytes(gzip.compress(b"@r2\nTGCA\n+\n!!!!\n"))
            spec = InputAssetSpec(
                asset_type="raw_short_dna_reads",
                paths=[str(r1), str(r2)],
                sample_id="sample_A",
            )
            props = _infer_asset_properties(spec)
            self.assertEqual(props["inferred_layout"], "paired")
            self.assertEqual(props["inferred_compression"], "gzip")
            self.assertEqual(props["inferred_format"], "fastq")

    def test_infer_cycloneseq_platform_from_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "sample.fastq.gz"
            fq.write_bytes(gzip.compress(b"@r1\nACGT\n+\n!!!!\n"))
            spec = InputAssetSpec(
                asset_type="raw_long_reads",
                paths=[str(fq)],
                metadata={"technology": "BGI CycloneSEQ"},
            )
            props = _infer_asset_properties(spec)
            self.assertEqual(props["inferred_platform"], "cycloneseq")


if __name__ == "__main__":
    unittest.main()
