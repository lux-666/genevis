"""Phase 10 parameter catalog, validation, and command-builder tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.state import initial_state
from app.stages.assembly.command_builder import ParameterCommandError, build_command, build_parameter_sources
from app.stages.assembly.method_application.hifiasm import freeze_hifiasm_attempt
from app.stages.assembly.method_application.hifiasm_ont import freeze_hifiasm_ont_attempt
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.parameter_profile import (
    build_full_parameter_profile,
    build_prompt_profile,
    flatten_profile_facts,
)
from app.stages.assembly.parameter_validation import validate_recommended_parameters


class ParameterCatalogSchemaTests(unittest.TestCase):
    def test_load_hifiasm_ont_catalog(self):
        catalog = load_parameter_catalog("hifiasm_ont")

        self.assertEqual(catalog.method_id, "hifiasm_ont")
        self.assertEqual(catalog.tool, "hifiasm")
        self.assertEqual(catalog.parameters["output_prefix"].level, 0)
        self.assertEqual(catalog.parameters["threads"].level, 0)
        self.assertEqual(catalog.parameters["input_reads"].level, 0)
        self.assertEqual(catalog.parameters["ont_mode"].level, 0)
        self.assertTrue(catalog.parameters["ont_mode"].fixed_value)
        self.assertEqual(catalog.parameters["hg_size"].level, 1)
        self.assertEqual(catalog.parameters["hom_cov"].level, 1)
        self.assertEqual(catalog.parameters["n_hap"].level, 1)
        self.assertEqual(catalog.parameters["purge_level"].level, 2)
        self.assertEqual(catalog.parameters["read_length_cutoff"].level, 2)
        self.assertEqual(catalog.parameters["score_cutoff"].level, 2)

    def test_catalogs_cover_supported_hifiasm_cli_surface(self):
        expected_keys = {
            "output_prefix",
            "threads",
            "input_reads",
            "help",
            "version",
            "ont_mode",
            "ignore_cached_overlaps",
            "write_paf",
            "write_ec",
            "kmer_length",
            "minimizer_window",
            "bloom_filter_bits",
            "high_freq_kmer_drop",
            "max_overlaps_per_read",
            "correction_rounds",
            "adapter_trim_length",
            "max_kmer_occurrence",
            "hg_size",
            "hom_cov",
            "n_hap",
            "assembly_cleaning_rounds",
            "contig_bubble_pop_distance",
            "unitig_bubble_pop_distance",
            "short_unitig_read_count",
            "max_overlap_drop_ratio",
            "min_overlap_drop_ratio",
            "post_join",
            "low_quality_bed_threshold",
            "break_low_coverage",
            "break_high_coverage",
            "break_overlap_rate",
            "primary_mode",
            "hap1_yak",
            "hap2_yak",
            "hap1_read_names",
            "hap2_read_names",
            "trio_lower_frequency",
            "trio_upper_frequency",
            "trio_unexpected_occurrence",
            "trio_dual",
            "purge_level",
            "purge_similarity",
            "duplicate_overlap_min_reads",
            "purge_max",
            "hic_r1",
            "hic_r2",
            "seed",
            "homology_base_similarity",
            "hic_weight_rounds",
            "hic_perturb_rounds",
            "hic_flip_fraction",
            "hic_misjoin_length",
            "ul_reads",
            "ul_error_rate",
            "ul_tip_read_count",
            "path_max_drop_ratio",
            "path_min_drop_ratio",
            "ul_cut",
            "dual_scaffold",
            "scaffold_gap",
            "telomere_motif",
            "telomere_penalty",
            "telomere_drop",
            "telomere_min_score",
            "contig_tip_read_count",
            "chimeric_support",
            "chimeric_flank",
            "read_length_cutoff",
            "score_cutoff",
        }
        for method_id in ("hifiasm", "hifiasm_ont"):
            catalog = load_parameter_catalog(method_id)
            self.assertTrue(expected_keys <= set(catalog.parameters))

    def test_load_catalog_is_cwd_independent(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                os.chdir(tmpdir)
                catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")
            finally:
                os.chdir(old_cwd)

        self.assertEqual(catalog.method_id, "hifiasm_ont")

    def test_registry_method_catalog_id_comes_from_descriptor(self):
        self.assertEqual(load_parameter_catalog("assembly.candidate.hifiasm").method_id, "hifiasm")
        self.assertEqual(load_parameter_catalog("assembly.candidate.hifiasm_ont").method_id, "hifiasm_ont")

    def test_missing_catalog_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_parameter_catalog("missing_method")

    def test_invalid_level_is_rejected(self):
        data = {
            "method_id": "bad",
            "tool": "hifiasm",
            "read_type": "ont",
            "parameters": {
                "bad_param": {
                    "cli": "--bad",
                    "level": 3,
                    "source": "parameter_recommender",
                    "value_type": "int",
                }
            },
        }
        with self.assertRaises(ValidationError):
            MethodParameterCatalog.model_validate(data)

    def test_unknown_schema_field_is_rejected(self):
        data = {
            "method_id": "bad",
            "tool": "hifiasm",
            "read_type": "ont",
            "parameters": {
                "bad_param": {
                    "cli": "--bad",
                    "level": 2,
                    "source": "parameter_recommender",
                    "value_type": "int",
                    "visible_to_llm": True,
                }
            },
        }
        with self.assertRaises(ValidationError):
            MethodParameterCatalog.model_validate(data)

    def test_loader_rejects_method_id_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "methods"
            method_dir = root / "hifiasm"
            method_dir.mkdir(parents=True)
            (method_dir / "params.yaml").write_text(
                "method_id: wrong\n"
                "tool: hifiasm\n"
                "read_type: hifi\n"
                "parameters: {}\n",
                encoding="utf-8",
            )
            with patch("app.stages.assembly.parameter_catalog.CATALOG_ROOT", root):
                with self.assertRaises(ValueError):
                    load_parameter_catalog("hifiasm")


class ParameterValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_parameter_catalog("hifiasm_ont")

    def test_valid_l2_recommendation_passes(self):
        result = validate_recommended_parameters(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            recommended_params={"purge_level": 1, "read_length_cutoff": 3000},
            readonly_facts={"coverage": 35, "heterozygosity": "medium"},
            rules={},
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["violations"], [])

    def test_unknown_parameter_fails(self):
        result = validate_recommended_parameters(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            recommended_params={"not_real": 1},
            readonly_facts={},
            rules={},
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["violations"][0]["reason"], "unknown parameter")

    def test_l0_and_l1_keys_fail(self):
        result = validate_recommended_parameters(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            recommended_params={"threads": 64, "hg_size": "2g", "purge_level": 1},
            readonly_facts={"genome_size_mb": 1000},
            rules={},
        )

        self.assertFalse(result["valid"])
        self.assertTrue({"threads", "hg_size"} <= {item["param"] for item in result["violations"]})

    def test_catalog_enum_and_bounds_fail(self):
        result = validate_recommended_parameters(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            recommended_params={"purge_level": 9, "kmer_length": 100},
            readonly_facts={},
            rules={},
        )

        self.assertFalse(result["valid"])
        reasons = {item["reason"] for item in result["violations"]}
        self.assertIn("value not in allowed_values", reasons)
        self.assertIn("value above catalog maximum", reasons)

    def test_conditional_rule_violation_fails(self):
        result = validate_recommended_parameters(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            recommended_params={"purge_level": 3},
            readonly_facts={"heterozygosity": "high"},
            rules={
                "rules": [
                    {
                        "rule_id": "high_heterozygosity_purge_ceiling",
                        "condition": {"heterozygosity": "high"},
                        "constraints": {"purge_level": [0, 1]},
                        "reason": "Aggressive purging is blocked for high-heterozygosity samples.",
                    }
                ]
            },
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["violations"][0]["rule_id"], "high_heterozygosity_purge_ceiling")

    def test_non_applicable_rule_is_skipped(self):
        result = validate_recommended_parameters(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            recommended_params={"read_length_cutoff": 10000},
            readonly_facts={},
            rules={
                "rules": [
                    {
                        "rule_id": "low_coverage_read_length_ceiling",
                        "condition": {"coverage": {"max": 30}},
                        "constraints": {"read_length_cutoff": [1000, 3000]},
                    }
                ]
            },
        )

        self.assertTrue(result["valid"])

    def test_rule_validator_uses_full_profile_facts(self):
        full_profile = {
            "genome_survey": {"heterozygosity": "high"},
            "read_qc": {"effective_coverage_5kb": 42},
        }

        result = validate_recommended_parameters(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            recommended_params={"purge_level": 3},
            readonly_facts={},
            full_profile=full_profile,
            rules={
                "rules": [
                    {
                        "rule_id": "profile_path_purge_ceiling",
                        "condition": {"genome_survey.heterozygosity": "high"},
                        "constraints": {"purge_level": [0, 1]},
                    }
                ]
            },
        )

        self.assertFalse(result["valid"])
        self.assertEqual(result["violations"][0]["rule_id"], "profile_path_purge_ceiling")


class ParameterProfileTests(unittest.TestCase):
    def _profile_state(self):
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        state["shared_parameters"] = {
            "organism": "Example species",
            "taxon_id": 12345,
            "clade": "vertebrate",
            "ploidy": 2,
            "target_level": "draft",
        }
        state["stage_outputs"] = {
            "read_qc": {
                "summary": {
                    "read_count": 100,
                    "total_bases": 5000000000,
                    "n50": 30000,
                    "q20_fraction": 0.91,
                    "effective_coverage_5kb": 38,
                }
            },
            "genome_survey": {
                "confidence": "high",
                "estimates": {
                    "genome_size": 1000000000,
                    "heterozygosity": "medium",
                    "repeat_fraction": 0.42,
                    "kmer_peak_coverage": 35,
                },
            },
            "assembly_review": {
                "metrics": {
                    "assembly_size": 1350000000,
                    "assembly_size_ratio": 1.35,
                    "contig_n50": 12000000,
                    "busco_complete": 0.94,
                    "busco_duplicate": 0.21,
                    "quast_metrics": {"misassemblies": 2},
                }
            },
        }
        assembly_out = {
            "assembly_route": "ont",
            "input_ref": "reads.fastq",
            "input_profile": {
                "has_hifi": False,
                "has_ont": True,
                "target_level": "draft",
                "recommended_route": "ont",
            },
        }
        previous_attempt = {
            "attempt_id": 0,
            "method_id": "hifiasm_ont",
            "params": {"purge_level": 1},
            "argv": ["hifiasm", "--ont", "-l", "1", "reads.fastq"],
            "selected_case_ids": ["case_a"],
            "execution_result": {"return_code": 1},
            "failure_class": "bad_parameter",
            "stderr_tail": "invalid value",
            "stdout_tail": "hifiasm started",
        }
        return state, assembly_out, previous_attempt

    def test_full_profile_contains_audit_sections(self):
        state, assembly_out, previous_attempt = self._profile_state()

        full_profile = build_full_parameter_profile(
            state,
            assembly_out=assembly_out,
            previous_attempt=previous_attempt,
        )

        self.assertEqual(set(full_profile), {"sample", "assets", "read_qc", "genome_survey", "previous_attempt", "assembly_review"})
        self.assertEqual(full_profile["sample"]["organism"], "Example species")
        self.assertEqual(full_profile["assets"]["input_files"], ["reads.fastq"])
        self.assertEqual(full_profile["read_qc"]["read_n50"], 30000)
        self.assertEqual(full_profile["genome_survey"]["estimated_genome_size"], 1000000000)
        self.assertEqual(full_profile["previous_attempt"]["stderr_tail"], "invalid value")
        self.assertEqual(full_profile["assembly_review"]["busco_duplicate"], 0.21)

    def test_prompt_profiles_are_task_scoped(self):
        state, assembly_out, previous_attempt = self._profile_state()
        full_profile = build_full_parameter_profile(state, assembly_out=assembly_out, previous_attempt=previous_attempt)

        initial = build_prompt_profile(full_profile, "recommend_initial")
        self.assertIn("read_qc", initial)
        self.assertIn("genome_survey", initial)
        self.assertNotIn("previous_attempt", initial)
        self.assertNotIn("input_files", initial["assets"])

        execution_repair = build_prompt_profile(full_profile, "retry_after_execution_failure")
        self.assertEqual(execution_repair["previous_attempt"]["stderr_tail"], "invalid value")
        self.assertNotIn("read_qc", execution_repair)
        self.assertNotIn("assembly_review", execution_repair)

        quality_replan = build_prompt_profile(full_profile, "retry_after_review_failure")
        self.assertEqual(quality_replan["previous_attempt"]["params"], {"purge_level": 1})
        self.assertEqual(quality_replan["assembly_review"]["assembly_size_ratio"], 1.35)
        self.assertNotIn("stderr_tail", quality_replan["previous_attempt"])

    def test_flatten_profile_facts_emits_dotted_and_leaf_keys(self):
        facts = flatten_profile_facts({"genome_survey": {"heterozygosity": "high"}})

        self.assertEqual(facts["genome_survey.heterozygosity"], "high")
        self.assertEqual(facts["heterozygosity"], "high")


class CommandBuilderTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_parameter_catalog("hifiasm_ont")

    def test_build_hifiasm_ont_command_merges_sources(self):
        argv = build_command(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            parameter_sources={
                "l0_fixed": {
                    "output_prefix": "work/out/asm",
                    "threads": 8,
                    "input_reads": ["reads1.fq", "reads2.fq"],
                    "ont_mode": True,
                },
                "l1_readonly": {"hg_size": "1g", "hom_cov": 36},
                "l2_recommended": {"purge_level": 1, "read_length_cutoff": 3000, "score_cutoff": 10},
            },
        )

        self.assertEqual(argv[0], "hifiasm")
        self.assertLess(argv.index("--ont"), argv.index("-o"))
        self.assertEqual(argv[argv.index("-o") + 1], "work/out/asm")
        self.assertEqual(argv[argv.index("-t") + 1], "8")
        self.assertEqual(argv[argv.index("--hg-size") + 1], "1g")
        self.assertEqual(argv[argv.index("--hom-cov") + 1], "36")
        self.assertEqual(argv[argv.index("-l") + 1], "1")
        self.assertEqual(argv[argv.index("--rl-cut") + 1], "3000")
        self.assertEqual(argv[-2:], ["reads1.fq", "reads2.fq"])

    def test_build_command_rejects_unknown_parameter(self):
        with self.assertRaises(ParameterCommandError):
            build_command(
                method_id="hifiasm_ont",
                catalog=self.catalog,
                parameter_sources={"l0_fixed": {"not_real": 1}, "l1_readonly": {}, "l2_recommended": {}},
            )

    def test_build_command_rejects_wrong_level(self):
        with self.assertRaises(ParameterCommandError):
            build_command(
                method_id="hifiasm_ont",
                catalog=self.catalog,
                parameter_sources={"l0_fixed": {}, "l1_readonly": {}, "l2_recommended": {"hg_size": "1g"}},
            )

    def test_build_command_omits_default_values(self):
        argv = build_command(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            parameter_sources={
                "l0_fixed": {"output_prefix": "asm", "threads": 1, "input_reads": ["reads.fq"], "ont_mode": True},
                "l1_readonly": {},
                "l2_recommended": {"purge_similarity": "default"},
            },
        )

        self.assertNotIn("-s", argv)

    def test_build_parameter_sources_derives_l0_l1_and_l2(self):
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        state["shared_parameters"] = {"estimated_genome_size": "1g"}
        state["stage_outputs"] = {"genome_survey": {"estimates": {"hom_cov": 30}}}
        assembly_out = {"hifiasm_ont_parameters": {"read_type": "ont", "threads": 4}}

        sources = build_parameter_sources(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            state=state,
            assembly_out=assembly_out,
            attempt_id=0,
            recommended_params={"purge_level": 1},
            execution_context={"output_prefix": "asm", "input_reads": ["reads.fq"]},
        )

        self.assertEqual(sources["l0_fixed"]["output_prefix"], "asm")
        self.assertEqual(sources["l0_fixed"]["threads"], 4)
        self.assertTrue(sources["l0_fixed"]["ont_mode"])
        self.assertEqual(sources["l1_readonly"]["hg_size"], "1g")
        self.assertEqual(sources["l1_readonly"]["hom_cov"], 30)
        self.assertEqual(sources["l2_recommended"], {"purge_level": 1})


class MethodFreezeParameterFailureTests(unittest.TestCase):
    def _state(self, tmpdir: str, *, method: str, recommended_params=None):
        reads = Path(tmpdir) / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        if method == "hifiasm":
            status = "hifiasm_execution_planned"
            param_key = "hifiasm_parameters"
            method_name = "assembly.candidate.hifiasm"
            read_type = "hifi"
            recommended_key = "hifiasm_recommended_params"
        else:
            status = "hifiasm_ont_execution_planned"
            param_key = "hifiasm_ont_parameters"
            method_name = "assembly.candidate.hifiasm_ont"
            read_type = "ont"
            recommended_key = "hifiasm_ont_recommended_params"
        assembly = {
            "status": status,
            "input_ref": str(reads),
            "input_type": "clean_long_reads",
            "env": {},
            "method_decision": {"primary": method_name},
            param_key: {"read_type": read_type, "threads": 1},
        }
        if recommended_params is not None:
            assembly[recommended_key] = recommended_params
        state["stage_outputs"] = {"assembly": assembly}
        return state

    def test_missing_hifiasm_catalog_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm")
            with patch("app.stages.assembly.method_application.hifiasm.resolve_method_env", return_value=({}, [], None)):
                with patch(
                    "app.stages.assembly.method_application.hifiasm.load_parameter_catalog",
                    side_effect=FileNotFoundError("missing params.yaml"),
                ):
                    result = freeze_hifiasm_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("parameter catalog is not usable", assembly["reason"])

    def test_corrupt_hifiasm_recommended_params_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm", recommended_params=["param1", "param2"])
            with patch("app.stages.assembly.method_application.hifiasm.resolve_method_env", return_value=({}, [], None)):
                result = freeze_hifiasm_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("recommended parameters must be a mapping", assembly["reason"])

    def test_missing_hifiasm_ont_catalog_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm_ont")
            with patch("app.stages.assembly.method_application.hifiasm_ont.resolve_method_env", return_value=({}, [], None)):
                with patch(
                    "app.stages.assembly.method_application.hifiasm_ont.load_parameter_catalog",
                    side_effect=FileNotFoundError("missing params.yaml"),
                ):
                    result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("parameter catalog is not usable", assembly["reason"])

    def test_corrupt_hifiasm_ont_recommended_params_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm_ont", recommended_params="invalid")
            with patch("app.stages.assembly.method_application.hifiasm_ont.resolve_method_env", return_value=({}, [], None)):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("recommended parameters must be a mapping", assembly["reason"])


if __name__ == "__main__":
    unittest.main()
