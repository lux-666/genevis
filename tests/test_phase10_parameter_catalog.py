"""Phase 10 parameter catalog, validation, and command-builder tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.domains.assembly import numeric_case_retriever as numeric_retriever_module
from app.domains.assembly.evidence_case import (
    _rank_hifiasm_prompt_case,
    validate_hifiasm_evidence_cases,
)
from app.execution.artifacts import read_artifact_records
from app.knowledge.packs import load_knowledge_pack
from app.schemas.artifacts import ParameterAttempt, file_sha256
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.state import initial_state
from app.control.lifecycle import resume_review_state
from app.review.base import ResumeReviewRequest
from app.stages.assembly.command_builder import ParameterCommandError, build_command, build_parameter_sources
from app.stages.assembly.constants import FLYE_READ_TYPE_FLAGS
from app.stages.assembly.method_application.hifiasm import freeze_hifiasm_attempt
from app.stages.assembly.method_application.hifiasm_ont import freeze_hifiasm_ont_attempt
from app.stages.assembly.method_application.nextdenovo import freeze_nextdenovo_attempt
from app.stages.assembly.nodes import (
    _parameter_prompt_contract,
    execution_retry_gate,
    parameter_quality_gate,
    recommend_assembly_parameters,
    _seal_parameter_attempt,
)
from app.stages.assembly.routing import route_after_method_subgraph
from app.stages.assembly_review.builder import build_stage as build_assembly_review_stage
from app.stages.assembly.parameter_catalog import load_parameter_catalog
from app.stages.assembly.parameter_profile import (
    build_full_parameter_profile,
    build_prompt_profile,
    flatten_profile_facts,
)
from app.stages.assembly.prompt_payload import generate_payload_ref
from app.stages.assembly.parameter_validation import validate_recommended_parameters


class _FakeResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeModel:
    def __init__(self, payload: dict):
        self.payload = payload
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return _FakeResponse(json.dumps(self.payload))


def _runtime_hifiasm_binary() -> str | None:
    explicit = os.environ.get("HIFIASM_BIN")
    if explicit:
        binary = explicit if os.path.sep in explicit else shutil.which(explicit)
        if binary and os.access(binary, os.X_OK):
            return binary
        raise AssertionError(f"HIFIASM_BIN does not point to an executable hifiasm binary: {explicit}")
    return shutil.which("hifiasm")


def _extract_hifiasm_help_flags(help_text: str) -> set[str]:
    return set(re.findall(r"(?m)^\s+(-\w|--[A-Za-z0-9][A-Za-z0-9_-]*)\b", help_text))


def _parse_semver_prefix(version_text: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version_text)
    if not match:
        raise AssertionError(f"Could not parse hifiasm version from: {version_text!r}")
    return tuple(int(part or 0) for part in match.groups())


def _project_hifiasm_min_version() -> tuple[int, int, int]:
    package_descriptor = Path(__file__).resolve().parents[1] / "descriptors" / "packages" / "hifiasm.yaml"
    descriptor_text = package_descriptor.read_text(encoding="utf-8")
    match = re.search(r'version_constraint:\s*">=([^"]+)"', descriptor_text)
    if not match:
        raise AssertionError("descriptors/packages/hifiasm.yaml does not declare a >= version_constraint")
    return _parse_semver_prefix(match.group(1))


def _fake_artifact_ref(name: str) -> dict[str, str]:
    return {
        "path_ref": f"data/artifacts/assembly/s1/parameter_attempt_0/{name}.json",
        "sha256": "0" * 64,
        "artifact_type": f"assembly.parameter.{name}",
    }


def _complete_hifiasm_methods_response(*, purge_level: int = 1) -> dict:
    catalog = load_parameter_catalog("assembly.candidate.hifiasm")
    decisions = {}
    for parameter, spec in catalog.parameters.items():
        if spec.level != 2:
            continue
        if parameter == "purge_level":
            decisions[parameter] = {
                "action": "set",
                "value": purge_level,
                "basis": "skill_heuristic",
                "heuristic_labels": ["conservative HiFi purging"],
                "supporting_case_ids": [],
                "supporting_span_ids": [],
                "reason": "Use conservative SKILL guidance for purging.",
            }
        elif spec.default is not None:
            decisions[parameter] = {
                "action": "keep_default",
                "value": spec.default,
                "basis": "catalog_default",
                "supporting_case_ids": [],
                "supporting_span_ids": [],
                "reason": "No specific evidence supports changing the catalog default.",
            }
        else:
            decisions[parameter] = {
                "action": "omit",
                "basis": "catalog_default",
                "supporting_case_ids": [],
                "supporting_span_ids": [],
                "reason": "No specific evidence supports rendering this optional parameter.",
            }
    return {
        "case_assessments": [],
        "recommendation": {"adjustable_params": decisions},
        "selected_case_ids": [],
        "summary": "Conservative HiFi parameters without case support.",
    }


def _complete_hifiasm_ont_legacy_response(**overrides: int | bool | str) -> dict:
    catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")
    l2_parameters = {
        parameter
        for parameter, spec in catalog.parameters.items()
        if spec.level == 2
    }
    unknown = sorted(set(overrides) - l2_parameters)
    if unknown:
        raise AssertionError(f"test response contains non-L2 ONT parameters: {unknown}")
    return {
        "params": dict(overrides),
        "reasoning": "Bounded ONT parameters without claimed paper support.",
        "selected_case_ids": [],
        "omitted_parameters": sorted(l2_parameters - set(overrides)),
        "uncertainty_notes": [],
        "parameter_evidence": {
            parameter: {
                "bases": ["skill_heuristic"],
                "heuristic_labels": ["bounded ONT parameter test"],
            }
            for parameter in overrides
        },
    }


def _validated_hifiasm_case(root: Path, *, with_budget_omission: bool = False) -> dict:
    case_id = "case_budget_omission" if with_budget_omission else "case_a"
    spans = (
        [
            {
                "span_id": f"methods.s{index}",
                "quote": char * 2_000,
            }
            for index, char in enumerate("abcd")
        ]
        if with_budget_omission
        else [
            {
                "span_id": "methods.a",
                "quote": "Exact excerpt.",
            }
        ]
    )
    source_root = root / "task4_case_sources"
    source_root.mkdir(parents=True, exist_ok=True)
    methods_path = source_root / "methods.md"
    methods_path.write_text(
        "A non-empty UTF-8 methods summary; YAML evidence quotes are curated independently.\n",
        encoding="utf-8",
    )
    raw_case = {
        "schema_version": 3,
        "case_id": case_id,
        "case_type": "initial",
        "source": {"kind": "paper", "title": "Task 4 validated case"},
        "applicability": {
            "read_type": "hifi",
            "source_tool": "hifiasm",
            "target_methods": ["hifiasm"],
        },
        "observed_facts": {
            "coverage": {
                "value": 30,
                "evidence_span_ids": [span["span_id"] for span in spans],
            }
        },
        "intervention": {"tool": "hifiasm", "parameters": {}},
        "rationale_claims": [],
        "outcomes": {},
        "methods_path": methods_path.name,
        "evidence_spans": spans,
        "curation": {"status": "approved", "curator": "test"},
    }
    validated = validate_hifiasm_evidence_cases([raw_case], pack_root=source_root)[0]
    return _rank_hifiasm_prompt_case(
        validated,
        numeric_match={"distance": 0.1, "weak_evidence": False},
    )


def _attested_numeric_selection(cases: list[dict], *, query_id: str) -> dict:
    case_ids = [case["case_id"] for case in cases]
    trace = [
        {
            "case_id": case["case_id"],
            "decision": "candidate",
            "hard_filter_reasons": [],
            **case["numeric_match"],
        }
        for case in cases
    ]
    selection = {
        "retrieval_backend": "hifiasm_numeric",
        "selected_case_ids": case_ids,
        "candidate_case_ids": case_ids,
        "selected_count": len(case_ids),
        "eligible_case_count": len(case_ids),
        "total_cases": len(case_ids),
        "evidence_available": bool(case_ids),
        "case_evidence_required": False,
        "warning": None,
        "query": {"query_id": query_id, "method_id": "hifiasm"},
        "trace": trace,
    }
    selection[numeric_retriever_module._RANKED_SELECTION_FIELD] = (
        numeric_retriever_module._attest_hifiasm_ranked_selection(
            selected_cases=cases,
            metadata=selection,
            trace=trace,
        )
    )
    return selection


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

    def test_flye_catalog_read_type_values_match_execution_flags(self):
        catalog = load_parameter_catalog("flye")

        self.assertEqual(catalog.parameters["read_type"].allowed_values, list(FLYE_READ_TYPE_FLAGS))

    def test_nextdenovo_catalog_keeps_behavior_knobs_in_l2(self):
        catalog = load_parameter_catalog("nextdenovo")

        self.assertEqual(catalog.parameters["read_cutoff"].level, 2)
        self.assertEqual(catalog.parameters["nextgraph_a"].level, 2)

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

    def test_runtime_hifiasm_help_flags_are_cataloged_when_binary_available(self):
        binary = _runtime_hifiasm_binary()
        if not binary:
            if os.environ.get("PHASE10_REQUIRE_HIFIASM_RUNTIME") == "1":
                self.fail("PHASE10_REQUIRE_HIFIASM_RUNTIME=1 requires HIFIASM_BIN or hifiasm on PATH.")
            self.skipTest("Set HIFIASM_BIN or put hifiasm on PATH to verify runtime help flags.")

        version_result = subprocess.run(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        runtime_version = version_result.stdout.strip()
        self.assertGreaterEqual(_parse_semver_prefix(runtime_version), _project_hifiasm_min_version())

        help_result = subprocess.run(
            [binary, "-h"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        help_flags = _extract_hifiasm_help_flags(help_result.stdout)
        self.assertIn("--ont", help_flags)
        self.assertIn("--rl-cut", help_flags)
        self.assertIn("--sc-cut", help_flags)

        for method_id in ("hifiasm", "hifiasm_ont"):
            catalog = load_parameter_catalog(method_id)
            catalog_flags = {spec.cli for spec in catalog.parameters.values() if spec.cli}
            missing = sorted(help_flags - catalog_flags)
            self.assertEqual(
                [],
                missing,
                f"{method_id} catalog is missing flags advertised by {binary} {runtime_version}: {missing}",
            )

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

    def test_nextdenovo_read_cutoff_is_valid_l2_recommendation(self):
        catalog = load_parameter_catalog("nextdenovo")

        result = validate_recommended_parameters(
            method_id="nextdenovo",
            catalog=catalog,
            recommended_params={"read_cutoff": 7777, "nextgraph_a": 2},
            readonly_facts={},
            rules={},
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["violations"], [])

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
        self.assertEqual(full_profile["assets"]["assembly_route"], "ont")
        self.assertEqual(full_profile["assets"]["read_type"], "ont")
        self.assertEqual(full_profile["assets"]["input_files"], ["reads.fastq"])
        self.assertEqual(full_profile["read_qc"]["read_n50"], 30000)
        self.assertEqual(full_profile["genome_survey"]["estimated_genome_size"], 1000000000)
        self.assertEqual(full_profile["previous_attempt"]["stderr_tail"], "invalid value")
        self.assertEqual(full_profile["assembly_review"]["busco_duplicate"], 0.21)

    def test_cyclone_route_keeps_route_separate_from_case_read_type(self):
        state, assembly_out, previous_attempt = self._profile_state()
        assembly_out["assembly_route"] = "cyclone"
        assembly_out["read_type"] = "nano-raw"
        assembly_out["input_profile"]["recommended_route"] = "cyclone"

        full_profile = build_full_parameter_profile(
            state,
            assembly_out=assembly_out,
            previous_attempt=previous_attempt,
        )
        prompt_profile = build_prompt_profile(full_profile, "recommend_initial")

        self.assertEqual(full_profile["assets"]["assembly_route"], "cyclone")
        self.assertEqual(full_profile["assets"]["read_type"], "ont")
        self.assertEqual(prompt_profile["assets"]["assembly_route"], "cyclone")
        self.assertEqual(prompt_profile["assets"]["read_type"], "ont")

    def test_hifi_route_maps_to_parameter_case_read_type(self):
        state, assembly_out, previous_attempt = self._profile_state()
        assembly_out["assembly_route"] = "hifi"
        assembly_out["input_profile"]["recommended_route"] = "hifi"

        full_profile = build_full_parameter_profile(
            state,
            assembly_out=assembly_out,
            previous_attempt=previous_attempt,
        )

        self.assertEqual(full_profile["assets"]["assembly_route"], "hifi")
        self.assertEqual(full_profile["assets"]["read_type"], "hifi")

    def test_prompt_profiles_are_task_scoped(self):
        state, assembly_out, previous_attempt = self._profile_state()
        full_profile = build_full_parameter_profile(state, assembly_out=assembly_out, previous_attempt=previous_attempt)

        initial = build_prompt_profile(full_profile, "recommend_initial")
        self.assertIn("read_qc", initial)
        self.assertIn("genome_survey", initial)
        self.assertNotIn("previous_attempt", initial)
        self.assertNotIn("input_files", initial["assets"])

        execution_repair = build_prompt_profile(full_profile, "retry_after_execution_failure")
        self.assertNotIn("stderr_tail", execution_repair["previous_attempt"])
        self.assertNotIn("stdout_tail", execution_repair["previous_attempt"])
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

    def test_build_parameter_sources_injects_l0_catalog_defaults_into_hifiasm_argv(self):
        for method_id, param_key in (
            ("assembly.candidate.hifiasm", "hifiasm_parameters"),
            ("assembly.candidate.hifiasm_ont", "hifiasm_ont_parameters"),
        ):
            with self.subTest(method_id=method_id):
                catalog = load_parameter_catalog(method_id)
                state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
                assembly_out = {param_key: {"read_type": "ont" if method_id.endswith("hifiasm_ont") else "hifi"}}

                sources = build_parameter_sources(
                    method_id=method_id,
                    catalog=catalog,
                    state=state,
                    assembly_out=assembly_out,
                    attempt_id=0,
                    recommended_params={},
                    execution_context={"output_prefix": "/tmp/hifiasm", "input_reads": ["/tmp/r.fq"]},
                )
                argv = build_command(method_id=method_id, catalog=catalog, parameter_sources=sources)

                self.assertEqual(sources["l0_fixed"]["threads"], 32)
                self.assertEqual(argv[argv.index("-t") + 1], "32")


class ParameterRecommendationNodeTests(unittest.TestCase):
    def _approve(self, state: dict, recommendation_update: dict) -> dict:
        assembly = recommendation_update["stage_outputs"]["assembly"]
        interrupt = assembly["active_interrupt"]
        self.assertIsInstance(interrupt, dict, assembly)
        blocked = {
            **state,
            "current_stage": "assembly",
            "supervision_state": "blocked_human",
            "active_interrupt": interrupt,
            "stage_outputs": recommendation_update["stage_outputs"],
        }
        update = resume_review_state(
            blocked,
            ResumeReviewRequest(
                session_id=state["session_id"],
                stage="assembly",
                gate_id="parameter_approval",
                decision="approve_parameters",
                metadata={
                    key: interrupt.get(key)
                    for key in ("attempt_id", "task", "previous_attempt_id", "recommendation_hash")
                },
            ),
        )
        return {**blocked, **update}

    def _numeric_cutover_knowledge_root(self, tmpdir: str) -> Path:
        source = (
            Path(__file__).resolve().parents[1]
            / "descriptors"
            / "knowledge_packs"
            / "assembly"
            / "parameter_recommender"
        )
        knowledge_root = Path(tmpdir) / "knowledge_packs" / "assembly"
        pack_root = knowledge_root / "parameter_recommender"
        shutil.copytree(source, pack_root)
        strategy_path = pack_root / "strategies.yaml"
        strategy_text = strategy_path.read_text(encoding="utf-8")
        self.assertIn("initial_cutover_enabled: true", strategy_text)
        return knowledge_root

    def _state(self, tmpdir: str):
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        state["shared_parameters"] = {"organism": "Example species", "target_level": "draft"}
        state["stage_outputs"] = {
            "read_qc": {"summary": {"n50": 30000, "effective_coverage_5kb": 42}},
            "genome_survey": {"estimates": {"genome_size": 1000000000, "heterozygosity": "medium"}},
            "assembly": {
                "status": "selected",
                "execution_mode": "local",
                "primary_method": "assembly.candidate.hifiasm_ont",
                "selected_route": "ont",
                "assembly_route": "ont",
                "input_ref": "reads.fastq",
                "input_type": "clean_long_reads",
                "input_profile": {"recommended_route": "ont", "has_ont": True, "has_hifi": False},
                "assembly_selection": {
                    "read_type": "ont",
                    "primary_method": "assembly.candidate.hifiasm_ont",
                    "source": "recommendation",
                    "rationale": "test",
                    "metadata": {},
                },
            },
        }
        return state

    def test_initial_recommendation_writes_replayable_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            payload = _complete_hifiasm_ont_legacy_response(
                purge_level=1, read_length_cutoff=3000
            )
            with patch("app.stages.assembly.decision.build_chat_model", return_value=_FakeModel(payload)):
                result = recommend_assembly_parameters(state)
            artifact_records = read_artifact_records(Path(tmpdir), "s1")

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "blocked_human")
        self.assertEqual(assembly.get("parameter_attempts") or [], [])
        attempt = assembly["pending_parameter_recommendation"]
        self.assertEqual(attempt["attempt_id"], 0)
        self.assertEqual(attempt["method_id"], "assembly.candidate.hifiasm_ont")
        self.assertEqual(attempt["initial_recommendation_contract"], "legacy_initial")
        self.assertEqual(
            attempt["catalog_ref"],
            "descriptors/knowledge_packs/assembly/methods/hifiasm_ont/params.yaml",
        )
        self.assertTrue(attempt["validation"]["valid"])
        self.assertIn("parameter_attempt_0/input_profile.json", attempt["input_profile_ref"]["path_ref"])
        self.assertIn("parameter_attempt_0/prompt_payload.json", attempt["prompt_payload_ref"]["path_ref"])
        self.assertIn("parameter_attempt_0/llm_response.json", attempt["llm_response_ref"]["path_ref"])
        self.assertIn("parameter_attempt_0/validation.json", attempt["validation_ref"]["path_ref"])
        artifact_types = {
            record["artifact_type"]
            for record in artifact_records
        }
        self.assertIn("assembly.parameter.input_profile", artifact_types)
        self.assertIn("assembly.parameter.prompt_payload", artifact_types)

    def test_hifiasm_recommendation_indexes_numeric_retrieval_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_root = self._numeric_cutover_knowledge_root(tmpdir)
            state = self._state(tmpdir)
            assembly = state["stage_outputs"]["assembly"]
            assembly.update(
                {
                    "primary_method": "assembly.candidate.hifiasm",
                    "selected_route": "hifi",
                    "assembly_route": "hifi",
                    "input_profile": {"recommended_route": "hifi", "has_ont": False, "has_hifi": True},
                    "assembly_selection": {
                        "read_type": "hifi",
                        "primary_method": "assembly.candidate.hifiasm",
                        "source": "recommendation",
                        "rationale": "test",
                        "metadata": {},
                    },
                }
            )
            payload = _complete_hifiasm_methods_response()
            purge_decision = payload["recommendation"]["adjustable_params"]["purge_level"]
            purge_decision["basis"] = "catalog_default"
            purge_decision.pop("heuristic_labels", None)
            model = _FakeModel(payload)
            with patch(
                "app.stages.assembly.prompt_payload.load_knowledge_pack",
                side_effect=lambda pack_id, **_kwargs: load_knowledge_pack(
                    pack_id,
                    root=knowledge_root,
                ),
            ), patch(
                "app.stages.assembly.nodes.load_knowledge_pack",
                side_effect=lambda pack_id, **_kwargs: load_knowledge_pack(
                    pack_id,
                    root=knowledge_root,
                ),
            ), patch(
                "app.stages.assembly.decision.build_chat_model",
                return_value=model,
            ):
                result = recommend_assembly_parameters(state)
            artifact_records = read_artifact_records(Path(tmpdir), "s1")

            result_assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(result_assembly["status"], "blocked_human")
            attempt = result_assembly["pending_parameter_recommendation"]
            query_ref = attempt["retrieval_query_ref"]
            trace_ref = attempt["retrieval_trace_ref"]
            self.assertIsNotNone(query_ref)
            self.assertIsNotNone(trace_ref)
            self.assertEqual(attempt["retrieval_backend"], "hifiasm_numeric")
            self.assertEqual(attempt["retrieval_query_id"], "s1:parameter_attempt_0")
            self.assertEqual(
                attempt["retrieval_candidate_case_ids"],
                [
                    "hifiasm.paper.s41597-024-03664-8",
                    "hifiasm.paper.s41467-025-65778-w",
                    "hifiasm.paper.s41467-025-65870-1",
                    "hifiasm.paper.s41586-022-05325-5",
                    "hifiasm.paper.s41588-025-02137-x",
                ],
            )
            self.assertEqual(attempt["retrieval_eligible_case_count"], 10)
            self.assertIn("weak evidence coverage", attempt["retrieval_warning"])
            evidence_ref = attempt["evidence_payload_ref"]
            self.assertIsNotNone(evidence_ref)
            self.assertTrue(attempt["validation"]["valid"], attempt["validation"])
            self.assertEqual(attempt["case_assessments"], [])
            self.assertEqual(attempt["parameter_decisions"]["purge_level"]["action"], "set")
            self.assertEqual(attempt["recommendation_summary"], payload["summary"])
            prompt_path = Path(tmpdir) / attempt["prompt_payload_ref"]["path_ref"].removeprefix(
                "data/"
            )
            replay_prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
            model_prompt = json.loads(model.messages[1]["content"])
            self.assertEqual(model_prompt, replay_prompt)
            self.assertEqual(
                model_prompt["methods_evidence"]["evidence_budget"],
                replay_prompt["methods_evidence"]["evidence_budget"],
            )
            self.assertEqual(
                replay_prompt["prompt_payload_hash"],
                generate_payload_ref(
                    {
                        key: value
                        for key, value in replay_prompt.items()
                        if key not in {"prompt_payload_ref", "prompt_payload_hash"}
                    }
                ),
            )
            for ref in (query_ref, trace_ref, evidence_ref):
                artifact_path = Path(tmpdir) / ref["path_ref"].removeprefix("data/")
                self.assertTrue(artifact_path.exists())
                self.assertEqual(ref["sha256"], file_sha256(artifact_path))

            artifact_types = {record["artifact_type"] for record in artifact_records}
            self.assertIn("assembly.parameter.retrieval_query", artifact_types)
            self.assertIn("assembly.parameter.retrieval_trace", artifact_types)
            self.assertIn("assembly.parameter.evidence_payload", artifact_types)

    def test_hifiasm_canonical_prompt_artifact_model_and_hash_preserve_omitted_spans(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_root = self._numeric_cutover_knowledge_root(tmpdir)
            state = self._state(tmpdir)
            assembly = state["stage_outputs"]["assembly"]
            assembly.update(
                {
                    "primary_method": "assembly.candidate.hifiasm",
                    "selected_route": "hifi",
                    "assembly_route": "hifi",
                    "input_profile": {
                        "recommended_route": "hifi",
                        "has_ont": False,
                        "has_hifi": True,
                    },
                    "assembly_selection": {
                        "read_type": "hifi",
                        "primary_method": "assembly.candidate.hifiasm",
                        "source": "recommendation",
                        "rationale": "test",
                        "metadata": {},
                    },
                }
            )
            evidence_case = _validated_hifiasm_case(Path(tmpdir), with_budget_omission=True)
            selection = _attested_numeric_selection(
                [evidence_case],
                query_id="s1:parameter_attempt_0",
            )
            response = _complete_hifiasm_methods_response()
            purge_decision = response["recommendation"]["adjustable_params"]["purge_level"]
            purge_decision["basis"] = "catalog_default"
            purge_decision.pop("heuristic_labels", None)
            model = _FakeModel(response)
            with patch(
                "app.stages.assembly.prompt_payload.load_knowledge_pack",
                side_effect=lambda pack_id, **_kwargs: load_knowledge_pack(
                    pack_id,
                    root=knowledge_root,
                ),
            ), patch(
                "app.stages.assembly.nodes.load_knowledge_pack",
                side_effect=lambda pack_id, **_kwargs: load_knowledge_pack(
                    pack_id,
                    root=knowledge_root,
                ),
            ), patch(
                "app.stages.assembly.prompt_payload.select_evidence_cases",
                return_value=([evidence_case], selection),
            ), patch(
                "app.stages.assembly.decision.build_chat_model",
                return_value=model,
            ):
                result = recommend_assembly_parameters(state)

            attempt = result["stage_outputs"]["assembly"]["pending_parameter_recommendation"]
            self.assertTrue(attempt["validation"]["valid"], attempt["validation"])
            prompt_path = Path(tmpdir) / attempt["prompt_payload_ref"]["path_ref"].removeprefix(
                "data/"
            )
            replay_prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
            model_prompt = json.loads(model.messages[1]["content"])
            omitted_spans = replay_prompt["methods_evidence"]["evidence_budget"][
                "omitted_spans"
            ]

            self.assertEqual(model_prompt, replay_prompt)
            self.assertNotIn("prompt_contract", replay_prompt)
            self.assertEqual(
                omitted_spans,
                [
                    {
                        "case_id": evidence_case["case_id"],
                        "span_id": "methods.s3",
                        "reason": "excerpt_chars_per_case",
                    }
                ],
            )
            self.assertEqual(
                model_prompt["methods_evidence"]["evidence_budget"]["omitted_spans"],
                omitted_spans,
            )
            self.assertEqual(
                replay_prompt["prompt_payload_hash"],
                generate_payload_ref(
                    {
                        key: value
                        for key, value in replay_prompt.items()
                        if key not in {"prompt_payload_ref", "prompt_payload_hash"}
                    }
                ),
            )

    def test_hifiasm_recommendation_default_gate_off_writes_no_numeric_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            assembly = state["stage_outputs"]["assembly"]
            assembly.update(
                {
                    "primary_method": "assembly.candidate.hifiasm",
                    "selected_route": "hifi",
                    "assembly_route": "hifi",
                    "input_profile": {"recommended_route": "hifi", "has_ont": False, "has_hifi": True},
                    "assembly_selection": {
                        "read_type": "hifi",
                        "primary_method": "assembly.candidate.hifiasm",
                        "source": "recommendation",
                        "rationale": "test",
                        "metadata": {},
                    },
                }
            )
            payload = {
                "params": {"purge_level": 1},
                "reasoning": "Use the pre-cutover compatibility backend conservatively.",
                "selected_case_ids": [],
                "omitted_parameters": [],
                "uncertainty_notes": [],
            }
            model = _FakeModel(payload)
            with patch(
                "app.stages.assembly.nodes._parameter_prompt_contract",
                return_value="legacy",
            ), patch(
                "app.stages.assembly.prompt_payload.select_evidence_cases",
                return_value=([], {
                    "selected_case_ids": [],
                    "selected_count": 0,
                    "total_cases": 0,
                    "warning": "no compatible cases",
                    "trace": [],
                    "retrieval_backend": "structured_default",
                    "hifiasm_numeric_gate": {
                        "initial_cutover_enabled": False,
                        "decision": "structured_default",
                        "reason": "initial_cutover_disabled",
                    },
                }),
            ), patch(
                "app.stages.assembly.decision.build_chat_model",
                return_value=model,
            ):
                result = recommend_assembly_parameters(state)
            artifact_records = read_artifact_records(Path(tmpdir), "s1")

            attempt = result["stage_outputs"]["assembly"]["pending_parameter_recommendation"]
            self.assertIsNone(attempt["retrieval_backend"])
            self.assertIsNone(attempt["retrieval_query_id"])
            self.assertIsNone(attempt["retrieval_query_ref"])
            self.assertIsNone(attempt["retrieval_trace_ref"])
            self.assertIsNone(attempt["evidence_payload_ref"])

            prompt_path = Path(tmpdir) / attempt["prompt_payload_ref"]["path_ref"].removeprefix("data/")
            prompt_payload = json.loads(prompt_path.read_text(encoding="utf-8"))
            self.assertNotIn("prompt_contract", prompt_payload)
            self.assertIsNone(prompt_payload["case_retrieval"])
            self.assertEqual(
                prompt_payload["case_selection"]["retrieval_backend"],
                "structured_default",
            )
            self.assertEqual(
                prompt_payload["case_selection"]["hifiasm_numeric_gate"]["reason"],
                "initial_cutover_disabled",
            )
            self.assertNotIn(
                "prompt_contract",
                json.loads(model.messages[1]["content"]),
            )

            artifact_types = {record["artifact_type"] for record in artifact_records}
            self.assertNotIn("assembly.parameter.retrieval_query", artifact_types)
            self.assertNotIn("assembly.parameter.retrieval_trace", artifact_types)
            self.assertNotIn("assembly.parameter.evidence_payload", artifact_types)

    def test_invalid_hifiasm_response_does_not_persist_retrieval_candidates_as_selected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_root = self._numeric_cutover_knowledge_root(tmpdir)
            state = self._state(tmpdir)
            assembly = state["stage_outputs"]["assembly"]
            assembly.update(
                {
                    "primary_method": "assembly.candidate.hifiasm",
                    "selected_route": "hifi",
                    "assembly_route": "hifi",
                    "input_profile": {"recommended_route": "hifi", "has_ont": False, "has_hifi": True},
                    "assembly_selection": {
                        "read_type": "hifi",
                        "primary_method": "assembly.candidate.hifiasm",
                        "source": "recommendation",
                        "rationale": "test",
                        "metadata": {},
                    },
                }
            )
            evidence_case = _validated_hifiasm_case(Path(tmpdir))
            selection = _attested_numeric_selection(
                [evidence_case],
                query_id="s1:parameter_attempt_0",
            )
            with (
                patch(
                    "app.stages.assembly.nodes.load_knowledge_pack",
                    side_effect=lambda pack_id, **_kwargs: load_knowledge_pack(
                        pack_id,
                        root=knowledge_root,
                    ),
                ),
                patch(
                    "app.stages.assembly.prompt_payload.select_evidence_cases",
                    return_value=([evidence_case], selection),
                ),
                patch(
                    "app.stages.assembly.decision.build_chat_model",
                    return_value=_FakeModel({"params": {"purge_level": 1}}),
                ),
            ):
                result = recommend_assembly_parameters(state)

        attempt = result["stage_outputs"]["assembly"]["failed_parameter_recommendation"]
        self.assertEqual(attempt["status"], "validation_failed")
        self.assertEqual(attempt["retrieval_candidate_case_ids"], ["case_a"])
        self.assertEqual(attempt["selected_case_ids"], [])
        self.assertIn("case_assessments", attempt["validation"]["violations"][0]["reason"])

    def test_model_unavailable_fails_before_execution_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            with patch("app.stages.assembly.decision.build_chat_model", side_effect=RuntimeError("missing api key")):
                result = recommend_assembly_parameters(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "parameter_validation_failed")
        attempt = assembly["failed_parameter_recommendation"]
        self.assertEqual(attempt["status"], "validation_failed")
        self.assertIn("missing api key", attempt["validation"]["violations"][0]["reason"])

    def test_invalid_level_recommendation_stops_before_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            payload = {"params": {"threads": 64, "purge_level": 1}, "reasoning": "bad"}
            with patch("app.stages.assembly.decision.build_chat_model", return_value=_FakeModel(payload)):
                result = recommend_assembly_parameters(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "parameter_validation_failed")
        self.assertTrue(any(item["param"] == "threads" for item in assembly["failed_parameter_recommendation"]["validation"]["violations"]))

    def test_execution_retry_gate_uses_last_100_stderr_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr = Path(tmpdir) / "stderr.log"
            stderr.write_text("\n".join(f"line {idx}" for idx in range(150)), encoding="utf-8")
            state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml", data_dir=tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [
                        {
                            "attempt_id": 0,
                            "method_id": "assembly.candidate.hifiasm_ont",
                            "status": "execution_failed",
                            "execution_result": {"return_code": 1, "stderr_path": str(stderr)},
                        }
                    ],
                }
            }

            result = execution_retry_gate(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "retry_parameters_after_execution_failure")
        self.assertEqual(assembly["current_parameter_attempt_id"], 0)
        self.assertEqual(assembly["proposed_parameter_attempt_id"], 1)
        self.assertNotIn("line 49", assembly["execution_failure_log_tail"])
        self.assertIn("line 149", assembly["execution_failure_log_tail"])
        self.assertNotIn("failure_class", assembly["parameter_attempts"][0])

    def test_execution_retry_gate_does_not_apply_hifiasm_classifier_to_other_methods(self):
        for method_id in (
            "assembly.candidate.flye",
            "assembly.candidate.nextdenovo",
            "assembly.candidate.hifiasm_ont",
        ):
            with self.subTest(method_id=method_id), tempfile.TemporaryDirectory() as tmpdir:
                stderr = Path(tmpdir) / "stderr.log"
                stderr.write_text("invalid parameter purge_level", encoding="utf-8")
                state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml", data_dir=tmpdir)
                state["stage_outputs"] = {
                    "assembly": {
                        "status": "failed",
                        "current_parameter_attempt_id": 0,
                        "parameter_attempts": [
                            {
                                "attempt_id": 0,
                                "method_id": method_id,
                                "status": "execution_failed",
                                "execution_result": {
                                    "return_code": 2,
                                    "stderr_path": str(stderr),
                                },
                            }
                        ],
                    }
                }
                result = execution_retry_gate(state)
                attempt = result["stage_outputs"]["assembly"]["parameter_attempts"][0]
                self.assertNotIn("failure_class", attempt)
                self.assertNotIn("candidate_evidence_case_ref", attempt)

    def test_execution_retry_gate_emits_no_candidate_and_binds_sealed_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr = Path(tmpdir) / "stderr.log"
            stderr.write_text("invalid parameter purge_level", encoding="utf-8")
            state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml", data_dir=tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 1,
                    "parameter_attempts": [
                        {
                            "attempt_id": 1,
                            "method_id": "assembly.candidate.hifiasm",
                            "task": "retry_after_execution_failure",
                            "status": "execution_failed",
                            "previous_attempt_id": 0,
                            "rendered_parameters": {"purge_level": 2},
                            "argv": ["hifiasm", "-l", "2"],
                            "proposed_parameter_changes": {
                                "purge_level": {"from": 1, "to": 2}
                            },
                            "execution_result": {
                                "success": False,
                                "return_code": 2,
                                "status": "failed",
                                "status_detail": "",
                                "stderr_path": str(stderr),
                            },
                        }
                    ],
                }
            }
            result = execution_retry_gate(state)
            attempt = result["stage_outputs"]["assembly"]["parameter_attempts"][0]
            self.assertNotIn("candidate_evidence_case_ref", attempt)
            request = result["stage_outputs"]["assembly"]["parameter_retry_request"]
            self.assertEqual(request["previous_attempt_id"], 1)
            self.assertEqual(request["previous_attempt_seal_hash"], attempt["seal_hash"])

    def test_hifiasm_curated_prompt_contract_gate_applies_to_all_three_tasks(self):
        with patch(
            "app.stages.assembly.nodes.load_knowledge_pack",
            return_value={"strategies": {"hifiasm_numeric": {"initial_cutover_enabled": True}}},
        ):
            self.assertEqual(_parameter_prompt_contract("assembly.candidate.hifiasm", "recommend_initial"), "hifiasm_methods_v1")
        curated = {"initial_recommendation_contract": "curated_initial_v1"}
        legacy = {"initial_recommendation_contract": "legacy_initial"}
        for task in ("retry_after_execution_failure", "retry_after_review_failure"):
            self.assertEqual(_parameter_prompt_contract("assembly.candidate.hifiasm", task, curated), "hifiasm_methods_v1")
            self.assertEqual(_parameter_prompt_contract("assembly.candidate.hifiasm", task, legacy), "legacy")
        with patch(
            "app.stages.assembly.nodes.load_knowledge_pack",
            return_value={"strategies": {"hifiasm_numeric": {"initial_cutover_enabled": False}}},
        ):
            self.assertEqual(_parameter_prompt_contract("assembly.candidate.hifiasm", "recommend_initial"), "legacy")

    def test_freeze_failure_without_execution_result_does_not_route_to_execution_retry(self):
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        state["stage_outputs"] = {
            "assembly": {
                "status": "failed",
                "current_parameter_attempt_id": 0,
                "parameter_attempts": [
                    {
                        "attempt_id": 0,
                        "method_id": "assembly.candidate.flye",
                        "status": "recommended",
                    }
                ],
            }
        }

        self.assertEqual(route_after_method_subgraph(state), "fail")
        self.assertEqual(execution_retry_gate(state), {})

    def test_review_quality_gate_classifies_under_purged_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml", data_dir=tmpdir)
            state["stage_outputs"] = {
                "assembly": {
                    "status": "draft_ready",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [
                        {"attempt_id": 0, "method_id": "assembly.candidate.hifiasm_ont", "status": "executed"}
                    ],
                },
                "assembly_review": {
                    "metrics": {"busco_complete": 0.94, "busco_duplicate": 0.21, "assembly_size_ratio": 1.35}
                },
            }

            result = parameter_quality_gate(state)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "retry_parameters_after_review_failure")
            self.assertEqual(assembly["review_failure"]["failure_mode"], "under_purged")
            self.assertEqual(assembly["current_parameter_attempt_id"], 0)
            self.assertEqual(assembly["proposed_parameter_attempt_id"], 1)
            review_ref = assembly["parameter_attempts"][0]["review_failure_ref"]
            self.assertIn("parameter_attempt_0/review_failure.json", review_ref["path_ref"])
            review_path = Path(tmpdir) / review_ref["path_ref"].removeprefix("data/")
            self.assertTrue(review_path.exists())
            self.assertEqual(review_ref["sha256"], file_sha256(review_path))

    def test_review_retry_recommendation_can_adjust_ont_l2_parameters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            initial_payload = _complete_hifiasm_ont_legacy_response(
                purge_level=1, read_length_cutoff=3000, score_cutoff=10
            )
            retry_payload = _complete_hifiasm_ont_legacy_response(
                purge_level=2, read_length_cutoff=5000, score_cutoff=12
            )

            with patch("app.stages.assembly.decision.build_chat_model", return_value=_FakeModel(initial_payload)):
                first = recommend_assembly_parameters(state)
            state = self._approve(state, first)
            state["stage_outputs"]["assembly"]["status"] = "draft_ready"
            state["stage_outputs"]["assembly"]["parameter_attempts"][0]["execution_result"] = {
                "status": "succeeded", "return_code": 0
            }
            state["stage_outputs"]["assembly_review"] = {
                "metrics": {"busco_complete": 0.94, "busco_duplicate": 0.21, "assembly_size_ratio": 1.35}
            }

            gate = parameter_quality_gate(state)
            state["stage_outputs"] = gate["stage_outputs"]
            self.assertEqual(state["stage_outputs"]["assembly"]["status"], "retry_parameters_after_review_failure")
            self.assertEqual(state["stage_outputs"]["assembly"]["current_parameter_attempt_id"], 0)
            self.assertEqual(state["stage_outputs"]["assembly"]["proposed_parameter_attempt_id"], 1)

            with patch("app.stages.assembly.decision.build_chat_model", return_value=_FakeModel(retry_payload)):
                retry = recommend_assembly_parameters(state)
            state = self._approve(state, retry)

        assembly = state["stage_outputs"]["assembly"]
        attempts = {item["attempt_id"]: item for item in assembly["parameter_attempts"]}
        self.assertEqual(assembly["status"], "parameters_approved")
        self.assertEqual(attempts[1]["previous_attempt_id"], 0)
        self.assertEqual(attempts[1]["review_failure"]["failure_mode"], "under_purged")
        self.assertEqual(attempts[0]["recommended_params"]["purge_level"], 1)
        self.assertEqual(attempts[1]["recommended_params"]["purge_level"], 2)
        self.assertEqual(attempts[0]["recommended_params"]["read_length_cutoff"], 3000)
        self.assertEqual(attempts[1]["recommended_params"]["read_length_cutoff"], 5000)
        self.assertEqual(attempts[0]["recommended_params"]["score_cutoff"], 10)
        self.assertEqual(attempts[1]["recommended_params"]["score_cutoff"], 12)
        argv = build_command(
            method_id="hifiasm_ont",
            catalog=load_parameter_catalog("hifiasm_ont"),
            parameter_sources={
                "l0_fixed": {"output_prefix": "asm", "threads": 1, "input_reads": ["reads.fastq"], "ont_mode": True},
                "l1_readonly": {},
                "l2_recommended": attempts[1]["recommended_params"],
            },
        )
        self.assertEqual(argv[argv.index("-l") + 1], "2")
        self.assertEqual(argv[argv.index("--rl-cut") + 1], "5000")
        self.assertEqual(argv[argv.index("--sc-cut") + 1], "12")

    def test_assembly_review_graph_contains_parameter_quality_gate(self):
        graph = build_assembly_review_stage().get_graph()

        self.assertIn("parameter_quality_gate", set(graph.nodes))


class MethodFreezeParameterFailureTests(unittest.TestCase):
    def _parameter_attempt(self, *, method_name: str, recommended_params=None) -> dict:
        return ParameterAttempt(
            attempt_id=0,
            method_id=method_name,
            status="parameters_recommended",
            input_profile_ref=_fake_artifact_ref("profile"),
            prompt_payload_ref=_fake_artifact_ref("prompt"),
            llm_response_ref=_fake_artifact_ref("response"),
            validation_ref=_fake_artifact_ref("validation"),
            recommended_params=dict(recommended_params or {}),
            validation={"valid": True, "violations": []},
        ).model_dump(mode="json")

    def _state(self, tmpdir: str, *, method: str, recommended_params=None, attempt_payload: dict | None = None):
        reads = Path(tmpdir) / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        if method == "hifiasm":
            status = "hifiasm_execution_planned"
            param_key = "hifiasm_parameters"
            method_name = "assembly.candidate.hifiasm"
            read_type = "hifi"
        else:
            status = "hifiasm_ont_execution_planned"
            param_key = "hifiasm_ont_parameters"
            method_name = "assembly.candidate.hifiasm_ont"
            read_type = "ont"
        assembly = {
            "status": status,
            "input_ref": str(reads),
            "input_type": "clean_long_reads",
            "env": {},
            "method_decision": {"primary": method_name},
            param_key: {"read_type": read_type, "threads": 1},
            "current_parameter_attempt_id": 0,
        }
        if attempt_payload is not None:
            assembly["parameter_attempts"] = [attempt_payload]
        elif recommended_params is not None:
            assembly["parameter_attempts"] = [
                self._parameter_attempt(method_name=method_name, recommended_params=recommended_params)
            ]
        state["stage_outputs"] = {"assembly": assembly}
        return state

    def _nextdenovo_state(self, tmpdir: str, *, recommended_params=None, attempt_payload: dict | None = None):
        reads = Path(tmpdir) / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        state = initial_state(session_id="s1", input_manifest_ref=str(Path(tmpdir) / "manifest.yaml"), data_dir=tmpdir)
        assembly = {
            "status": "nextdenovo_execution_planned",
            "input_ref": str(reads),
            "input_type": "clean_long_reads",
            "env": {},
            "method_decision": {"primary": "assembly.candidate.nextdenovo"},
            "nextdenovo_parameters": {"read_type": "ont", "genome_size": 2_000_000, "threads": 1},
            "current_parameter_attempt_id": 0,
        }
        if attempt_payload is not None:
            assembly["parameter_attempts"] = [attempt_payload]
        elif recommended_params is not None:
            assembly["parameter_attempts"] = [
                self._parameter_attempt(
                    method_name="assembly.candidate.nextdenovo",
                    recommended_params=recommended_params,
                )
            ]
        state["stage_outputs"] = {"assembly": assembly}
        return state

    def test_missing_hifiasm_catalog_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm")
            with patch("app.stages.assembly.method_application.hifiasm.resolve_method_env", return_value=(None, [], None)):
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
            payload = self._parameter_attempt(method_name="assembly.candidate.hifiasm")
            payload["recommended_params"] = ["param1", "param2"]
            state = self._state(tmpdir, method="hifiasm", attempt_payload=payload)
            with patch("app.stages.assembly.method_application.hifiasm.resolve_method_env", return_value=(None, [], None)):
                result = freeze_hifiasm_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("ParameterAttempt", assembly["reason"])
        self.assertIn("recommended_params", assembly["reason"])

    def test_missing_hifiasm_ont_catalog_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm_ont")
            with patch("app.stages.assembly.method_application.hifiasm_ont.resolve_method_env", return_value=(None, [], None)):
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
            payload = self._parameter_attempt(method_name="assembly.candidate.hifiasm_ont")
            payload["recommended_params"] = "invalid"
            state = self._state(tmpdir, method="hifiasm_ont", attempt_payload=payload)
            with patch("app.stages.assembly.method_application.hifiasm_ont.resolve_method_env", return_value=(None, [], None)):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("ParameterAttempt", assembly["reason"])
        self.assertIn("recommended_params", assembly["reason"])

    def test_hifiasm_ont_freeze_rejects_untyped_parameter_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                method="hifiasm_ont",
                attempt_payload={"attempt_id": 0, "recommended_params": {"purge_level": 1}},
            )
            with patch("app.stages.assembly.method_application.hifiasm_ont.resolve_method_env", return_value=(None, [], None)):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("ParameterAttempt", assembly["reason"])
        self.assertIn("input_profile_ref", assembly["reason"])

    def test_hifiasm_ont_freeze_writes_command_artifact_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm_ont", recommended_params={"purge_level": 1})
            with patch("app.stages.assembly.method_application.hifiasm_ont.resolve_method_env", return_value=(None, [], None)):
                result = freeze_hifiasm_ont_attempt(state)
            artifact_records = read_artifact_records(Path(tmpdir), "s1")

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "hifiasm_ont_frozen")
            command_ref = assembly["parameter_attempts"][0]["command_ref"]
            self.assertIn("parameter_attempt_0/command.json", command_ref["path_ref"])
            command_path = Path(tmpdir) / command_ref["path_ref"].removeprefix("data/")
            self.assertTrue(command_path.exists())
            self.assertEqual(command_ref["sha256"], file_sha256(command_path))
            command_payload = json.loads(command_path.read_text(encoding="utf-8"))
            self.assertEqual(command_payload["method_id"], "assembly.candidate.hifiasm_ont")
            self.assertIn("--ont", command_payload["argv"])
            self.assertIn("assembly.parameter.command", {record["artifact_type"] for record in artifact_records})

    def test_nextdenovo_freeze_rejects_l2_override_of_l0_parameter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._nextdenovo_state(tmpdir, recommended_params={"threads": 64})
            with patch("app.stages.assembly.method_application.nextdenovo.resolve_method_env", return_value=(None, [], None)):
                result = freeze_nextdenovo_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("NextDenovo parameter sources are invalid", assembly["reason"])
        self.assertIn("threads belongs to L0", assembly["reason"])

    def test_nextdenovo_freeze_renders_l2_read_cutoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._nextdenovo_state(tmpdir, recommended_params={"read_cutoff": 7777, "nextgraph_a": 2})
            with patch("app.stages.assembly.method_application.nextdenovo.resolve_method_env", return_value=(None, [], None)):
                result = freeze_nextdenovo_attempt(state)
            run_cfg_text = Path(result["stage_outputs"]["assembly"]["nextdenovo_run_cfg_path"]).read_text(encoding="utf-8")

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "nextdenovo_frozen")
        attempt = assembly["parameter_attempts"][0]
        self.assertEqual(attempt["parameter_sources"]["l2_recommended"]["read_cutoff"], 7777)
        self.assertEqual(attempt["rendered_parameters"]["read_cutoff"], 7777)
        self.assertEqual(attempt["rendered_parameters"]["nextgraph_a"], 2)
        self.assertIn("read_cutoff = 7777", run_cfg_text)
        self.assertIn("nextgraph_options = -a 2", run_cfg_text)

    def test_nextdenovo_freeze_rejects_corrupt_l2_integer_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._nextdenovo_state(tmpdir, recommended_params={"read_cutoff": "bad"})
            with patch("app.stages.assembly.method_application.nextdenovo.resolve_method_env", return_value=(None, [], None)):
                result = freeze_nextdenovo_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("NextDenovo parameter sources are invalid", assembly["reason"])
        self.assertIn("read_cutoff must be an integer", assembly["reason"])


if __name__ == "__main__":
    unittest.main()
