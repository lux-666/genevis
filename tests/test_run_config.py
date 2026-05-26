"""Tests for the Phase 4 structured run-config schema."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.schemas.run_config import RunConfig, load_run_config


class RunConfigSchemaTests(unittest.TestCase):
    def test_empty_config_is_valid(self):
        config = RunConfig.model_validate({})

        self.assertEqual(config.local_exec, [])
        self.assertIsNone(config.assembly)

    def test_valid_config_with_flye_parameters(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["read_qc", "assembly", "assembly_review"],
                "assembly": {
                    "flye_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                        "threads": 4,
                    }
                },
            }
        )

        self.assertEqual(config.local_exec, ["read_qc", "assembly", "assembly_review"])
        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(config.assembly.flye_parameters.read_type, "nano-raw")
        self.assertEqual(config.assembly.flye_parameters.estimated_genome_size, 2_000_000)
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

    def test_local_assembly_accepts_shared_parameters_with_flye_threads_only(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["assembly"],
                "shared_parameters": {
                    "read_type": "ont_raw",
                    "estimated_genome_size": 2_000_000,
                },
                "assembly": {
                    "flye_parameters": {
                        "threads": 4,
                    }
                },
            }
        )

        assert config.shared_parameters is not None
        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(config.shared_parameters.read_type, "ont_raw")
        self.assertIsNone(config.assembly.flye_parameters.read_type)
        self.assertIsNone(config.assembly.flye_parameters.estimated_genome_size)
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

    def test_read_qc_length_filter_choice_is_accepted(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["read_qc"],
                "read_qc": {
                    "length_filter": {
                        "method": "read_qc.length_filter.seqkit_seq",
                        "approved": True,
                        "thresholds": {"min_len": 1000},
                    }
                },
            }
        )

        assert config.read_qc is not None
        assert config.read_qc.length_filter is not None
        self.assertEqual(config.read_qc.length_filter.method, "read_qc.length_filter.seqkit_seq")
        self.assertEqual(config.read_qc.length_filter.thresholds, {"min_len": 1000})

    def test_read_qc_seqkit_filter_requires_threshold_when_approved(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "read_qc": {
                        "length_filter": {
                            "method": "read_qc.length_filter.seqkit_seq",
                            "approved": True,
                            "thresholds": {},
                        }
                    }
                }
            )

        self.assertIn("min_len", str(ctx.exception))

    def test_read_qc_planned_only_filter_cannot_be_approved(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "read_qc": {
                        "length_filter": {
                            "method": "read_qc.length_filter.filtlong",
                            "approved": True,
                            "thresholds": {"min_len": 1000},
                        }
                    }
                }
            )

        self.assertIn("approved execution", str(ctx.exception))

    def test_assembly_review_quast_parameters_are_accepted(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["assembly_review"],
                "assembly_review": {"quast": {"threads": 4}},
            }
        )

        assert config.assembly_review is not None
        assert config.assembly_review.quast is not None
        self.assertEqual(config.assembly_review.quast.threads, 4)

    def test_invalid_shared_read_type_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "shared_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                    }
                }
            )

        self.assertIn("read_type", str(ctx.exception))

    def test_unknown_stage_in_local_exec_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({"local_exec": ["assembly", "gene_annotation"]})

        self.assertIn("gene_annotation", str(ctx.exception))

    def test_unknown_top_level_field_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({"local_exec": ["read_qc"], "stages": ["read_qc"]})

        self.assertIn("stages", str(ctx.exception))

    def test_unknown_assembly_field_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "assembly": {
                        "flye_parameters": {
                            "read_type": "nano-raw",
                            "estimated_genome_size": 2_000_000,
                            "threads": 4,
                        },
                        "canu_parameters": {},
                    }
                }
            )

        self.assertIn("canu_parameters", str(ctx.exception))

    def test_unknown_flye_parameter_field_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "assembly": {
                        "flye_parameters": {
                            "read_type": "nano-raw",
                            "estimated_genome_size": 2_000_000,
                            "threads": 4,
                            "min_overlap": 1000,
                        }
                    }
                }
            )

        self.assertIn("min_overlap", str(ctx.exception))

    def test_local_exec_assembly_without_flye_parameters_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({"local_exec": ["assembly"]})

        self.assertIn("flye_parameters", str(ctx.exception))

    def test_local_exec_assembly_without_assembly_block_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {"flye_parameters": None},
                }
            )

        self.assertIn("flye_parameters", str(ctx.exception))

    def test_flye_read_type_and_genome_size_are_optional_when_threads_present(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["assembly"],
                "assembly": {
                    "flye_parameters": {
                        "threads": 4,
                    }
                },
            }
        )

        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

    def test_invalid_flye_read_type_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "flye_parameters": {
                            "read_type": "hifi",
                            "estimated_genome_size": 2_000_000,
                            "threads": 4,
                        }
                    },
                }
            )

        self.assertIn("read_type", str(ctx.exception))

    def test_non_positive_genome_size_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "flye_parameters": {
                            "read_type": "nano-raw",
                            "estimated_genome_size": 0,
                            "threads": 4,
                        }
                    },
                }
            )

        self.assertIn("estimated_genome_size", str(ctx.exception))

    def test_non_positive_threads_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "flye_parameters": {
                            "read_type": "nano-raw",
                            "estimated_genome_size": 2_000_000,
                            "threads": 0,
                        }
                    },
                }
            )

        self.assertIn("threads", str(ctx.exception))

    def test_flye_parameters_without_local_assembly_is_allowed(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["read_qc"],
                "assembly": {
                    "flye_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                        "threads": 4,
                    }
                },
            }
        )

        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

    def test_review_policy_accepts_strict_auto_and_stage_override(self):
        config = RunConfig.model_validate(
            {
                "review_policy": {
                    "mode": "auto",
                    "max_blocked_seconds": 0,
                    "stages": {
                        "hic_scaffolding": {"mode": "strict", "max_blocked_seconds": 3600}
                    },
                }
            }
        )

        assert config.review_policy is not None
        self.assertEqual(
            config.review_policy.effective_for_stage("assembly_review"),
            {"mode": "auto", "max_blocked_seconds": 0},
        )
        self.assertEqual(
            config.review_policy.effective_for_stage("hic_scaffolding"),
            {"mode": "strict", "max_blocked_seconds": 3600},
        )

    def test_review_policy_rejects_unknown_mode(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({"review_policy": {"mode": "relaxed"}})

        self.assertIn("review_policy", str(ctx.exception))

    def test_review_policy_rejects_negative_timeout(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({"review_policy": {"mode": "strict", "max_blocked_seconds": -1}})

        self.assertIn("max_blocked_seconds", str(ctx.exception))

    def test_review_policy_rejects_unknown_stage_override(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({"review_policy": {"mode": "auto", "stages": {"gene_annotation": {"mode": "strict"}}}})

        self.assertIn("gene_annotation", str(ctx.exception))

    def test_assembly_review_busco_parameters_accepted(self):
        cfg = RunConfig.model_validate({
            "local_exec": ["assembly_review"],
            "assembly_review": {
                "busco": {
                    "lineage_path": "/data/busco/embryophyta_odb10",
                    "lineage_name": "embryophyta_odb10",
                    "lineage_version": "odb10",
                    "lineage_source": "operator_offline_cache",
                    "threads": 16,
                }
            },
        })

        assert cfg.assembly_review is not None
        assert cfg.assembly_review.busco is not None
        self.assertEqual(cfg.assembly_review.busco.lineage_name, "embryophyta_odb10")
        self.assertEqual(cfg.assembly_review.busco.threads, 16)

    def test_assembly_review_busco_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"assembly_review": {"busco": {"lineage_path": "/x", "unknown": "x"}}})

    def test_assembly_review_max_reruns_accepted(self):
        cfg = RunConfig.model_validate({"assembly_review": {"max_reruns": 5}})
        assert cfg.assembly_review is not None
        self.assertEqual(cfg.assembly_review.max_reruns, 5)

    def test_assembly_review_max_reruns_default(self):
        cfg = RunConfig.model_validate({"assembly_review": {}})
        assert cfg.assembly_review is not None
        self.assertEqual(cfg.assembly_review.max_reruns, 3)

    def test_assembly_review_max_reruns_out_of_range_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"assembly_review": {"max_reruns": 0}})
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"assembly_review": {"max_reruns": 11}})

    def test_function_annotation_database_provenance_accepted(self):
        cfg = RunConfig.model_validate({
            "function_annotation": {
                "database_provenance": [
                    {
                        "method_name": "function_annotation.candidate.interproscan",
                        "database_name": "InterPro",
                        "database_version": "99.0",
                        "snapshot_date": "2026-05-16",
                        "database_source_ref": "/db/interpro/99.0",
                        "descriptor_version": "interproscan.yaml",
                        "input_proteins_ref": "/data/proteins.fa",
                    }
                ]
            }
        })
        assert cfg.function_annotation is not None
        assert len(cfg.function_annotation.database_provenance) == 1
        assert cfg.function_annotation.database_provenance[0].database_name == "InterPro"

    def test_function_annotation_empty_database_provenance_rejected_by_run_config(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"function_annotation": {"database_provenance": []}})

    def test_function_annotation_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"function_annotation": {"unknown_field": "x"}})

    def test_function_annotation_empty_database_source_ref_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "function_annotation": {
                        "database_provenance": [
                            {
                                "method_name": "function_annotation.candidate.interproscan",
                                "database_name": "InterPro",
                                "database_version": "99.0",
                                "snapshot_date": "2026-05-16",
                                "database_source_ref": "",
                                "descriptor_version": "interproscan.yaml",
                                "input_proteins_ref": "/data/proteins.fa",
                            }
                        ]
                    }
                }
            )

    def test_skip_method_with_approved_true_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["read_qc"],
                    "read_qc": {
                        "length_filter": {
                            "method": "skip",
                            "approved": True,
                        }
                    },
                }
            )

        self.assertIn("skip", str(ctx.exception))

    def test_approved_filter_without_read_qc_in_local_exec_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "read_qc": {
                        "length_filter": {
                            "method": "read_qc.length_filter.seqkit_seq",
                            "approved": True,
                            "thresholds": {"min_len": 1000},
                        }
                    }
                }
            )

        self.assertIn("local_exec", str(ctx.exception))

    def test_unapproved_filter_without_read_qc_in_local_exec_is_allowed(self):
        config = RunConfig.model_validate(
            {
                "read_qc": {
                    "length_filter": {
                        "method": "read_qc.length_filter.seqkit_seq",
                        "approved": False,
                        "thresholds": {"min_len": 1000},
                    }
                }
            }
        )

        assert config.read_qc is not None
        assert config.read_qc.length_filter is not None
        self.assertFalse(config.read_qc.length_filter.approved)


class RunConfigLoaderTests(unittest.TestCase):
    def test_load_run_config_reads_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_config.json"
            path.write_text(
                json.dumps(
                    {
                        "local_exec": ["read_qc", "assembly", "assembly_review"],
                        "assembly": {
                            "flye_parameters": {
                                "read_type": "nano-hq",
                                "estimated_genome_size": 2_000_000,
                                "threads": 4,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_run_config(path)

        self.assertEqual(config.local_exec, ["read_qc", "assembly", "assembly_review"])
        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(config.assembly.flye_parameters.read_type, "nano-hq")

    def test_load_run_config_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_run_config(Path(tmp) / "missing.json")

    def test_load_run_config_invalid_json_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_config.json"
            path.write_text("{not: valid json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_run_config(path)

        self.assertIn("not valid JSON", str(ctx.exception))

    def test_load_run_config_non_object_top_level_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_config.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_run_config(path)

        self.assertIn("JSON object", str(ctx.exception))

    def test_load_run_config_validation_error_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_config.json"
            path.write_text(json.dumps({"local_exec": ["assembly"]}), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_run_config(path)

        self.assertIn("flye_parameters", str(ctx.exception))

    def test_load_run_config_directory_path_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "config_dir"
            directory.mkdir()
            with self.assertRaises(ValueError) as ctx:
                load_run_config(directory)

        self.assertIn("not readable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
