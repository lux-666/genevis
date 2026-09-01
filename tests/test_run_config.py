"""Tests for the Phase 4 structured run-config schema."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.entrypoints.run import _stage_seeds_from_run_config
from app.schemas.run_config import RunConfig, load_run_config


def _busco_config(lineage: str = "embryophyta_odb10") -> dict[str, object]:
    """Return the only BUSCO fields a user RunConfig may provide."""

    return {"lineage": lineage, "threads": 4}


def _stage_requests(
    *stages: str,
    local: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    return [
        {"stage": stage, "allow_local_execution": stage in local} for stage in stages
    ]


def _local_stage_requests(*stages: str) -> list[dict[str, object]]:
    return _stage_requests(*stages, local=stages)


class RunConfigSchemaTests(unittest.TestCase):
    def test_empty_requested_stage_scope_is_valid(self):
        config = RunConfig.model_validate({"requested_stages": []})

        self.assertEqual(config.requested_stage_names(), [])
        self.assertIsNone(config.assembly)

    def test_requested_stages_is_required(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate({})

        self.assertIn("requested_stages", str(ctx.exception))

    def test_review_placement_is_literal_and_optional(self):
        valid_sequences = (
            ("assembly",),
            ("assembly", "assembly", "assembly_review"),
            ("assembly", "t2t", "assembly_review"),
            ("assembly", "gapcloser", "assembly_review"),
            ("t2t",),
            ("t2t", "t2t", "assembly_review"),
            ("t2t", "base_polish", "assembly_review"),
            ("assembly", "assembly_review", "t2t"),
        )

        for stages in valid_sequences:
            with self.subTest(stages=stages):
                config = RunConfig.model_validate(
                    {"requested_stages": _stage_requests(*stages)}
                )
                self.assertEqual(config.requested_stage_names(), list(stages))

    def test_standalone_review_and_t2t_then_review_are_valid(self):
        for stages in (("assembly_review",), ("t2t", "assembly_review")):
            with self.subTest(stages=stages):
                config = RunConfig.model_validate(
                    {"requested_stages": _stage_requests(*stages)}
                )
                self.assertEqual(config.requested_stage_names(), list(stages))

    def test_terminal_placeholders_are_allowed_after_review_boundary_closes(self):
        for stages in (
            ("assembly", "assembly_review", "gapcloser"),
            ("t2t", "assembly_review", "base_polish"),
        ):
            with self.subTest(stages=stages):
                config = RunConfig.model_validate(
                    {"requested_stages": _stage_requests(*stages)}
                )
                self.assertEqual(config.requested_stage_names(), list(stages))

    def test_duplicate_stage_requests_preserve_order_and_local_consent(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [
                    {"stage": "read_qc", "allow_local_execution": False},
                    {"stage": "t2t", "allow_local_execution": False},
                    {"stage": "assembly_review", "allow_local_execution": False},
                    {"stage": "read_qc", "allow_local_execution": True},
                ]
            }
        )

        self.assertEqual(
            config.requested_stage_names(),
            ["read_qc", "t2t", "assembly_review", "read_qc"],
        )
        self.assertEqual(
            [request.allow_local_execution for request in config.requested_stages],
            [False, False, False, True],
        )

    def test_allow_local_execution_must_be_a_bool(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": [
                        {"stage": "read_qc", "allow_local_execution": "true"}
                    ]
                }
            )

        self.assertIn("allow_local_execution", str(ctx.exception))

    def test_manual_only_stages_reject_local_execution(self):
        for stage in ("gapcloser", "base_polish"):
            with self.subTest(stage=stage), self.assertRaises(ValidationError) as ctx:
                RunConfig.model_validate(
                    {"requested_stages": _local_stage_requests(stage)}
                )
            self.assertIn(stage, str(ctx.exception))

    def test_legacy_local_exec_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {"requested_stages": [], "local_exec": ["read_qc"]}
            )

        self.assertIn("local_exec", str(ctx.exception))

    def test_local_assembly_can_defer_method_and_parameters_to_stage_time(self):
        config = RunConfig.model_validate(
            {"requested_stages": _local_stage_requests("assembly")}
        )

        self.assertEqual(config.requested_stage_names(), ["assembly"])
        self.assertIsNone(config.assembly)

    def test_assembly_method_and_parameters_are_stage_owned(self):
        cases = (
            ("assembly_method", "flye"),
            ("flye_parameters", {"threads": 4}),
            ("nextdenovo_parameters", {"threads": 4}),
            ("hifiasm_parameters", {"threads": 4}),
            ("hifiasm_ont_parameters", {"threads": 4}),
        )

        for field, value in cases:
            with self.subTest(field=field), self.assertRaises(ValidationError) as ctx:
                RunConfig.model_validate(
                    {
                        "requested_stages": _local_stage_requests("assembly"),
                        "assembly": {field: value},
                    }
                )
            self.assertIn(field, str(ctx.exception))

    def test_assembly_phase8_fields_are_accepted(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [],
                "assembly": {
                    "hifi_reads_ref": "hifi.fofn",
                    "hic_reads_ref": "hic.fofn",
                    "target_level": "near_t2t",
                    "assembly_route": "hifi",
                },
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.hifi_reads_ref, "hifi.fofn")
        self.assertEqual(config.assembly.hic_reads_ref, "hic.fofn")
        self.assertEqual(config.assembly.target_level, "near_t2t")
        self.assertEqual(config.assembly.assembly_route, "hifi")

    def test_assembly_recommendation_policy_defaults_and_ablation_modes(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("assembly"),
                "assembly": {
                    "method_route_advisor": "deterministic_baseline",
                    "parameter_advisor": "advisor",
                },
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.method_route_advisor, "deterministic_baseline")
        self.assertEqual(config.assembly.parameter_advisor, "advisor")

        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": _local_stage_requests("assembly"),
                    "assembly": {"method_route_advisor": "enabled"},
                }
            )
        self.assertIn("method_route_advisor", str(ctx.exception))

    def test_route_consistency_allows_compatible_inferred_method(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [],
                "assembly": {
                    "assembly_route": "hifi",
                    "target_level": "near_t2t",
                },
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.assembly_route, "hifi")

    def test_run_config_rejects_manifest_owned_genome_size(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "shared_parameters": {
                        "estimated_genome_size": 2_000_000,
                    },
                }
            )

        self.assertIn("estimated_genome_size", str(ctx.exception))

    def test_read_qc_length_filter_choice_is_accepted(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("read_qc"),
                "read_qc": {
                    "length_filter": {
                        "approved": True,
                        "thresholds": {"min_len": 1000},
                    }
                },
            }
        )

        assert config.read_qc is not None
        assert config.read_qc.length_filter is not None
        self.assertEqual(
            config.read_qc.length_filter.selected_method(),
            "read_qc.length_filter.seqkit_seq",
        )
        self.assertEqual(config.read_qc.length_filter.thresholds, {"min_len": 1000})

    def test_genome_survey_parameters_are_accepted_without_read_length(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("genome_survey"),
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

    def test_genome_survey_seed_freezes_shared_ploidy(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("genome_survey"),
                "shared_parameters": {
                    "species_facts": {
                        "ploidy": 4,
                    }
                },
                "genome_survey": {
                    "survey_parameters": {"kmer_size": 21, "hash_size": "1G"}
                },
            }
        )

        seeds = _stage_seeds_from_run_config(config, shared_parameters=None)

        self.assertEqual(seeds["genome_survey"]["survey_parameters"]["ploidy"], 4)

    def test_assembly_threads_are_method_independent(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("assembly"),
                "assembly": {"threads": 12},
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.threads, 12)
        self.assertEqual(
            _stage_seeds_from_run_config(config, shared_parameters=None)["assembly"][
                "threads"
            ],
            12,
        )

        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "requested_stages": _local_stage_requests("assembly"),
                    "assembly": {"threads": 0},
                }
            )

    def test_genome_survey_read_length_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": _local_stage_requests("genome_survey"),
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
                    "requested_stages": [],
                    "read_qc": {
                        "length_filter": {
                            "approved": True,
                            "thresholds": {},
                        }
                    },
                }
            )

        self.assertIn("min_len", str(ctx.exception))

    def test_read_qc_method_selection_field_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "read_qc": {
                        "length_filter": {
                            "method": "read_qc.length_filter.seqkit_seq",
                            "approved": True,
                            "thresholds": {"min_len": 1000},
                        }
                    },
                }
            )

        self.assertIn("method", str(ctx.exception))

    def test_assembly_review_quast_parameters_are_accepted(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("assembly_review"),
                "assembly_review": {"quast": {"threads": 4}},
            }
        )

        assert config.assembly_review is not None
        assert config.assembly_review.quast is not None
        self.assertEqual(config.assembly_review.quast.threads, 4)

    def test_assembly_dedup_can_be_locally_executed(self):
        config = RunConfig.model_validate(
            {"requested_stages": _local_stage_requests("assembly_dedup")}
        )
        self.assertTrue(config.requested_stages[0].allow_local_execution)

    def test_assembly_dedup_requires_explicit_automatic_purge_approval(self):
        default = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("assembly_dedup"),
                "assembly_dedup": {},
            }
        )
        approved = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("assembly_dedup"),
                "assembly_dedup": {
                    "automatic_purge_approved": True,
                    "threads": 12,
                    "get_seqs_exclude_haplotigs": False,
                },
            }
        )

        assert default.assembly_dedup is not None
        self.assertFalse(default.assembly_dedup.automatic_purge_approved)
        self.assertEqual(
            _stage_seeds_from_run_config(approved, shared_parameters=None)[
                "assembly_dedup"
            ],
            {
                "automatic_purge_approved": True,
                "threads": 12,
                "get_seqs_exclude_haplotigs": False,
            },
        )

        with self.assertRaisesRegex(
            ValidationError, "automatic_purge_approved.*allow_local_execution"
        ):
            RunConfig.model_validate(
                {
                    "requested_stages": _stage_requests("assembly_dedup"),
                    "assembly_dedup": {"automatic_purge_approved": True},
                }
            )

    def test_invalid_shared_read_type_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "shared_parameters": {
                        "read_type": "nano-raw",
                        "estimated_genome_size": 2_000_000,
                    },
                }
            )

        self.assertIn("read_type", str(ctx.exception))

    def test_unknown_stage_request_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {"requested_stages": _stage_requests("assembly", "not_a_real_stage")}
            )

        self.assertIn("not_a_real_stage", str(ctx.exception))

    def test_removed_annotation_stages_are_rejected(self):
        for stage in ("repeat_annotation", "gene_annotation", "function_annotation"):
            with self.subTest(stage=stage), self.assertRaises(ValidationError):
                RunConfig.model_validate({"requested_stages": _stage_requests(stage)})

    def test_unknown_top_level_field_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": _local_stage_requests("read_qc"),
                    "stages": ["read_qc"],
                }
            )

        self.assertIn("stages", str(ctx.exception))

    def test_unknown_assembly_field_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "assembly": {"canu_parameters": {}},
                }
            )

        self.assertIn("canu_parameters", str(ctx.exception))

    def test_local_assembly_empty_block_is_deferred(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("assembly"),
                "assembly": {},
            }
        )

        assert config.assembly is not None
        self.assertEqual(config.assembly.target_level, "draft")

    def test_review_policy_defaults_to_five_minute_countdown_and_stage_override(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": [],
                "review_policy": {
                    "stages": {
                        "assembly": {"max_blocked_seconds": -1},
                        "hic_scaffolding": {"max_blocked_seconds": 0},
                    },
                },
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
                    },
                }
            )

        self.assertIn("removed_stage", str(ctx.exception))

    def test_assembly_review_busco_parameters_accepted(self):
        cfg = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("assembly_review"),
                "assembly_review": {
                    "busco": {
                        "lineage": "embryophyta_odb10",
                        "threads": 16,
                    }
                },
            }
        )

        assert cfg.assembly_review is not None
        assert cfg.assembly_review.busco is not None
        self.assertEqual(cfg.assembly_review.busco.lineage, "embryophyta_odb10")
        self.assertEqual(cfg.assembly_review.busco.threads, 16)

    def test_requested_assembly_review_uses_host_default_without_busco_config(self):
        cfg = RunConfig.model_validate(
            {"requested_stages": _stage_requests("assembly_review")}
        )

        self.assertIsNone(cfg.assembly_review)

    def test_busco_rejects_host_database_identity_fields(self):
        for forbidden in (
            "lineage_path",
            "lineage_name",
            "lineage_version",
            "lineage_source",
        ):
            busco = _busco_config()
            busco[forbidden] = "host-owned-value"
            with self.subTest(forbidden=forbidden), self.assertRaises(ValidationError):
                RunConfig.model_validate(
                    {
                        "requested_stages": _stage_requests("assembly_review"),
                        "assembly_review": {"busco": busco},
                    }
                )

    def test_assembly_review_busco_unknown_field_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "assembly_review": {"busco": {"unknown": "x"}},
                }
            )

    def test_assembly_review_related_species_reference_dir_rejected(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "assembly_review": {
                        "related_species_reference_dir": "/ref/ncbi_metazoa"
                    },
                }
            )

    def test_read_qc_method_selection_is_not_user_owned(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": _stage_requests("read_qc"),
                    "read_qc": {
                        "length_filter": {
                            "method": "skip",
                            "approved": True,
                        }
                    },
                }
            )

        self.assertIn("method", str(ctx.exception))

    def test_approved_filter_without_local_read_qc_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": _stage_requests("read_qc"),
                    "read_qc": {
                        "length_filter": {
                            "approved": True,
                            "thresholds": {"min_len": 1000},
                        }
                    },
                }
            )

        self.assertIn("allow_local_execution", str(ctx.exception))

    def test_unapproved_filter_without_local_read_qc_is_allowed(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _stage_requests("read_qc"),
                "read_qc": {
                    "length_filter": {
                        "approved": False,
                        "thresholds": {"min_len": 1000},
                    }
                },
            }
        )

        assert config.read_qc is not None
        assert config.read_qc.length_filter is not None
        self.assertFalse(config.read_qc.length_filter.approved)

    def test_autohic_parameters_are_accepted(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("hic_scaffolding"),
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
            {"requested_stages": [], "hic_scaffolding": {"autohic_parameters": {}}}
        )
        assert defaulted.hic_scaffolding.autohic_parameters is not None
        self.assertIsNone(defaulted.hic_scaffolding.autohic_parameters.threads)
        self.assertEqual(
            defaulted.hic_scaffolding.autohic_parameters.python_command,
            "python3.9",
        )

    def test_autohic_parameter_policy_accepts_deterministic_baseline(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("hic_scaffolding"),
                "hic_scaffolding": {
                    "parameter_advisor": "deterministic_baseline",
                    "autohic_parameters": {"restriction_enzyme": "MboI"},
                },
            }
        )

        assert config.hic_scaffolding is not None
        self.assertEqual(
            config.hic_scaffolding.parameter_advisor,
            "deterministic_baseline",
        )
        self.assertEqual(
            _stage_seeds_from_run_config(config, shared_parameters=None)[
                "hic_scaffolding"
            ]["parameter_advisor"],
            "deterministic_baseline",
        )

        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": _local_stage_requests("hic_scaffolding"),
                    "hic_scaffolding": {"parameter_advisor": "disabled"},
                }
            )
        self.assertIn("parameter_advisor", str(ctx.exception))

        with self.assertRaises(ValidationError) as ctx:
            RunConfig.model_validate(
                {
                    "requested_stages": _local_stage_requests("hic_scaffolding"),
                    "hic_scaffolding": {
                        "parameter_advisor": "deterministic_baseline"
                    },
                }
            )
        self.assertIn("restriction_enzyme", str(ctx.exception))

    def test_t2t_run_config_does_not_materialize_catalog_owned_mode(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("t2t", "assembly_review"),
                "t2t": {},
            }
        )

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
                        "requested_stages": [],
                        "hic_scaffolding": {
                            "autohic_parameters": {name: value},
                        },
                    }
                )

    def test_local_hic_execution_needs_no_method_or_enzyme_choice(self):
        config = RunConfig.model_validate(
            {
                "requested_stages": _local_stage_requests("hic_scaffolding"),
                "hic_scaffolding": {},
            }
        )

        self.assertIsNotNone(config.hic_scaffolding)
        self.assertIsNone(config.hic_scaffolding.autohic_parameters)

    def test_hic_run_config_rejects_removed_method_selection_fields(self):
        with self.assertRaises(ValidationError):
            RunConfig.model_validate(
                {
                    "requested_stages": [],
                    "hic_scaffolding": {
                        "method": "autohic",
                        "methods": ["autohic"],
                    },
                }
            )


class RunConfigLoaderTests(unittest.TestCase):
    def test_load_run_config_reads_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_config.json"
            path.write_text(
                json.dumps(
                    {
                        "requested_stages": _stage_requests(
                            "read_qc",
                            "assembly",
                            "assembly_review",
                            local=("read_qc", "assembly", "assembly_review"),
                        ),
                        "assembly": {
                            "assembly_route": "hifi",
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = load_run_config(path)

        self.assertEqual(
            [
                request.stage
                for request in config.requested_stages
                if request.allow_local_execution
            ],
            ["read_qc", "assembly", "assembly_review"],
        )
        assert config.assembly is not None
        self.assertEqual(config.assembly.assembly_route, "hifi")

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

    def test_load_run_config_accepts_deferred_local_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_config.json"
            path.write_text(
                json.dumps({"requested_stages": _local_stage_requests("assembly")}),
                encoding="utf-8",
            )
            config = load_run_config(path)

        self.assertIsNone(config.assembly)

    def test_load_run_config_directory_path_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "config_dir"
            directory.mkdir()
            with self.assertRaises(ValueError) as ctx:
                load_run_config(directory)

        self.assertIn("not readable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
