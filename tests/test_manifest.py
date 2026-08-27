"""Tests for input manifest schema, validation, and asset inference."""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import tempfile
import unittest
from pathlib import Path

from app.schemas.manifest import (
    InputAssetSpec,
    InputManifest,
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
        self.assertTrue(
            any(
                "manifest must declare at least one asset" in e
                for e in report["errors"]
            )
        )

    def test_input_manifest_with_assets(self):
        manifest = InputManifest(
            session_id="test-02",
            sample_id="sample_A",
            assets=[
                InputAssetSpec(asset_type="raw_long_reads", paths=["reads/ont.fq.gz"]),
                InputAssetSpec(
                    asset_type="raw_short_dna_reads",
                    paths=["reads/R1.fq.gz", "reads/R2.fq.gz"],
                ),
            ],
            genome_size_estimate=450_000_000,
        )
        self.assertEqual(len(manifest.assets), 2)
        self.assertEqual(manifest.assets[0].asset_type, "raw_long_reads")
        self.assertEqual(manifest.assets[1].asset_type, "raw_short_dna_reads")

    def test_invalid_asset_type_rejected(self):
        with self.assertRaises(ValueError):
            InputAssetSpec(asset_type="invalid_type", paths=["x.fq"])

    def test_non_paired_asset_requires_non_empty_paths(self):
        for paths in ([], [""], ["   "]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                InputAssetSpec(asset_type="raw_long_reads", paths=paths)

    def test_genome_size_estimate_must_be_positive(self):
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                InputManifest(session_id="invalid-size", genome_size_estimate=value)

    def test_target_species_requires_full_latin_scientific_name(self):
        manifest = InputManifest(
            session_id="scientific-name",
            user_metadata={"target_species": "  Solea   ovata  "},
        )
        self.assertEqual(manifest.user_metadata["target_species"], "Solea ovata")

        for value in ("卵鳎", "S. ovata", "Solea sp.", 123):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "user_metadata.target_species"),
            ):
                InputManifest(
                    session_id="invalid-scientific-name",
                    user_metadata={"target_species": value},
                )

    def test_manifest_supports_all_expected_asset_types(self):
        types = [
            "raw_long_reads",
            "raw_short_dna_reads",
            "raw_rnaseq_reads",
            "raw_isoseq_or_long_rna_reads",
            "hic_reads_registered",
            "hic_alignment_bam",
            "assembly_fasta_registered",
            "reference_proteins",
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
                InputAssetSpec(
                    asset_type="raw_isoseq_or_long_rna_reads", paths=["rna.fq"]
                ),
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

    def test_detect_format_faa_is_fasta(self):
        self.assertEqual(_detect_format(Path("proteins.faa")), "fasta")
        self.assertEqual(_detect_format(Path("proteins.faa.gz")), "fasta")

    def test_detect_format_unknown(self):
        self.assertEqual(_detect_format(Path("x.txt")), "unknown")

    def test_detect_platform_from_name(self):
        self.assertEqual(_detect_platform(Path("ont_reads.fq"), "unknown"), "ont")
        self.assertEqual(_detect_platform(Path("clr_reads.fq"), "unknown"), "clr")
        self.assertEqual(
            _detect_platform(Path("hifi_reads.fq"), "unknown"), "pacbio_hifi"
        )
        self.assertEqual(
            _detect_platform(Path("pacbio_hifi_reads.fq"), "unknown"), "pacbio_hifi"
        )
        self.assertEqual(
            _detect_platform(Path("pacbio-hifi.fastq"), "unknown"), "pacbio_hifi"
        )
        self.assertEqual(
            _detect_platform(Path("pacbio.ccs.fastq"), "unknown"), "pacbio_hifi"
        )
        self.assertEqual(
            _detect_platform(Path("sample_CycloneSEQ.fastq.gz"), "unknown"),
            "cycloneseq",
        )

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
    def test_validate_rejects_directory_and_fifo_manifest_assets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            directory = root / "reads.fastq"
            directory.mkdir()
            fifo = root / "stream.fastq"
            os.mkfifo(fifo)
            for path in (directory, fifo):
                with self.subTest(path=path):
                    manifest = InputManifest(
                        session_id="special-file",
                        assets=[
                            InputAssetSpec(
                                asset_type="raw_long_reads", paths=[str(path)]
                            )
                        ],
                    )
                    report = validate_manifest(manifest)
                    self.assertFalse(report["valid"])
                    self.assertTrue(
                        any(
                            "must be regular files" in error
                            for error in report["errors"]
                        )
                    )

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
            self.assertEqual(len(report["inferred"]), 1)
            inferred = report["inferred"][0]
            self.assertEqual(inferred["asset_type"], "raw_long_reads")
            self.assertEqual(inferred["manifest_entry_ordinal"], 0)
            self.assertEqual(inferred["path_facts"][0]["declared_path"], str(fq))
            self.assertEqual(inferred["path_facts"][0]["format"], "fastq")
            self.assertEqual(inferred["path_facts"][0]["compression"], "none")

    def test_validate_rejects_zero_byte_sequence_and_assembly_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for asset_type, filename in (
                ("raw_long_reads", "reads.fastq"),
                ("assembly_fasta_registered", "assembly.fasta"),
            ):
                path = root / filename
                path.touch()
                with self.subTest(asset_type=asset_type):
                    report = validate_manifest(
                        InputManifest(
                            session_id=f"empty-{asset_type}",
                            assets=[
                                InputAssetSpec(
                                    asset_type=asset_type,
                                    paths=[str(path)],
                                )
                            ],
                        )
                    )
                    self.assertFalse(report["valid"])
                    self.assertTrue(
                        any(
                            "sequence/assembly inputs must not be empty" in error
                            for error in report["errors"]
                        )
                    )

    def test_validate_rejects_logically_empty_and_corrupt_compressed_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for kind, contents, expected in (
                ("empty", gzip.compress(b""), "logically empty"),
                ("corrupt", b"not-a-compressed-stream", "unreadable"),
            ):
                with self.subTest(codec="gzip", kind=kind):
                    path = root / f"gzip-{kind}.fastq.gz"
                    path.write_bytes(contents)
                    report = validate_manifest(
                        InputManifest(
                            session_id=f"gzip-{kind}",
                            assets=[
                                InputAssetSpec(
                                    asset_type="raw_long_reads",
                                    paths=[str(path)],
                                )
                            ],
                        )
                    )
                    self.assertFalse(report["valid"])
                    self.assertTrue(
                        any(expected in error for error in report["errors"]),
                        report["errors"],
                    )

    def test_validate_accepts_non_empty_compressed_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reads.fastq.gz"
            path.write_bytes(gzip.compress(b"@r\nACGT\n+\n!!!!\n"))
            report = validate_manifest(
                InputManifest(
                    session_id="gzip",
                    assets=[
                        InputAssetSpec(asset_type="raw_long_reads", paths=[str(path)])
                    ],
                )
            )
            self.assertTrue(report["valid"], report["errors"])

    def test_validate_rejects_compression_not_supported_by_execution_stages(self):
        codecs = (
            ("bzip2", ".fastq.bz2", bz2.compress),
            ("xz", ".fastq.xz", lzma.compress),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            for compression, suffix, compress in codecs:
                with self.subTest(compression=compression):
                    path = Path(tmpdir) / f"reads{suffix}"
                    path.write_bytes(compress(b"@r\nACGT\n+\n!!!!\n"))
                    report = validate_manifest(
                        InputManifest(
                            session_id=compression,
                            assets=[
                                InputAssetSpec(
                                    asset_type="raw_long_reads", paths=[str(path)]
                                )
                            ],
                        )
                    )
                    self.assertFalse(report["valid"])
                    self.assertTrue(
                        any(
                            "support only gzip or uncompressed" in error
                            for error in report["errors"]
                        ),
                        report["errors"],
                    )

    def test_declared_compression_controls_logical_content_probe_without_suffix(self):
        codecs = (("gzip", gzip.compress),)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for compression, compress in codecs:
                with self.subTest(compression=compression, contents="nonempty"):
                    path = root / f"{compression}-declared.fastq"
                    path.write_bytes(compress(b"@r\nACGT\n+\n!!!!\n"))
                    report = validate_manifest(
                        InputManifest(
                            session_id=f"declared-{compression}",
                            assets=[
                                InputAssetSpec(
                                    asset_type="raw_long_reads",
                                    paths=[str(path)],
                                    compression=compression,
                                )
                            ],
                        )
                    )
                    self.assertTrue(report["valid"], report["errors"])
                    self.assertEqual(
                        report["inferred"][0]["path_facts"][0]["compression"],
                        compression,
                    )

                for kind, contents, expected in (
                    ("empty", compress(b""), "logically empty"),
                    ("corrupt", b"not-a-compressed-stream", "unreadable"),
                ):
                    with self.subTest(
                        compression=compression,
                        contents=kind,
                    ):
                        path = root / f"{compression}-{kind}.fastq"
                        path.write_bytes(contents)
                        report = validate_manifest(
                            InputManifest(
                                session_id=f"declared-{compression}-{kind}",
                                assets=[
                                    InputAssetSpec(
                                        asset_type="raw_long_reads",
                                        paths=[str(path)],
                                        compression=compression,
                                    )
                                ],
                            )
                        )
                        self.assertFalse(report["valid"])
                        self.assertTrue(
                            any(expected in error for error in report["errors"]),
                            report["errors"],
                        )

    def test_declared_compression_rejects_conflicting_compression_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reads.fastq.gz"
            path.write_bytes(gzip.compress(b"@r\nACGT\n+\n!!!!\n"))
            report = validate_manifest(
                InputManifest(
                    session_id="conflicting-compression",
                    assets=[
                        InputAssetSpec(
                            asset_type="raw_long_reads",
                            paths=[str(path)],
                            compression="bzip2",
                        )
                    ],
                )
            )
        self.assertFalse(report["valid"])
        self.assertTrue(
            any(
                "declared compression conflicts" in error for error in report["errors"]
            ),
            report["errors"],
        )

    def test_declared_format_is_authoritative_without_filename_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reads.data"
            path.write_text("@r\nACGT\n+\n!!!!\n", encoding="utf-8")
            report = validate_manifest(
                InputManifest(
                    session_id="declared-format",
                    assets=[
                        InputAssetSpec(
                            asset_type="raw_long_reads",
                            paths=[str(path)],
                            platform="ont",
                            format="fastq",
                        )
                    ],
                )
            )
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["inferred"][0]["path_facts"][0]["format"], "fastq")
        self.assertFalse(report["warnings"])

    def test_declared_format_rejects_conflicting_filename_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reads.fasta"
            path.write_text(">r\nACGT\n", encoding="utf-8")
            report = validate_manifest(
                InputManifest(
                    session_id="conflicting-format",
                    assets=[
                        InputAssetSpec(
                            asset_type="raw_long_reads",
                            paths=[str(path)],
                            format="fastq",
                        )
                    ],
                )
            )
        self.assertFalse(report["valid"])
        self.assertTrue(
            any("declared format conflicts" in error for error in report["errors"]),
            report["errors"],
        )

    def test_format_inference_is_case_insensitive(self):
        self.assertEqual(_detect_format(Path("reads.FASTQ.GZ")), "fastq")

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
            self.assertTrue(
                any("could not infer format" in w for w in report["warnings"])
            )

    def test_validate_warns_ambiguous(self):
        manifest = InputManifest(
            session_id="test-ambiguous",
            assets=[
                InputAssetSpec(asset_type="raw_long_reads", paths=["a.fq"]),
                InputAssetSpec(
                    asset_type="raw_isoseq_or_long_rna_reads", paths=["b.fq"]
                ),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertTrue(any("ambiguous" in w for w in report["warnings"]))

    def test_validate_existing_assembly_skips_reads(self):
        manifest = InputManifest(
            session_id="test-assembly",
            assets=[
                InputAssetSpec(
                    asset_type="assembly_fasta_registered", paths=["asm.fa"]
                ),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertTrue(report["valid"])

    def test_validate_hic_alignment_bam_input(self):
        manifest = InputManifest(
            session_id="test-hic-bam",
            assets=[
                InputAssetSpec(
                    asset_type="hic_alignment_bam", paths=["hic.bam"], format="bam"
                ),
            ],
        )
        report = validate_manifest(manifest, require_existing_files=False)
        self.assertTrue(report["valid"])
        self.assertEqual(report["inferred"][0]["asset_type"], "hic_alignment_bam")

    def test_annotation_only_asset_types_are_rejected(self):
        for asset_type in (
            "repeat_library",
            "repeat_annotation",
            "softmasked_genome",
            "gene_models",
            "predicted_proteins",
            "transcripts_fasta",
            "function_database",
            "function_annotation",
        ):
            with self.subTest(asset_type=asset_type), self.assertRaises(ValueError):
                InputAssetSpec(asset_type=asset_type, paths=["removed"])

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
            props = _infer_asset_properties(spec, manifest_entry_ordinal=0)
            self.assertEqual(props.inferred_layout, "paired")
            self.assertEqual(
                [fact.compression for fact in props.path_facts],
                ["gzip", "gzip"],
            )
            self.assertEqual(
                [fact.format for fact in props.path_facts],
                ["fastq", "fastq"],
            )

    def test_infer_cycloneseq_platform_from_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fq = Path(tmpdir) / "sample.fastq.gz"
            fq.write_bytes(gzip.compress(b"@r1\nACGT\n+\n!!!!\n"))
            spec = InputAssetSpec(
                asset_type="raw_long_reads",
                paths=[str(fq)],
                metadata={"technology": "BGI CycloneSEQ"},
            )
            props = _infer_asset_properties(spec, manifest_entry_ordinal=0)
            self.assertEqual(props.path_facts[0].platform, "cycloneseq")


if __name__ == "__main__":
    unittest.main()
