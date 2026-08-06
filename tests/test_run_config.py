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
                        "threads": 4,
                    }
                },
            }
        )

        self.assertEqual(config.local_exec, ["read_qc", "assembly", "assembly_review"])
        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

    def test_valid_config_with_nextdenovo_parameters(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["assembly"],
                "assembly": {
                    "assembly_method": "nextdenovo",
                    "nextdenovo_parameters": {
                        "threads": 4,
                    },
                },
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.assembly_method, "nextdenovo")
        assert config.assembly.nextdenovo_parameters is not None
        self.assertEqual(config.assembly.nextdenovo_parameters.threads, 4)

    def test_legacy_method_fact_and_l2_fields_are_rejected(self):
        cases = (
            ("flye_parameters", {"threads": 4, "estimated_genome_size": 2_000_000}),
            ("flye_parameters", {"threads": 4, "read_type": "nano-raw"}),
            ("nextdenovo_parameters", {"threads": 4, "genome_size": 2_000_000}),
            ("nextdenovo_parameters", {"threads": 4, "read_type": "ont"}),
            ("nextdenovo_parameters", {"threads": 4, "read_cutoff": 1000}),
            ("hifiasm_parameters", {"threads": 4, "read_type": "hifi"}),
            ("hifiasm_ont_parameters", {"threads": 4, "read_type": "ont"}),
        )

        for parameter_key, parameters in cases:
            with self.subTest(parameter_key=parameter_key, parameters=parameters):
                with self.assertRaises(ValidationError):
                    RunConfig.model_validate(
                        {
                            "assembly": {
                                parameter_key: parameters,
                            }
                        }
                    )

    def test_valid_config_with_hifiasm_parameters(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["assembly"],
                "assembly": {
                    "assembly_method": "hifiasm",
                    "hifiasm_parameters": {
                        "threads": 4,
                    },
                },
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.assembly_method, "hifiasm")
        assert config.assembly.hifiasm_parameters is not None
        self.assertEqual(config.assembly.hifiasm_parameters.threads, 4)

    def test_valid_config_with_hifiasm_ont_parameters(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["assembly"],
                "assembly": {
                    "assembly_method": "hifiasm_ont",
                    "hifiasm_ont_parameters": {
                        "threads": 4,
                    },
                },
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.assembly_method, "hifiasm_ont")
        assert config.assembly.hifiasm_ont_parameters is not None
        self.assertEqual(config.assembly.hifiasm_ont_parameters.threads, 4)

    def test_empty_assembly_parameter_blocks_do_not_materialize_threads(self):
        cases = (
            ("flye", "flye_parameters"),
            ("nextdenovo", "nextdenovo_parameters"),
            ("hifiasm", "hifiasm_parameters"),
            ("hifiasm_ont", "hifiasm_ont_parameters"),
        )

        for method, parameter_key in cases:
            payload = {
                "local_exec": ["assembly"],
                "assembly": {
                    "assembly_method": method,
                    parameter_key: {},
                },
            }
            with self.subTest(method=method):
                config = RunConfig.model_validate(payload)
                parameters = getattr(config.assembly, parameter_key)
                self.assertIsNotNone(parameters)
                self.assertIsNone(parameters.threads)

    def test_local_hifiasm_method_requires_hifiasm_parameters(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "assembly_method": "hifiasm",
                        "flye_parameters": {"threads": 4},
                    },
                }
            )

        self.assertIn("hifiasm_parameters", str(ctx.exception))

    def test_local_hifiasm_ont_method_requires_hifiasm_ont_parameters(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "assembly_method": "hifiasm_ont",
                        "hifiasm_parameters": {"threads": 4},
                    },
                }
            )

        self.assertIn("hifiasm_ont_parameters", str(ctx.exception))

    def test_local_assembly_with_two_parameter_sets_requires_explicit_method(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "flye_parameters": {"threads": 4},
                        "nextdenovo_parameters": {"threads": 4},
                    },
                }
            )

        self.assertIn("assembly_method", str(ctx.exception))

    def test_assembly_phase8_fields_are_accepted(self):
        config = RunConfig.model_validate(
            {
                "assembly": {
                    "hifi_reads_ref": "hifi.fofn",
                    "hic_reads_ref": "hic.fofn",
                    "target_level": "near_t2t",
                    "assembly_route": "hifi",
                }
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.hifi_reads_ref, "hifi.fofn")
        self.assertEqual(config.assembly.hic_reads_ref, "hic.fofn")
        self.assertEqual(config.assembly.target_level, "near_t2t")
        self.assertEqual(config.assembly.assembly_route, "hifi")

    def test_route_consistency_allows_compatible_inferred_method(self):
        config = RunConfig.model_validate(
            {
                "assembly": {
                    "assembly_route": "hifi",
                    "flye_parameters": {"threads": 4},
                }
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.assembly_route, "hifi")

    def test_cyclone_route_allows_same_explicit_method_overrides_as_ont(self):
        """run_config route compatibility is an explicit override allow-list."""
        for route in ["ont", "cyclone"]:
            for method, parameters_key in [
                ("flye", "flye_parameters"),
                ("nextdenovo", "nextdenovo_parameters"),
                ("hifiasm_ont", "hifiasm_ont_parameters"),
            ]:
                with self.subTest(route=route, method=method):
                    config = RunConfig.model_validate(
                        {
                            "assembly": {
                                "assembly_route": route,
                                "assembly_method": method,
                                parameters_key: {"threads": 4},
                            }
                        }
                    )

                    assert config.assembly is not None
                    self.assertEqual(config.assembly.assembly_route, route)
                    self.assertEqual(config.assembly.assembly_method, method)

    def test_cyclone_route_read_type_rejects_incompatible_hifiasm(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "assembly": {
                        "assembly_route": "cyclone",
                        "assembly_method": "hifiasm",
                        "hifiasm_parameters": {"threads": 4},
                    }
                }
            )

        self.assertIn(
            "implies read_type 'cyclone' and is incompatible with "
            "assembly_method 'hifiasm'",
            str(ctx.exception),
        )

    def test_local_assembly_accepts_shared_parameters_with_flye_threads_only(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["assembly"],
                "shared_parameters": {
                    "platform": "ont",
                    "read_type": "ont_raw",
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
        self.assertEqual(config.shared_parameters.platform, "ont")
        self.assertEqual(config.shared_parameters.read_type, "ont_raw")
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

    def test_run_config_rejects_manifest_owned_genome_size(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "shared_parameters": {
                        "estimated_genome_size": 2_000_000,
                    },
                }
            )

        self.assertIn("estimated_genome_size", str(ctx.exception))

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
        self.assertEqual(
            config.read_qc.length_filter.method, "read_qc.length_filter.seqkit_seq"
        )
        self.assertEqual(config.read_qc.length_filter.thresholds, {"min_len": 1000})

    def test_genome_survey_parameters_are_accepted_without_read_length(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["genome_survey"],
                "genome_survey": {
                    "survey_parameters": {
                        "kmer_size": 21,
                        "hash_size": "1G",
                        "threads": 4,
                    }
                },
            }
        )

        assert config.genome_survey is not None
        assert config.genome_survey.survey_parameters is not None
        self.assertEqual(config.genome_survey.survey_parameters.kmer_size, 21)
        self.assertEqual(config.genome_survey.survey_parameters.hash_size, "1G")
        self.assertEqual(config.genome_survey.survey_parameters.threads, 4)

    def test_genome_survey_read_length_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["genome_survey"],
                    "genome_survey": {
                        "survey_parameters": {
                            "kmer_size": 21,
                            "hash_size": "1G",
                            "threads": 4,
                            "read_length": 150,
                        }
                    },
                }
            )

        self.assertIn("read_length", str(ctx.exception))

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

    def test_read_qc_unsupported_filter_method_is_rejected(self):
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

        self.assertIn("literal_error", str(ctx.exception))

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

    def test_assembly_dedup_is_not_a_local_exec_stage(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({"local_exec": ["assembly_dedup"]})

        self.assertIn("assembly_dedup", str(ctx.exception))

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
            RunConfig.model_validate({"local_exec": ["assembly", "not_a_real_stage"]})

        self.assertIn("not_a_real_stage", str(ctx.exception))

    def test_gene_annotation_and_function_annotation_accepted_in_local_exec(self):
        config = RunConfig.model_validate(
            {"local_exec": ["gene_annotation", "function_annotation"]}
        )
        self.assertIn("gene_annotation", config.local_exec)
        self.assertIn("function_annotation", config.local_exec)

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

    def test_local_nextdenovo_method_requires_nextdenovo_parameters(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "assembly_method": "nextdenovo",
                        "flye_parameters": {"threads": 4},
                    },
                }
            )

        self.assertIn("nextdenovo_parameters", str(ctx.exception))

    def test_flye_method_block_contains_only_executor_threads(self):
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
                            "threads": 4,
                        }
                    },
                }
            )

        self.assertIn("read_type", str(ctx.exception))

    def test_legacy_method_genome_size_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "local_exec": ["assembly"],
                    "assembly": {
                        "flye_parameters": {
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
                        "threads": 4,
                    }
                },
            }
        )

        assert config.assembly is not None
        assert config.assembly.flye_parameters is not None
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

    def test_review_policy_defaults_to_five_minute_countdown_and_stage_override(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [],
                "review_policy": {
                    "stages": {
                        "assembly": {"max_blocked_seconds": -1},
                        "hic_scaffolding": {"max_blocked_seconds": 0},
                    },
                }
            }
        )

        assert config.review_policy is not None
        self.assertEqual(config.review_policy.max_blocked_seconds, 300)
        self.assertEqual(
            config.review_policy.effective_max_blocked_seconds("assembly_review"), 300
        )
        self.assertEqual(
            config.review_policy.effective_max_blocked_seconds("assembly"), -1
        )
        self.assertEqual(
            config.review_policy.effective_max_blocked_seconds("hic_scaffolding"), 0
        )

    def test_review_policy_rejects_removed_mode(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {"requested_stages": [], "review_policy": {"mode": "auto"}}
            )

        self.assertIn("mode", str(ctx.exception))

    def test_review_policy_rejects_timeout_below_indefinite(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {"requested_stages": [], "review_policy": {"max_blocked_seconds": -2}}
            )

        self.assertIn("max_blocked_seconds", str(ctx.exception))

    def test_review_policy_rejects_unknown_stage_override(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "review_policy": {
                        "stages": {"removed_stage": {"max_blocked_seconds": 1}},
                    }
                }
            )

        self.assertIn("removed_stage", str(ctx.exception))

    def test_assembly_review_busco_parameters_accepted(self):
        cfg = RunConfig.model_validate(
            {
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
            }
        )

        assert cfg.assembly_review is not None
        assert cfg.assembly_review.busco is not None
        self.assertEqual(cfg.assembly_review.busco.lineage_name, "embryophyta_odb10")
        self.assertEqual(cfg.assembly_review.busco.threads, 16)

    def test_assembly_review_busco_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {"assembly_review": {"busco": {"lineage_path": "/x", "unknown": "x"}}}
            )

    def test_assembly_review_max_reruns_accepted(self):
        cfg = RunConfig.model_validate({"assembly_review": {"max_reruns": 5}})
        assert cfg.assembly_review is not None
        self.assertEqual(cfg.assembly_review.max_reruns, 5)

    def test_assembly_review_related_species_reference_dir_accepted(self):
        cfg = RunConfig.model_validate(
            {"assembly_review": {"related_species_reference_dir": "/ref/ncbi_metazoa"}}
        )
        assert cfg.assembly_review is not None
        self.assertEqual(
            cfg.assembly_review.related_species_reference_dir, "/ref/ncbi_metazoa"
        )

    def test_assembly_review_max_reruns_default(self):
        cfg = RunConfig.model_validate({"assembly_review": {}})
        assert cfg.assembly_review is not None
        self.assertEqual(cfg.assembly_review.max_reruns, 3)

    def test_assembly_review_max_reruns_out_of_range_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"assembly_review": {"max_reruns": 0}})
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"assembly_review": {"max_reruns": 11}})

    def test_function_annotation_database_provenance_accepts_diamond(self):
        cfg = RunConfig.model_validate(
            {
                "function_annotation": {
                    "database_provenance": [
                        {
                            "method_name": "function_annotation.candidate.diamond",
                            "database_name": "UniProt",
                            "database_version": "2026_02",
                            "snapshot_date": "2026-05-16",
                            "database_source_ref": "/db/uniprot/2026_02.dmnd",
                            "descriptor_version": "diamond.yaml",
                            "input_proteins_ref": "/data/proteins.fa",
                        }
                    ]
                }
            }
        )
        assert cfg.function_annotation is not None
        assert len(cfg.function_annotation.database_provenance) == 1
        assert cfg.function_annotation.database_provenance[0].database_name == "UniProt"

    def test_function_annotation_rejects_deleted_provenance_methods(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "function_annotation": {
                        "database_provenance": [
                            {
                                "method_name": (
                                    "function_annotation.candidate.interproscan"
                                ),
                                "database_name": "InterPro",
                                "database_version": "99.0",
                                "snapshot_date": "2026-05-16",
                                "database_source_ref": "/db/interpro/99.0",
                                "descriptor_version": "interproscan.yaml",
                                "input_proteins_ref": "/data/proteins.fa",
                            }
                        ]
                    }
                }
            )

    def test_function_annotation_empty_database_provenance_rejected_by_run_config(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {"function_annotation": {"database_provenance": []}}
            )

    def test_function_annotation_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"function_annotation": {"unknown_field": "x"}})

    def test_gene_annotation_lineage_or_species_hints_accepted(self):
        cfg = RunConfig.model_validate(
            {
                "gene_annotation": {
                    "lineage_or_species_hints": {
                        "busco_lineage": "/db/busco/actinopterygii_odb10",
                        "augustus_species_model": "zebrafish",
                    }
                }
            }
        )

        assert cfg.gene_annotation is not None
        self.assertEqual(
            cfg.gene_annotation.lineage_or_species_hints,
            {
                "busco_lineage": "/db/busco/actinopterygii_odb10",
                "augustus_species_model": "zebrafish",
            },
        )

    def test_gene_annotation_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate({"gene_annotation": {"unknown_field": "x"}})

    def test_function_annotation_empty_database_source_ref_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "function_annotation": {
                        "database_provenance": [
                            {
                                "method_name": "function_annotation.candidate.diamond",
                                "database_name": "UniProt",
                                "database_version": "2026_02",
                                "snapshot_date": "2026-05-16",
                                "database_source_ref": "",
                                "descriptor_version": "diamond.yaml",
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

    def test_autohic_parameters_are_accepted(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["hic_scaffolding"],
                "hic_scaffolding": {
                    "autohic_parameters": {
                        "restriction_enzyme": "MboI",
                        "threads": 40,
                    },
                },
            }
        )
        assert config.hic_scaffolding.autohic_parameters is not None
        self.assertEqual(
            config.hic_scaffolding.autohic_parameters.restriction_enzyme,
            "MboI",
        )
        self.assertEqual(config.hic_scaffolding.autohic_parameters.threads, 40)

        defaulted = RunConfig.model_validate(
            {"hic_scaffolding": {"autohic_parameters": {}}}
        )
        assert defaulted.hic_scaffolding.autohic_parameters is not None
        self.assertIsNone(defaulted.hic_scaffolding.autohic_parameters.threads)
        self.assertEqual(
            defaulted.hic_scaffolding.autohic_parameters.python_command,
            "python3.9",
        )

    def test_t2t_run_config_does_not_materialize_catalog_owned_mode(self):
        config = RunConfig.model_validate({"local_exec": ["t2t"], "t2t": {}})

        self.assertIsNotNone(config.t2t)
        self.assertIsNone(config.t2t.mode)

    def test_autohic_run_config_rejects_catalog_owned_parameters(self):
        for name, value in (
            ("number_of_edit_rounds", 3),
            ("translocation_adjust", False),
            ("inversion_adjust", False),
            ("debris_adjust", True),
            ("error_max_epoch", 10),
            ("error_min_len", 20_000),
            ("error_max_len", 10_000_000),
            ("error_filter_score", 0.8),
            ("error_filter_iou_score", 0.7),
        ):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                RunConfig.model_validate(
                    {
                        "hic_scaffolding": {
                            "autohic_parameters": {name: value},
                        }
                    }
                )

    def test_local_hic_execution_needs_no_method_or_enzyme_choice(self):
        config = RunConfig.model_validate(
            {
                "local_exec": ["hic_scaffolding"],
                "hic_scaffolding": {},
            }
        )

        self.assertIsNotNone(config.hic_scaffolding)
        self.assertIsNone(config.hic_scaffolding.autohic_parameters)

    def test_hic_run_config_rejects_removed_method_selection_fields(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "hic_scaffolding": {
                        "method": "autohic",
                        "methods": ["autohic"],
                    }
                }
            )


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
        self.assertEqual(config.assembly.flye_parameters.threads, 4)

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
