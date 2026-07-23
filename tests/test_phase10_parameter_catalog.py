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

from app.control.lifecycle import resume_review_state
from app.execution.evidence import load_execution_evidence, persist_execution_result
from app.integrity import sha256_file
from app.review.base import ResumeReviewRequest
from app.review.persistence import persist_review_decision, persist_review_request
from app.schemas.artifacts import (
    ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    ASSEMBLY_PARAMETER_BASIS,
    ASSEMBLY_PARAMETER_INPUT_PROFILE,
    ASSEMBLY_PARAMETER_MODEL_REQUEST,
    ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    ASSEMBLY_PARAMETER_RECOMMENDATION,
    ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    ArtifactMeta,
    ArtifactRef,
    ParameterAttempt,
    ParameterRecommendationCursor,
)
from app.schemas.execution import ExecutionResult
from app.schemas.parameter_catalog import MethodParameterCatalog
from app.schemas.state import initial_state
from app.stages.assembly.command_builder import (
    ParameterCommandError,
    build_command,
    build_parameter_sources,
    format_hifiasm_hg_size,
)
from app.stages.assembly.method_application.flye import FLYE_READ_TYPE_FLAGS
from app.stages.assembly.method_application.hifiasm import freeze_hifiasm_attempt
from app.stages.assembly.method_application.hifiasm_ont import (
    freeze_hifiasm_ont_attempt,
)
from app.stages.assembly.method_application.nextdenovo import freeze_nextdenovo_attempt
from app.stages.assembly.parameters.catalog import (
    lint_parameter_catalogs,
    load_parameter_catalog,
    validate_source_path_contract,
)
from app.stages.assembly.parameters.profile import (
    build_full_parameter_profile,
    build_prompt_profile,
)
from app.stages.assembly.parameters.storage import (
    load_recommendation_bundle,
    read_json_artifact,
    write_recommendation_json_artifact,
)
from app.stages.assembly.parameters.workflow import (
    execution_retry_gate,
    hydrate_parameter_attempt,
    parameter_quality_gate,
    recommend_assembly_parameters,
)
from app.stages.assembly.routing import route_after_method_subgraph
from app.stages.assembly_review.builder import (
    build_stage as build_assembly_review_stage,
)
from app.storage.artifacts import (
    publish_artifact_record,
    read_artifact_records,
    resolve_artifact_ref,
)
from app.storage.paths import SessionPaths
from tests.data_v2 import v2_run_dir


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


def _v2_state(tmpdir: str, *, session_id: str = "s1") -> dict:
    run_dir = v2_run_dir(tmpdir, session_id)
    return initial_state(
        session_id=session_id,
        input_manifest_ref="control/manifest.yaml",
        data_dir=str(run_dir),
    )


def _ref_path(state: dict, ref: dict | str) -> Path:
    if isinstance(ref, dict):
        return SessionPaths.from_run_dir(state["data_dir"]).resolve_ref(ref["path_ref"])
    return Path(ref)


def _frozen_attempt_ref(attempt_id: int) -> ArtifactRef:
    marker = f"{attempt_id:x}"[-1]
    return ArtifactRef(
        artifact_id=f"art_{marker * 32}",
        path_ref=f"artifacts/assembly/attempts/{attempt_id:03d}/frozen_attempt.json",
        sha256=marker * 64,
        size_bytes=1,
        artifact_type="assembly.frozen_attempt",
    )


def _persist_parameter_attempt(
    state: dict,
    *,
    method_id: str,
    recommended_params: dict | None = None,
    status: str = "recommended",
    attempt_id: int = 0,
    task: str = "recommend_initial",
    previous_attempt: dict | None = None,
    execution_result: dict | None = None,
    execution_stderr: str = "",
) -> dict:
    """Publish a minimal valid recommendation bundle and return its compact cursor."""

    params = dict(recommended_params or {})
    catalog = load_parameter_catalog(method_id)
    common = {
        "state": state,
        "recommendation_id": attempt_id,
        "method_name": method_id,
    }
    input_profile_ref = write_recommendation_json_artifact(
        **common,
        filename="input_profile.json",
        payload={"schema_version": 2, "method_id": method_id},
        artifact_type=ASSEMBLY_PARAMETER_INPUT_PROFILE,
    )
    basis_ref = write_recommendation_json_artifact(
        **common,
        filename="basis_snapshot.json",
        payload={
            "schema_version": 2,
            "method_id": catalog.method_id,
            "catalog_snapshot": catalog.model_dump(mode="json"),
            "literature_selection": {},
            "literature_cases": [],
            "method_guidance": [],
        },
        artifact_type=ASSEMBLY_PARAMETER_BASIS,
    )
    model_request_ref = write_recommendation_json_artifact(
        **common,
        filename="model_request.json",
        payload={"schema_version": 2, "messages": []},
        artifact_type=ASSEMBLY_PARAMETER_MODEL_REQUEST,
    )
    model_response_ref = write_recommendation_json_artifact(
        **common,
        filename="model_response.json",
        payload={"schema_version": 2, "validity": "parsed"},
        artifact_type=ASSEMBLY_PARAMETER_MODEL_RESPONSE,
    )
    recommendation_ref = write_recommendation_json_artifact(
        **common,
        filename="recommendation.json",
        payload={
            "schema_version": 2,
            "method_id": catalog.method_id,
            "task": task,
            "recommendation": {
                "parameters": params,
            },
        },
        artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION,
    )
    response_parse_ref = write_recommendation_json_artifact(
        **common,
        filename="response_parse.json",
        payload={"schema_version": 1, "parsed": True, "error": None},
        artifact_type=ASSEMBLY_PARAMETER_RESPONSE_PARSE,
    )
    refs = {
        "input_profile_ref": input_profile_ref.model_dump(mode="json"),
        "basis_snapshot_ref": basis_ref.model_dump(mode="json"),
        "model_request_ref": model_request_ref.model_dump(mode="json"),
        "model_response_ref": model_response_ref.model_dump(mode="json"),
        "recommendation_ref": recommendation_ref.model_dump(mode="json"),
        "response_parse_ref": response_parse_ref.model_dump(mode="json"),
    }
    manifest_ref = write_recommendation_json_artifact(
        **common,
        filename="recommendation_manifest.json",
        payload={
            "schema_version": 2,
            "recommendation_contract": "parameter_recommendation_v1",
            "stage": "assembly",
            "method_id": method_id,
            "task": task,
            "recommendation_id": attempt_id,
            "status": "approved",
            "previous_attempt_id": (
                previous_attempt.get("attempt_id") if previous_attempt else None
            ),
            "previous_attempt_seal_hash": (
                previous_attempt.get("seal_hash") if previous_attempt else None
            ),
            "refs": refs,
        },
        artifact_type=ASSEMBLY_PARAMETER_RECOMMENDATION_MANIFEST,
    )
    persisted_request = persist_review_request(
        state,
        stage="assembly",
        gate_id="parameter_approval",
        reason="test_parameter_approval",
        payload={
            "attempt_id": attempt_id,
            "recommendation_manifest_ref": manifest_ref.model_dump(mode="json"),
            "decisions_allowed": ["approve_parameters", "reject_parameters"],
        },
        bindings={"attempt_id": attempt_id, "task": task},
    )
    approval_ref = persist_review_decision(
        state["data_dir"],
        session_id=state["session_id"],
        review_request_id=persisted_request.review_request_id,
        stage="assembly",
        gate_id="parameter_approval",
        decision={"decision": "approve_parameters", "attempt_id": attempt_id},
        request_ref=persisted_request.request_ref,
        request_hash=persisted_request.request_hash,
        artifact_type=ASSEMBLY_PARAMETER_APPROVAL_DECISION,
    )
    execution_ref = None
    if execution_result is not None:
        paths = SessionPaths.from_run_dir(state["data_dir"])
        log_dir = paths.work_path("assembly", "parameter_test_logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_source = log_dir / f"attempt_{attempt_id}.stdout.log"
        stderr_source = log_dir / f"attempt_{attempt_id}.stderr.log"
        stdout_source.write_text("", encoding="utf-8")
        stderr_source.write_text(execution_stderr, encoding="utf-8")
        stdout_published = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            ArtifactMeta.from_file(
                stdout_source,
                stage="assembly",
                method_name=method_id,
                artifact_type="command.stdout",
                metadata={"attempt_id": attempt_id},
            ),
            destination=paths.artifact_path(
                "assembly", "logs", f"parameter_attempt_{attempt_id}.stdout.log"
            ),
        )
        stderr_published = publish_artifact_record(
            state["data_dir"],
            state["session_id"],
            ArtifactMeta.from_file(
                stderr_source,
                stage="assembly",
                method_name=method_id,
                artifact_type="command.stderr",
                metadata={"attempt_id": attempt_id},
            ),
            destination=paths.artifact_path(
                "assembly", "logs", f"parameter_attempt_{attempt_id}.stderr.log"
            ),
        )
        runtime_result = ExecutionResult(
            attempt_id=f"{state['session_id']}:assembly:parameter-{attempt_id}",
            success=bool(execution_result.get("success", False)),
            return_code=int(execution_result.get("return_code", 1)),
            stdout_path=stdout_published.path,
            stderr_path=stderr_published.path,
            output_paths=[],
            status=execution_result.get("status", "failed"),
            status_detail=str(execution_result.get("status_detail", "")),
        )
        execution_ref = persist_execution_result(
            state,
            stage="assembly",
            method_name=method_id,
            result=runtime_result,
            error=str(execution_result.get("error", "")),
        )
    return ParameterAttempt(
        attempt_id=attempt_id,
        method_id=method_id,
        status=status,
        task=task,
        previous_attempt_id=(
            previous_attempt.get("attempt_id") if previous_attempt else None
        ),
        recommendation_manifest_ref=manifest_ref,
        approval_decision_ref=approval_ref,
        frozen_attempt_ref=(
            _frozen_attempt_ref(attempt_id) if status != "recommended" else None
        ),
        execution_ref=execution_ref,
        previous_attempt_seal_hash=(
            previous_attempt.get("seal_hash") if previous_attempt else None
        ),
    ).model_dump(mode="json")




def _parameter_response(
    **overrides: int | bool | str,
) -> dict:
    return {
        "parameters": dict(overrides),
        "summary": "Present this parameter recommendation for human review.",
    }


def _complete_hifiasm_ont_response(
    **overrides: int | bool | str,
) -> dict:
    catalog = load_parameter_catalog("assembly.candidate.hifiasm_ont")
    l2_parameters = {
        parameter
        for parameter, spec in catalog.parameters.items()
        if spec.level == 2
    }
    unknown = sorted(set(overrides) - l2_parameters)
    if unknown:
        raise AssertionError(f"test response contains non-L2 ONT parameters: {unknown}")
    return _parameter_response(**overrides)




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
            with patch("app.stages.assembly.parameters.catalog.CATALOG_ROOT", root):
                with self.assertRaises(ValueError):
                    load_parameter_catalog("hifiasm")


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
                    "num_seqs": 100,
                    "total_bases": 5000000000,
                    "n50": 30000,
                    "avg_len": 25000,
                    "max_len": 90000,
                    "q20_percent": 91,
                    "avg_qual": 22.5,
                }
            },
            "genome_survey": {
                "confidence": "high",
                "estimates": {
                    "genome_size": 1200000000,
                    "heterozygosity": 0.012,
                    "repeat_fraction": 0.42,
                },
                "measured_estimates": {
                    "genome_size": 1000000000,
                    "heterozygosity": 0.012,
                    "repeat_fraction": 0.42,
                },
                "provided_overrides": {"genome_size": 1200000000},
                "estimate_sources": {
                    "genome_size": "provided",
                    "heterozygosity": "survey",
                    "repeat_fraction": "survey",
                },
                "estimate_source_paths": {"genome_size": "shared_parameters.estimated_genome_size"},
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
            "parameters": {"purge_level": 1},
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

        self.assertEqual(
            set(full_profile),
            {
                "sample",
                "species_facts",
                "assets",
                "read_qc",
                "genome_survey",
                "previous_attempt",
                "attempt_history",
                "assembly_review",
            },
        )
        self.assertEqual(full_profile["sample"]["organism"], "Example species")
        self.assertEqual(full_profile["assets"]["assembly_route"], "ont")
        self.assertEqual(full_profile["assets"]["read_type"], "ont")
        self.assertEqual(full_profile["assets"]["input_files"], ["reads.fastq"])
        self.assertNotIn("has_ultra_long", full_profile["assets"])
        self.assertNotIn("has_trio", full_profile["assets"])
        self.assertEqual(full_profile["read_qc"]["read_n50"], 30000)
        self.assertEqual(full_profile["read_qc"]["read_count"], 100)
        self.assertEqual(full_profile["read_qc"]["read_mean"], 25000)
        self.assertEqual(full_profile["read_qc"]["longest_read"], 90000)
        self.assertEqual(full_profile["read_qc"]["q20_fraction"], 0.91)
        self.assertEqual(full_profile["read_qc"]["mean_quality"], 22.5)
        self.assertAlmostEqual(full_profile["read_qc"]["raw_coverage"], 5000000000 / 1200000000)
        self.assertEqual(
            full_profile["read_qc"]["raw_coverage_basis"],
            {
                "numerator_bases": 5000000000,
                "numerator_source_path": "read_qc.summary.total_bases",
                "denominator_bases": 1200000000,
                "denominator_source": "provided",
                "denominator_source_path": "shared_parameters.estimated_genome_size",
            },
        )
        self.assertEqual(full_profile["genome_survey"]["effective_genome_size"], 1200000000)
        self.assertEqual(full_profile["genome_survey"]["measured_genome_size"], 1000000000)
        self.assertEqual(full_profile["genome_survey"]["provided_genome_size"], 1200000000)
        self.assertEqual(full_profile["genome_survey"]["genome_size_source"], "provided")
        self.assertAlmostEqual(full_profile["genome_survey"]["genome_size_delta_ratio"], 0.2)
        self.assertEqual(full_profile["genome_survey"]["heterozygosity"], 0.012)
        self.assertEqual(full_profile["genome_survey"]["repeat_fraction"], 0.42)
        self.assertEqual(full_profile["previous_attempt"]["stderr_tail"], "invalid value")
        self.assertEqual(full_profile["assembly_review"]["busco_duplicate"], 0.21)

    def test_survey_measurement_drives_coverage_without_provided_override(self):
        state, assembly_out, previous_attempt = self._profile_state()
        survey = state["stage_outputs"]["genome_survey"]
        survey["estimates"]["genome_size"] = 1000000000
        survey["provided_overrides"] = {}
        survey["estimate_sources"]["genome_size"] = "survey"
        survey["estimate_source_paths"] = {}

        full_profile = build_full_parameter_profile(
            state,
            assembly_out=assembly_out,
            previous_attempt=previous_attempt,
        )

        self.assertEqual(full_profile["read_qc"]["raw_coverage"], 5.0)
        self.assertEqual(
            full_profile["read_qc"]["raw_coverage_basis"]["denominator_source_path"],
            "genome_survey.measured_estimates.genome_size",
        )
        self.assertNotIn("provided_genome_size", full_profile["genome_survey"])
        self.assertNotIn("genome_size_delta_ratio", full_profile["genome_survey"])

    def test_missing_genome_size_does_not_fabricate_coverage(self):
        state, assembly_out, previous_attempt = self._profile_state()
        state["stage_outputs"]["genome_survey"] = {
            "estimates": {"heterozygosity": 0.01, "repeat_fraction": 0.3},
            "measured_estimates": {"heterozygosity": 0.01, "repeat_fraction": 0.3},
            "estimate_sources": {"heterozygosity": "survey", "repeat_fraction": "survey"},
        }
        state["stage_outputs"]["read_qc"]["summary"]["effective_coverage_1kb"] = 99

        full_profile = build_full_parameter_profile(
            state,
            assembly_out=assembly_out,
            previous_attempt=previous_attempt,
        )

        self.assertNotIn("raw_coverage", full_profile["read_qc"])
        self.assertNotIn("effective_coverage_1kb", full_profile["read_qc"])

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
        self.assertNotIn("has_ultra_long", initial["assets"])
        self.assertNotIn("has_trio", initial["assets"])

        execution_repair = build_prompt_profile(full_profile, "retry_after_execution_failure")
        self.assertNotIn("stderr_tail", execution_repair["previous_attempt"])
        self.assertNotIn("stdout_tail", execution_repair["previous_attempt"])
        self.assertNotIn("read_qc", execution_repair)
        self.assertNotIn("assembly_review", execution_repair)

        quality_replan = build_prompt_profile(full_profile, "retry_after_review_failure")
        self.assertEqual(
            quality_replan["previous_attempt"]["parameters"],
            {"purge_level": 1},
        )
        self.assertEqual(quality_replan["assembly_review"]["assembly_size_ratio"], 1.35)
        self.assertNotIn("stderr_tail", quality_replan["previous_attempt"])

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

    def test_build_command_rejects_unrenderable_l2_without_substituting_default(self):
        with self.assertRaisesRegex(ParameterCommandError, "cannot be rendered as float"):
            build_command(
                method_id="hifiasm_ont",
                catalog=self.catalog,
                parameter_sources={
                    "l0_fixed": {"output_prefix": "asm", "threads": 1, "input_reads": ["reads.fq"], "ont_mode": True},
                    "l1_readonly": {},
                    "l2_recommended": {"purge_similarity": "default"},
                },
            )

    def test_build_parameter_sources_derives_l0_l1_and_l2(self):
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        assembly_out = {
            "hifiasm_ont_parameters": {"read_type": "ont", "threads": 4},
            "resolved_genome_size": {
                "value_bp": 1_000_000_000,
                "source": "provided",
                "source_path": "shared_parameters.estimated_genome_size",
            },
        }

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
        self.assertNotIn("hom_cov", sources["l1_readonly"])
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


    def test_format_hifiasm_hg_size_converts_bp_and_passthrough(self):
        cases = [
            (500_000_000, "500m"),
            (3_100_000_000, "3.1g"),
            (2_000_000, "2m"),
            (12_345, "12.345k"),
            (500, "0.5k"),
            ("500m", "500m"),
            ("1g", "1g"),
            ("1G", "1g"),
            ("0.5g", "0.5g"),
            ("500000000", "500m"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(format_hifiasm_hg_size(raw), expected)

    def test_format_hifiasm_hg_size_rejects_invalid(self):
        for raw in (0, -1, "0", "abc", "500bp", True, None, ""):
            with self.subTest(raw=raw):
                with self.assertRaises(ParameterCommandError):
                    format_hifiasm_hg_size(raw)

    def test_build_command_formats_bare_bp_hg_size(self):
        argv = build_command(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            parameter_sources={
                "l0_fixed": {
                    "output_prefix": "asm",
                    "threads": 4,
                    "input_reads": ["reads.fq"],
                    "ont_mode": True,
                },
                "l1_readonly": {"hg_size": 500_000_000},
                "l2_recommended": {},
            },
        )
        self.assertEqual(argv[argv.index("--hg-size") + 1], "500m")

    def test_build_parameter_sources_formats_survey_genome_size_prior_bp(self):
        state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
        state["stage_outputs"] = {
            "genome_survey": {
                "genome_size_prior": 500_000_000,
                "estimates": {"hom_cov": 30},
            }
        }
        assembly_out = {
            "hifiasm_ont_parameters": {"read_type": "ont", "threads": 4},
            "resolved_genome_size": {
                "value_bp": 500_000_000,
                "source": "survey",
                "source_path": "genome_survey.genome_size_prior",
            },
        }

        sources = build_parameter_sources(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            state=state,
            assembly_out=assembly_out,
            attempt_id=0,
            recommended_params={},
            execution_context={"output_prefix": "asm", "input_reads": ["reads.fq"]},
        )
        argv = build_command(
            method_id="hifiasm_ont",
            catalog=self.catalog,
            parameter_sources=sources,
        )

        self.assertEqual(sources["l1_readonly"]["hg_size"], "500m")
        self.assertEqual(argv[argv.index("--hg-size") + 1], "500m")

    def test_build_parameter_sources_formats_canonical_provided_genome_size_bp(self):
        for method_id, param_key in (
            ("hifiasm", "hifiasm_parameters"),
            ("hifiasm_ont", "hifiasm_ont_parameters"),
        ):
            with self.subTest(method_id=method_id):
                catalog = load_parameter_catalog(method_id)
                state = initial_state(session_id="s1", input_manifest_ref="manifest.yaml")
                assembly_out = {
                    param_key: {
                        "read_type": "ont" if method_id.endswith("ont") else "hifi",
                        "threads": 8,
                    },
                    "resolved_genome_size": {
                        "value_bp": 3_100_000_000,
                        "source": "provided",
                        "source_path": "shared_parameters.estimated_genome_size",
                    },
                }
                sources = build_parameter_sources(
                    method_id=method_id,
                    catalog=catalog,
                    state=state,
                    assembly_out=assembly_out,
                    attempt_id=0,
                    recommended_params={},
                    execution_context={"output_prefix": "asm", "input_reads": ["reads.fq"]},
                )
                self.assertEqual(sources["l1_readonly"]["hg_size"], "3.1g")

    def test_all_catalog_source_paths_match_the_static_contract(self):
        lint_parameter_catalogs()

    def test_catalog_source_path_typo_fails_closed(self):
        catalog = self.catalog.model_copy(deep=True)
        catalog.parameters["hg_size"].source_priority = ["genome_survey.estimates.genmoe_size"]
        with self.assertRaisesRegex(ValueError, "invalid source_priority"):
            validate_source_path_contract(catalog)


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
                review_request_id=interrupt["review_request_id"],
                review_request_hash=interrupt["request_hash"],
                decision="approve_parameters",
                metadata={
                    key: interrupt.get(key)
                    for key in ("attempt_id", "task", "previous_attempt_id", "recommendation_hash")
                },
            ),
        )
        return {**blocked, **update}


    def _state(self, tmpdir: str):
        state = _v2_state(tmpdir)
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
                    "read_type": "ont_raw",
                    "primary_method": "assembly.candidate.hifiasm_ont",
                    "fallback_method": None,
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
            payload = _complete_hifiasm_ont_response(
                purge_level=1, read_length_cutoff=3000
            )
            with patch("app.llm.parameters.assembly.recommender.build_chat_model", return_value=_FakeModel(payload)):
                result = recommend_assembly_parameters(state)
            artifact_records = read_artifact_records(Path(state["data_dir"]), "s1")
            assembly = result["stage_outputs"]["assembly"]
            cursor = ParameterRecommendationCursor.model_validate(
                assembly["current_parameter_recommendation"]
            )
            bundle = load_recommendation_bundle(
                {**state, "stage_outputs": result["stage_outputs"]},
                cursor.manifest_ref,
            )

        self.assertEqual(assembly["status"], "blocked_human")
        self.assertEqual(assembly.get("parameter_attempts") or [], [])
        recommendation = bundle["recommendation"]["recommendation"]
        self.assertEqual(cursor.recommendation_id, 0)
        self.assertEqual(cursor.method_id, "assembly.candidate.hifiasm_ont")
        self.assertEqual(
            bundle["manifest"]["recommendation_contract"],
            "parameter_recommendation_v1",
        )
        self.assertEqual(
            recommendation["parameters"],
            {"purge_level": 1, "read_length_cutoff": 3000},
        )
        self.assertEqual(
            bundle["basis_snapshot"]["catalog_snapshot"]["method_id"],
            "hifiasm_ont",
        )
        self.assertTrue(bundle["response_parse"]["parsed"])
        refs = bundle["manifest"]["refs"]
        self.assertIn("parameter_recommendations/000/input_profile.json", refs["input_profile_ref"]["path_ref"])
        self.assertIn("parameter_recommendations/000/model_request.json", refs["model_request_ref"]["path_ref"])
        self.assertIn("parameter_recommendations/000/model_response.json", refs["model_response_ref"]["path_ref"])
        self.assertIn("parameter_recommendations/000/response_parse.json", refs["response_parse_ref"]["path_ref"])
        artifact_types = {
            record["artifact_type"]
            for record in artifact_records
        }
        self.assertIn("assembly.parameter.input_profile", artifact_types)
        self.assertIn("assembly.parameter.model_request", artifact_types)
        self.assertIn("assembly.parameter.recommendation_manifest", artifact_types)






    def test_model_unavailable_fails_before_execution_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            with patch("app.llm.parameters.assembly.recommender.build_chat_model", side_effect=RuntimeError("missing api key")):
                result = recommend_assembly_parameters(state)
            assembly = result["stage_outputs"]["assembly"]
            cursor = ParameterRecommendationCursor.model_validate(
                assembly["current_parameter_recommendation"]
            )
            bundle = load_recommendation_bundle(
                {**state, "stage_outputs": result["stage_outputs"]},
                cursor.manifest_ref,
            )

        self.assertEqual(assembly["status"], "parameter_response_invalid")
        self.assertEqual(cursor.status, "response_invalid")
        self.assertIn(
            "missing api key",
            bundle["response_parse"]["error"],
        )
        self.assertEqual(assembly.get("parameter_attempts") or [], [])

    def test_any_parsed_parameter_mapping_reaches_human_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            payload = _parameter_response(threads=64, purge_level=9, unknown_knob="review-me")
            with patch("app.llm.parameters.assembly.recommender.build_chat_model", return_value=_FakeModel(payload)):
                result = recommend_assembly_parameters(state)
            assembly = result["stage_outputs"]["assembly"]
            cursor = ParameterRecommendationCursor.model_validate(
                assembly["current_parameter_recommendation"]
            )
            bundle = load_recommendation_bundle(
                {**state, "stage_outputs": result["stage_outputs"]},
                cursor.manifest_ref,
            )

        self.assertEqual(assembly["status"], "blocked_human")
        self.assertEqual(
            bundle["recommendation"]["recommendation"]["parameters"],
            {"threads": 64, "purge_level": 9, "unknown_knob": "review-me"},
        )

    def test_execution_retry_gate_keeps_stderr_in_execution_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr_text = "\n".join(f"line {idx}" for idx in range(150))
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm_ont",
                status="execution_failed",
                execution_result={"return_code": 1},
                execution_stderr=stderr_text,
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                }
            }

            result = execution_retry_gate(state)
            assembly = result["stage_outputs"]["assembly"]
            execution = load_execution_evidence(
                state,
                assembly["parameter_retry_request"]["failure_ref"],
                expected_stage="assembly",
                expected_method_name="assembly.candidate.hifiasm_ont",
            )

            self.assertEqual(assembly["status"], "retry_parameters_after_execution_failure")
            self.assertEqual(assembly["current_parameter_attempt_id"], 0)
            self.assertEqual(assembly["proposed_parameter_attempt_id"], 1)
            self.assertNotIn("execution_failure_log_tail", assembly)
            self.assertNotIn("stderr_path", execution.model_dump(mode="json"))
            stderr_path = resolve_artifact_ref(
                state["data_dir"],
                state["session_id"],
                execution.stderr_ref,
            )
            self.assertIn("line 149", stderr_path.read_text(encoding="utf-8"))
            self.assertNotIn("failure_class", assembly["parameter_attempts"][0])

    def test_execution_retry_gate_does_not_apply_hifiasm_classifier_to_other_methods(self):
        for method_id in (
            "assembly.candidate.flye",
            "assembly.candidate.nextdenovo",
            "assembly.candidate.hifiasm_ont",
        ):
            with self.subTest(method_id=method_id), tempfile.TemporaryDirectory() as tmpdir:
                state = _v2_state(tmpdir)
                attempt = _persist_parameter_attempt(
                    state,
                    method_id=method_id,
                    status="execution_failed",
                    execution_result={"return_code": 2},
                    execution_stderr="invalid parameter purge_level",
                )
                state["stage_outputs"] = {
                    "assembly": {
                        "status": "failed",
                        "current_parameter_attempt_id": 0,
                        "parameter_attempts": [attempt],
                    }
                }
                result = execution_retry_gate(state)
                attempt = result["stage_outputs"]["assembly"]["parameter_attempts"][0]
                self.assertNotIn("failure_class", attempt)
                self.assertNotIn("candidate_evidence_case_ref", attempt)
                self.assertIsNotNone(
                    result["stage_outputs"]["assembly"]["parameter_retry_request"]["failure_ref"]
                )

    def test_execution_retry_gate_emits_no_candidate_and_binds_sealed_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm",
                status="execution_failed",
                execution_result={
                    "success": False,
                    "return_code": 2,
                    "status": "failed",
                    "status_detail": "",
                },
                execution_stderr="invalid parameter purge_level",
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                }
            }
            result = execution_retry_gate(state)
            attempt = result["stage_outputs"]["assembly"]["parameter_attempts"][0]
            self.assertNotIn("candidate_evidence_case_ref", attempt)
            request = result["stage_outputs"]["assembly"]["parameter_retry_request"]
            self.assertEqual(request["previous_attempt_id"], 0)
            self.assertEqual(request["previous_attempt_seal_hash"], attempt["seal_hash"])
            self.assertEqual(request["failure_ref"], attempt["execution_ref"])


    def test_freeze_failure_without_execution_result_does_not_route_to_execution_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.flye",
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "failed",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                }
            }

            self.assertEqual(route_after_method_subgraph(state), "fail")
            self.assertEqual(execution_retry_gate(state), {})

    def test_review_quality_gate_classifies_under_purged_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _v2_state(tmpdir)
            attempt = _persist_parameter_attempt(
                state,
                method_id="assembly.candidate.hifiasm_ont",
                status="executed",
            )
            state["stage_outputs"] = {
                "assembly": {
                    "status": "draft_ready",
                    "current_parameter_attempt_id": 0,
                    "parameter_attempts": [attempt],
                },
                "assembly_review": {
                    "metrics": {"busco_complete": 0.94, "busco_duplicate": 0.21, "assembly_size_ratio": 1.35}
                },
            }

            result = parameter_quality_gate(state)

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "retry_parameters_after_review_failure")
            self.assertEqual(assembly["quality_gate"]["failure_mode"], "under_purged")
            self.assertEqual(assembly["current_parameter_attempt_id"], 0)
            self.assertEqual(assembly["proposed_parameter_attempt_id"], 1)
            review_ref = assembly["parameter_attempts"][0]["review_ref"]
            self.assertIn("attempts/000/review.json", review_ref["path_ref"])
            review_path = _ref_path(state, review_ref)
            self.assertTrue(review_path.exists())
            self.assertEqual(review_ref["sha256"], sha256_file(review_path))
            review = read_json_artifact(state, review_ref)
            self.assertEqual(review["diagnosis"]["failure_mode"], "under_purged")
            self.assertNotIn("review_failure", assembly["parameter_attempts"][0])

    def test_review_retry_recommendation_can_adjust_ont_l2_parameters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir)
            initial_payload = _complete_hifiasm_ont_response(
                purge_level=1, read_length_cutoff=3000, score_cutoff=10
            )
            retry_payload = _complete_hifiasm_ont_response(
                purge_level=2, read_length_cutoff=5000, score_cutoff=12
            )

            with patch("app.llm.parameters.assembly.recommender.build_chat_model", return_value=_FakeModel(initial_payload)):
                first = recommend_assembly_parameters(state)
            state = self._approve(state, first)
            state["stage_outputs"]["assembly"]["status"] = "draft_ready"
            state["stage_outputs"]["assembly"]["parameter_attempts"][0]["status"] = "executed"
            state["stage_outputs"]["assembly"]["parameter_attempts"][0][
                "frozen_attempt_ref"
            ] = _frozen_attempt_ref(0).model_dump(mode="json")
            state["stage_outputs"]["assembly_review"] = {
                "metrics": {"busco_complete": 0.94, "busco_duplicate": 0.21, "assembly_size_ratio": 1.35}
            }

            gate = parameter_quality_gate(state)
            state["stage_outputs"] = gate["stage_outputs"]
            self.assertEqual(state["stage_outputs"]["assembly"]["status"], "retry_parameters_after_review_failure")
            self.assertEqual(state["stage_outputs"]["assembly"]["current_parameter_attempt_id"], 0)
            self.assertEqual(state["stage_outputs"]["assembly"]["proposed_parameter_attempt_id"], 1)

            with patch("app.llm.parameters.assembly.recommender.build_chat_model", return_value=_FakeModel(retry_payload)):
                retry = recommend_assembly_parameters(state)
            state = self._approve(state, retry)
            assembly = state["stage_outputs"]["assembly"]
            attempts = {item["attempt_id"]: item for item in assembly["parameter_attempts"]}
            hydrated = {
                attempt_id: hydrate_parameter_attempt(state, attempt)
                for attempt_id, attempt in attempts.items()
            }
            retry_bundle = load_recommendation_bundle(
                state,
                attempts[1]["recommendation_manifest_ref"],
            )

        self.assertEqual(assembly["status"], "parameters_approved")
        self.assertEqual(attempts[1]["previous_attempt_id"], 0)
        self.assertEqual(hydrated[0]["review_failure"]["failure_mode"], "under_purged")
        self.assertEqual(
            retry_bundle["input_profile"]["assembly_review"]["failure_mode"],
            "under_purged",
        )
        self.assertEqual(hydrated[0]["recommended_params"]["purge_level"], 1)
        self.assertEqual(hydrated[1]["recommended_params"]["purge_level"], 2)
        self.assertEqual(hydrated[0]["recommended_params"]["read_length_cutoff"], 3000)
        self.assertEqual(hydrated[1]["recommended_params"]["read_length_cutoff"], 5000)
        self.assertEqual(hydrated[0]["recommended_params"]["score_cutoff"], 10)
        self.assertEqual(hydrated[1]["recommended_params"]["score_cutoff"], 12)
        argv = build_command(
            method_id="hifiasm_ont",
            catalog=load_parameter_catalog("hifiasm_ont"),
            parameter_sources={
                "l0_fixed": {"output_prefix": "asm", "threads": 1, "input_reads": ["reads.fastq"], "ont_mode": True},
                "l1_readonly": {},
                "l2_recommended": hydrated[1]["parameters"],
            },
        )
        self.assertEqual(argv[argv.index("-l") + 1], "2")
        self.assertEqual(argv[argv.index("--rl-cut") + 1], "5000")
        self.assertEqual(argv[argv.index("--sc-cut") + 1], "12")

    def test_assembly_review_graph_contains_parameter_quality_gate(self):
        graph = build_assembly_review_stage().get_graph()

        self.assertIn("parameter_quality_gate", set(graph.nodes))


class MethodFreezeParameterFailureTests(unittest.TestCase):
    def _parameter_attempt(self, state: dict, *, method_name: str, recommended_params=None) -> dict:
        return _persist_parameter_attempt(
            state,
            method_id=method_name,
            recommended_params=dict(recommended_params or {}),
        )

    def _state(self, tmpdir: str, *, method: str, recommended_params=None, attempt_payload: dict | None = None):
        reads = Path(tmpdir) / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        state = _v2_state(tmpdir)
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
            "assembly_selection": {
                "read_type": "pacbio_hifi" if method == "hifiasm" else "ont_raw",
                "primary_method": method_name,
                "fallback_method": None,
                "source": "run_config",
                "rationale": "test fixture explicitly selects the assembly method",
            },
            param_key: {"read_type": read_type, "threads": 1},
            "current_parameter_attempt_id": 0,
        }
        if attempt_payload is not None:
            assembly["parameter_attempts"] = [attempt_payload]
        elif recommended_params is not None:
            assembly["parameter_attempts"] = [
                self._parameter_attempt(
                    state,
                    method_name=method_name,
                    recommended_params=recommended_params,
                )
            ]
        state["stage_outputs"] = {"assembly": assembly}
        return state

    def _nextdenovo_state(self, tmpdir: str, *, recommended_params=None, attempt_payload: dict | None = None):
        reads = Path(tmpdir) / "reads.fastq"
        reads.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
        state = _v2_state(tmpdir)
        assembly = {
            "status": "nextdenovo_execution_planned",
            "input_ref": str(reads),
            "input_type": "clean_long_reads",
            "env": {},
            "assembly_selection": {
                "read_type": "ont_raw",
                "primary_method": "assembly.candidate.nextdenovo",
                "fallback_method": None,
                "source": "run_config",
                "rationale": "test fixture explicitly selects the assembly method",
            },
            "nextdenovo_parameters": {"read_type": "ont", "genome_size": 2_000_000, "threads": 1},
            "resolved_genome_size": {
                "value_bp": 2_000_000,
                "source": "provided",
                "source_path": "assembly.nextdenovo_parameters.genome_size",
            },
            "current_parameter_attempt_id": 0,
        }
        if attempt_payload is not None:
            assembly["parameter_attempts"] = [attempt_payload]
        elif recommended_params is not None:
            assembly["parameter_attempts"] = [
                self._parameter_attempt(
                    state,
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

    def test_hifiasm_freeze_rejects_loose_attempt_cursor_before_work_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                method="hifiasm",
                recommended_params={"purge_level": 1},
            )
            state["stage_outputs"]["assembly"]["current_parameter_attempt_id"] = "0"
            work_dir = SessionPaths.from_run_dir(state["data_dir"]).work_path(
                "assembly", "attempts", "000", "hifiasm"
            )

            with patch(
                "app.stages.assembly.method_application.hifiasm.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "failed")
        self.assertIn("explicit non-negative integer", assembly["reason"])
        self.assertFalse(work_dir.exists())

    def test_corrupt_hifiasm_recommended_params_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm", recommended_params={})
            payload = state["stage_outputs"]["assembly"]["parameter_attempts"][0]
            payload["recommended_params"] = ["param1", "param2"]
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
            state = self._state(tmpdir, method="hifiasm_ont", recommended_params={})
            payload = state["stage_outputs"]["assembly"]["parameter_attempts"][0]
            payload["recommended_params"] = "invalid"
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
        self.assertIn("task", assembly["reason"])

    def test_hifiasm_ont_freeze_ignores_corrupt_non_current_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(
                tmpdir,
                method="hifiasm_ont",
                recommended_params={"purge_level": 1},
            )
            state["stage_outputs"]["assembly"]["parameter_attempts"].append(
                {"attempt_id": 1}
            )
            with patch(
                "app.stages.assembly.method_application.hifiasm_ont.resolve_method_env",
                return_value=(None, [], None),
            ):
                result = freeze_hifiasm_ont_attempt(state)

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "hifiasm_ont_frozen")
        self.assertEqual(assembly["parameter_attempts"][0]["status"], "frozen")
        self.assertEqual(assembly["parameter_attempts"][1], {"attempt_id": 1})

    def test_hifiasm_ont_freeze_writes_command_artifact_ref(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = self._state(tmpdir, method="hifiasm_ont", recommended_params={"purge_level": 1})
            with patch("app.stages.assembly.method_application.hifiasm_ont.resolve_method_env", return_value=(None, [], None)):
                result = freeze_hifiasm_ont_attempt(state)
            artifact_records = read_artifact_records(Path(state["data_dir"]), "s1")

            assembly = result["stage_outputs"]["assembly"]
            self.assertEqual(assembly["status"], "hifiasm_ont_frozen")
            command_ref = assembly["parameter_attempts"][0]["command_ref"]
            self.assertIn("attempts/000/command.json", command_ref["path_ref"])
            command_path = _ref_path(state, command_ref)
            self.assertTrue(command_path.exists())
            self.assertEqual(command_ref["sha256"], sha256_file(command_path))
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
            command = read_json_artifact(
                state,
                result["stage_outputs"]["assembly"]["parameter_attempts"][0]["command_ref"],
            )

        assembly = result["stage_outputs"]["assembly"]
        self.assertEqual(assembly["status"], "nextdenovo_frozen")
        self.assertEqual(command["parameter_sources"]["l2_recommended"]["read_cutoff"], 7777)
        self.assertEqual(command["rendered_parameters"]["read_cutoff"], 7777)
        self.assertEqual(command["rendered_parameters"]["nextgraph_a"], 2)
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
        self.assertIn("read_cutoff cannot be rendered as int", assembly["reason"])


if __name__ == "__main__":
    unittest.main()
